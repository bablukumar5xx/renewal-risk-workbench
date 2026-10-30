"""HTTP API. Tenant comes from the bearer token, never from the URL."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..agent.runner import approve_action, build_digest, run_agent
from ..auth import Principal, current_principal
from ..config import AS_OF, TENANT_CONFIG, iso
from ..db import get_conn, one, rows, scalar, unj
from ..delivery import DeliveryError
from ..features.builder import _evidence_item, recent_signals
from ..features.registry import FEATURE_META, FEATURES

router = APIRouter(prefix="/api")
_run_locks: dict[str, threading.Lock] = {}


def _lock(tenant_id: str) -> threading.Lock:
    return _run_locks.setdefault(tenant_id, threading.Lock())


def _latest_run(conn, tenant_id: str):
    return one(conn, """SELECT * FROM agent_runs WHERE tenant_id=? AND status='success'
                        ORDER BY started_at DESC LIMIT 1""", (tenant_id,))


def _account_or_404(conn, tenant_id: str, account_id: str):
    a = one(conn, "SELECT * FROM dim_accounts WHERE tenant_id=? AND account_id=?", (tenant_id, account_id))
    if not a:
        raise HTTPException(status_code=404, detail="account not found")
    return a


# ---------------------------------------------------------------- demo access
@router.get("/demo/tenants")
def demo_tenants():
    """Only available in the demo build; production uses an SSO/API-key exchange."""
    out = []
    for cfg in TENANT_CONFIG:
        out.append({"slug": cfg["slug"], "name": cfg["name"], "api_key": cfg["api_key"],
                    "accounts": cfg["accounts"]})
    return {"tenants": out, "demo": True}


@router.get("/me")
def me(p: Principal = Depends(current_principal)):
    conn = get_conn()
    run = _latest_run(conn, p.tenant_id)
    n = scalar(conn, "SELECT COUNT(*) FROM dim_accounts WHERE tenant_id=? AND status='active'", (p.tenant_id,))
    return {"tenant_id": p.tenant_id, "name": p.tenant_name, "active_accounts": n,
            "config": {k: v for k, v in p.config.items() if k != "kill_switch"},
            "kill_switch": p.config.get("kill_switch", False),
            "latest_run": dict(run) if run else None}


# ---------------------------------------------------------------- queue
@router.get("/digest")
def digest(p: Principal = Depends(current_principal)):
    conn = get_conn()
    d = build_digest(conn, p.tenant_id)
    if not d.get("run_id"):
        raise HTTPException(status_code=404, detail="no completed run yet")
    run = one(conn, "SELECT * FROM agent_runs WHERE id=?", (d["run_id"],))
    model = one(conn, "SELECT * FROM model_versions WHERE id=?", (run["model_version"],))
    d["run"] = {**dict(run), "stats": unj(run["stats_json"])}
    d["model"] = {"id": model["id"], "trained_at": model["trained_at"],
                  "metrics": unj(model["metrics_json"])} if model else None
    d["actions"] = dict(one(conn, """SELECT COUNT(*) total,
                            SUM(CASE WHEN status='proposed' AND channel='email' THEN 1 ELSE 0 END) awaiting_approval,
                            SUM(CASE WHEN status='proposed' AND channel='task' THEN 1 ELSE 0 END) open_tasks,
                            SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) sent,
                            SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) blocked
                            FROM actions WHERE tenant_id=? AND run_id=?""", (p.tenant_id, d["run_id"])) or {})
    return d


@router.get("/queue")
def queue(
    band: Optional[str] = None,
    owner: Optional[str] = None,
    segment: Optional[str] = None,
    search: Optional[str] = None,
    include_outside_horizon: bool = False,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    p: Principal = Depends(current_principal),
):
    conn = get_conn()
    run = _latest_run(conn, p.tenant_id)
    if not run:
        raise HTTPException(status_code=404, detail="no completed run yet")
    where = ["s.tenant_id=?", "s.run_id=?"]
    params: list = [p.tenant_id, run["id"]]
    # Counts describe the same population as the table (owner/segment/search/
    # horizon), but deliberately ignore the band tab so all tabs stay populated.
    base_where = list(where)
    base_params = list(params)
    if owner:
        base_where.append("a.owner_email=?")
        base_params.append(owner)
    if segment:
        base_where.append("a.segment=?")
        base_params.append(segment)
    if search:
        base_where.append("(a.name LIKE ? OR a.account_id LIKE ?)")
        base_params += [f"%{search}%", f"%{search}%"]
    if not include_outside_horizon:
        base_where.append("json_extract(s.reason_json, '$.in_horizon') = 1")
    if band:
        where.append("s.band IN (" + ",".join("?" for _ in band.split(",")) + ")")
        params += band.split(",")
    where += [w for w in base_where[2:]]
    params += base_params[2:]
    where_sql = " AND ".join(where)
    base_sql = " AND ".join(base_where)

    total = scalar(conn, f"SELECT COUNT(*) FROM scores s JOIN dim_accounts a ON a.tenant_id=s.tenant_id AND a.account_id=s.account_id WHERE {where_sql}", params)
    rs = rows(conn, f"""SELECT s.*, a.name, a.segment, a.industry, a.owner_name, a.owner_email, a.renewal_date,
                               a.arr_cents, a.domain,
                               (SELECT status FROM actions x WHERE x.tenant_id=s.tenant_id AND x.account_id=s.account_id
                                ORDER BY x.created_at DESC LIMIT 1) AS action_status,
                               (SELECT play_name FROM actions x WHERE x.tenant_id=s.tenant_id AND x.account_id=s.account_id
                                ORDER BY x.created_at DESC LIMIT 1) AS action_play
                        FROM scores s JOIN dim_accounts a ON a.tenant_id=s.tenant_id AND a.account_id=s.account_id
                        WHERE {where_sql}
                        ORDER BY s.expected_arr_at_risk_cents DESC, s.prob DESC
                        LIMIT ? OFFSET ?""", params + [limit, offset])

    def shape(r):
        rj = unj(r["reason_json"], {})
        reasons = rj.get("reasons", [])
        return {
            "account_id": r["account_id"], "name": r["name"], "segment": r["segment"],
            "industry": r["industry"], "owner_name": r["owner_name"], "owner_email": r["owner_email"],
            "renewal_date": r["renewal_date"], "arr_cents": r["arr_cents"],
            "prob": round(r["prob"], 4), "band": r["band"],
            "expected_arr_at_risk_cents": r["expected_arr_at_risk_cents"],
            "coverage": round(r["coverage"], 2), "confidence": r["confidence"],
            "days_to_renewal": r["days_to_renewal"], "in_horizon": rj.get("in_horizon", True),
            "action_status": r["action_status"], "action_play": r["action_play"],
            "reasons": [{"label": x["label"], "contribution": x["contribution"],
                         "value_display": x["value_display"], "imputed": x.get("imputed", False)}
                        for x in reasons[:3]],
            "top_reason": next((x["label"] for x in reasons if not x.get("imputed")),
                               reasons[0]["label"] if reasons else None),
        }

    counts = {b: 0 for b in ("red", "yellow", "green")}
    for r in rows(conn, f"""SELECT s.band, COUNT(*) n, SUM(s.expected_arr_at_risk_cents) amt
                            FROM scores s JOIN dim_accounts a
                              ON a.tenant_id=s.tenant_id AND a.account_id=s.account_id
                            WHERE {base_sql} GROUP BY s.band""", base_params):
        counts[r["band"]] = {"accounts": r["n"], "expected_arr_at_risk_cents": r["amt"] or 0}
    return {"run_id": run["id"], "as_of": run["as_of"], "total": total, "counts": counts,
            "rows": [shape(r) for r in rs]}


@router.get("/owners")
def owners(p: Principal = Depends(current_principal)):
    conn = get_conn()
    rs = rows(conn, """SELECT owner_name, owner_email, COUNT(*) n FROM dim_accounts
                       WHERE tenant_id=? AND status='active' GROUP BY owner_email ORDER BY owner_name""",
              (p.tenant_id,))
    return {"rows": [dict(r) for r in rs]}


@router.get("/accounts/{account_id}")
def account(account_id: str, p: Principal = Depends(current_principal)):
    conn = get_conn()
    a = _account_or_404(conn, p.tenant_id, account_id)
    run = _latest_run(conn, p.tenant_id)
    s = one(conn, "SELECT * FROM scores WHERE tenant_id=? AND account_id=? ORDER BY as_of DESC LIMIT 1",
            (p.tenant_id, account_id))
    reasons = unj(s["reason_json"], {}).get("reasons", []) if s else []
    feats = []
    if s:
        for f in rows(conn, "SELECT * FROM score_features WHERE score_id=?", (s["id"],)):
            meta = FEATURE_META.get(f["feature"], {})
            feats.append({**dict(f), "label": meta.get("label", f["feature"]), "group": meta.get("group", ""),
                          "why": meta.get("label", "")})
        feats.sort(key=lambda x: abs(x["contribution"] or 0), reverse=True)
    contacts = [dict(r) for r in rows(conn, "SELECT * FROM contacts WHERE tenant_id=? AND account_id=?",
                                      (p.tenant_id, account_id))]
    actions = [dict(r) for r in rows(conn, "SELECT * FROM actions WHERE tenant_id=? AND account_id=? ORDER BY created_at DESC LIMIT 10",
                                     (p.tenant_id, account_id))]
    renewals = [dict(r) for r in rows(conn, "SELECT * FROM renewals WHERE tenant_id=? AND account_id=? ORDER BY renewal_date DESC",
                                      (p.tenant_id, account_id))]
    src = {r["source"]: r["n"] for r in rows(conn, """SELECT source, COUNT(*) n FROM facts
                                                     WHERE tenant_id=? AND account_id=? GROUP BY source""",
                                             (p.tenant_id, account_id))}
    model = one(conn, "SELECT * FROM model_versions WHERE id=?", (s["model_version"],)) if s else None
    return {
        "account": dict(a), "score": {**dict(s), "reasons": reasons} if s else None,
        "features": feats, "contacts": contacts, "actions": actions, "renewals": renewals,
        "source_counts": src,
        "model": {"id": model["id"], "trained_at": model["trained_at"],
                  "metrics": unj(model["metrics_json"]), "dataset_hash": model["dataset_hash"]} if model else None,
        "live_signals": recent_signals(conn, p.tenant_id, account_id, datetime.fromisoformat(run["as_of"].replace("Z", "+00:00")) if run else AS_OF, 2)
        if run else {},
    }


@router.get("/accounts/{account_id}/timeline")
def timeline(account_id: str, limit: int = Query(150, ge=1, le=500), p: Principal = Depends(current_principal)):
    conn = get_conn()
    _account_or_404(conn, p.tenant_id, account_id)
    rs = rows(conn, """SELECT * FROM facts WHERE tenant_id=? AND account_id=?
                       ORDER BY occurred_at DESC LIMIT ?""", (p.tenant_id, account_id, limit))
    return {"events": [{**_evidence_item(r), "id": r["id"]} for r in rs]}


@router.get("/lineage/fact/{fact_id}")
def lineage_fact(fact_id: str, p: Principal = Depends(current_principal)):
    conn = get_conn()
    f = one(conn, "SELECT * FROM facts WHERE tenant_id=? AND id=?", (p.tenant_id, fact_id))
    if not f:
        raise HTTPException(status_code=404, detail="fact not found")
    raw = one(conn, "SELECT * FROM raw_events WHERE tenant_id=? AND id=?", (p.tenant_id, f["raw_event_id"]))
    return {"fact": {**dict(f), "attrs": unj(f["attrs_json"])},
            "raw": {**dict(raw), "payload": unj(raw["payload_json"])} if raw else None,
            "explanation": "fact.raw_event_id -> raw_events.id; occurred_at is event time, arrived_at is ingestion time"}


@router.get("/lineage/raw/{raw_id}")
def lineage_raw(raw_id: str, p: Principal = Depends(current_principal)):
    conn = get_conn()
    raw = one(conn, "SELECT * FROM raw_events WHERE tenant_id=? AND id=?", (p.tenant_id, raw_id))
    if not raw:
        raise HTTPException(status_code=404, detail="raw event not found")
    facts = [dict(r) for r in rows(conn, "SELECT * FROM facts WHERE tenant_id=? AND raw_event_id=?",
                                   (p.tenant_id, raw_id))]
    return {"raw": {**dict(raw), "payload": unj(raw["payload_json"])}, "facts": facts}


# ---------------------------------------------------------------- actions
@router.get("/actions")
def actions(status: Optional[str] = None, channel: Optional[str] = None,
            limit: int = Query(100, ge=1, le=500), p: Principal = Depends(current_principal)):
    conn = get_conn()
    where = ["x.tenant_id=?"]
    params: list = [p.tenant_id]
    if status:
        where.append("x.status IN (" + ",".join("?" for _ in status.split(",")) + ")")
        params += status.split(",")
    if channel:
        where.append("x.channel=?")
        params.append(channel)
    rs = rows(conn, f"""SELECT x.*, a.name, a.arr_cents, a.renewal_date, a.segment, s.prob, s.band,
                               s.expected_arr_at_risk_cents
                        FROM actions x JOIN dim_accounts a ON a.tenant_id=x.tenant_id AND a.account_id=x.account_id
                        LEFT JOIN scores s ON s.id=x.score_id
                        WHERE {' AND '.join(where)} ORDER BY x.priority, x.created_at DESC LIMIT ?""",
              params + [limit])
    counts = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM actions WHERE tenant_id=? GROUP BY status", (p.tenant_id,))}
    channel_counts = dict(one(conn, """SELECT
        SUM(CASE WHEN status='proposed' AND channel='email' THEN 1 ELSE 0 END) email_proposed,
        SUM(CASE WHEN status='proposed' AND channel='task' THEN 1 ELSE 0 END) task_proposed,
        COUNT(*) total
        FROM actions WHERE tenant_id=?""", (p.tenant_id,)) or {})
    return {"counts": counts, "channel_counts": channel_counts,
            "rows": [{**dict(r), "policy": unj(r["policy_json"])} for r in rs]}


@router.get("/actions/{action_id}")
def action_detail(action_id: str, p: Principal = Depends(current_principal)):
    conn = get_conn()
    a = one(conn, "SELECT * FROM actions WHERE tenant_id=? AND id=?", (p.tenant_id, action_id))
    if not a:
        raise HTTPException(status_code=404, detail="action not found")
    events = [dict(r) for r in rows(conn, "SELECT * FROM action_events WHERE tenant_id=? AND action_id=? ORDER BY at",
                                    (p.tenant_id, action_id))]
    score = one(conn, "SELECT * FROM scores WHERE tenant_id=? AND id=?", (p.tenant_id, a["score_id"]))
    out = [dict(r) for r in rows(conn, "SELECT * FROM outbox WHERE tenant_id=? AND action_id=?", (p.tenant_id, action_id))]
    return {"action": {**dict(a), "policy": unj(a["policy_json"]),
                       "reasons": unj(score["reason_json"], {}).get("reasons", []) if score else []},
            "events": events, "outbox": out}


class DecideBody(BaseModel):
    note: Optional[str] = None


@router.post("/actions/{action_id}/approve")
def decide_approve(action_id: str, body: DecideBody, p: Principal = Depends(current_principal)):
    conn = get_conn()
    try:
        r = approve_action(conn, p.tenant_id, action_id, actor="csm@tenant")
    except DeliveryError as e:
        # The action stays proposed; nothing was written to the outbox.
        raise HTTPException(status_code=502, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return r


@router.post("/actions/{action_id}/reject")
def decide_reject(action_id: str, body: DecideBody, p: Principal = Depends(current_principal)):
    conn = get_conn()
    a = one(conn, "SELECT * FROM actions WHERE tenant_id=? AND id=?", (p.tenant_id, action_id))
    if not a:
        raise HTTPException(status_code=404, detail="action not found")
    now_s = iso(datetime.now(timezone.utc))
    conn.execute("UPDATE actions SET status='rejected', decided_by=?, decided_at=?, updated_at=? WHERE id=?",
                 ("csm@tenant", now_s, now_s, action_id))
    conn.execute("""INSERT INTO action_events (id, tenant_id, action_id, at, actor, event, detail_json)
                    VALUES (?, ?, ?, ?, 'csm@tenant', 'rejected', ?)""",
                 ("aev_" + action_id[-10:] + "_rej", p.tenant_id, action_id, now_s,
                  json.dumps({"note": body.note})))
    conn.commit()
    return {"status": "rejected"}


@router.post("/actions/{action_id}/complete")
def decide_complete(action_id: str, body: DecideBody, p: Principal = Depends(current_principal)):
    conn = get_conn()
    a = one(conn, "SELECT * FROM actions WHERE tenant_id=? AND id=?", (p.tenant_id, action_id))
    if not a:
        raise HTTPException(status_code=404, detail="action not found")
    now_s = iso(datetime.now(timezone.utc))
    conn.execute("UPDATE actions SET status='completed', decided_by=?, decided_at=?, updated_at=? WHERE id=?",
                 ("csm@tenant", now_s, now_s, action_id))
    conn.execute("""INSERT INTO action_events (id, tenant_id, action_id, at, actor, event, detail_json)
                    VALUES (?, ?, ?, ?, 'csm@tenant', 'completed', ?)""",
                 ("aev_" + action_id[-10:] + "_cmp", p.tenant_id, action_id, now_s,
                  json.dumps({"note": body.note})))
    conn.commit()
    return {"status": "completed"}


@router.get("/outbox")
def outbox(p: Principal = Depends(current_principal)):
    conn = get_conn()
    rs = rows(conn, """SELECT o.*, a.name FROM outbox o JOIN dim_accounts a ON a.tenant_id=o.tenant_id
                       AND a.account_id=o.account_id WHERE o.tenant_id=? ORDER BY o.created_at DESC LIMIT 100""",
              (p.tenant_id,))
    return {"rows": [dict(r) for r in rs]}


# ---------------------------------------------------------------- runs / model
@router.get("/runs")
def runs(p: Principal = Depends(current_principal)):
    conn = get_conn()
    rs = rows(conn, "SELECT * FROM agent_runs WHERE tenant_id=? ORDER BY started_at DESC LIMIT 50", (p.tenant_id,))
    return {"rows": [{**dict(r), "stats": unj(r["stats_json"])} for r in rs]}


@router.get("/runs/{run_id}")
def run_detail(run_id: str, p: Principal = Depends(current_principal)):
    conn = get_conn()
    r = one(conn, "SELECT * FROM agent_runs WHERE tenant_id=? AND id=?", (p.tenant_id, run_id))
    if not r:
        raise HTTPException(status_code=404, detail="run not found")
    steps = [dict(s) for s in rows(conn, "SELECT * FROM agent_steps WHERE run_id=? ORDER BY seq", (run_id,))]
    for s in steps:
        s["stats"] = unj(s["stats_json"])
    return {"run": {**dict(r), "stats": unj(r["stats_json"])}, "steps": steps}


class RunBody(BaseModel):
    as_of: Optional[str] = None


@router.post("/runs")
def trigger_run(body: RunBody, p: Principal = Depends(current_principal)):
    conn = get_conn()
    lock = _lock(p.tenant_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="a run is already in progress for this tenant")
    try:
        as_of = datetime.fromisoformat(body.as_of.replace("Z", "+00:00")) if body.as_of else datetime.now(timezone.utc)
        r = run_agent(conn, p.tenant_id, trigger="manual", as_of=as_of)
    finally:
        lock.release()
    return {"run_id": r["run_id"], "status": r["status"], "stats": {k: r[k] for k in ("ingest", "score", "actions", "digest") if k in r}}


@router.get("/model/health")
def model_health(p: Principal = Depends(current_principal)):
    conn = get_conn()
    m = one(conn, "SELECT * FROM model_versions WHERE tenant_id=? AND is_active=1 ORDER BY trained_at DESC LIMIT 1",
            (p.tenant_id,))
    if not m:
        raise HTTPException(status_code=404, detail="no active model")
    metrics = unj(m["metrics_json"])
    run = _latest_run(conn, p.tenant_id)
    scores = rows(conn, "SELECT prob, band, confidence FROM scores WHERE tenant_id=? AND run_id=?",
                  (p.tenant_id, run["id"])) if run else []
    return {
        "model": {**dict(m), "features": unj(m["features_json"]), "hyperparams": unj(m["hyperparams_json"]),
                  "metrics": metrics},
        "latest_run": dict(run) if run else None,
        "score_distribution": {
            "count": len(scores),
            "mean_prob": round(sum(s["prob"] for s in scores) / len(scores), 4) if scores else None,
            "bands": {b: sum(1 for s in scores if s["band"] == b) for b in ("red", "yellow", "green")},
            "low_confidence": sum(1 for s in scores if s["confidence"] == "low"),
        },
    }


@router.get("/model/versions")
def model_versions(p: Principal = Depends(current_principal)):
    conn = get_conn()
    return {"rows": [{**dict(r), "metrics": unj(r["metrics_json"])} for r in
                     rows(conn, "SELECT * FROM model_versions WHERE tenant_id=? ORDER BY trained_at DESC", (p.tenant_id,))]}


@router.get("/pipeline/quality")
def pipeline_quality(p: Principal = Depends(current_principal)):
    conn = get_conn()
    raw = {r["source"]: r["n"] for r in rows(conn, "SELECT source, COUNT(*) n FROM raw_events WHERE tenant_id=? GROUP BY source", (p.tenant_id,))}
    facts = {r["source"]: r["n"] for r in rows(conn, "SELECT source, COUNT(*) n FROM facts WHERE tenant_id=? GROUP BY source", (p.tenant_id,))}
    issues = {r["issue_type"]: r["n"] for r in rows(conn, "SELECT issue_type, COUNT(*) n FROM ingest_issues WHERE tenant_id=? GROUP BY issue_type", (p.tenant_id,))}
    total_raw = sum(raw.values()) or 1
    cov = rows(conn, """SELECT COUNT(*) n FROM (
                          SELECT f.account_id, COUNT(DISTINCT f.source) s
                          FROM facts f JOIN dim_accounts a
                            ON a.tenant_id=f.tenant_id AND a.account_id=f.account_id
                          WHERE f.tenant_id=? AND a.status='active'
                          GROUP BY f.account_id HAVING s >= 4)""", (p.tenant_id,))
    active = scalar(conn, "SELECT COUNT(*) FROM dim_accounts WHERE tenant_id=? AND status='active'", (p.tenant_id,)) or 1
    runs_ = rows(conn, """SELECT id, started_at, status, stats_json FROM agent_runs WHERE tenant_id=?
                          ORDER BY started_at DESC LIMIT 8""", (p.tenant_id,))
    return {
        "raw_by_source": raw, "facts_by_source": facts, "issues": issues,
        "raw_events": sum(raw.values()), "facts": sum(facts.values()),
        "duplicate_rate": round(issues.get("duplicate", 0) / total_raw, 4),
        "unmatched_rate": round((issues.get("unmatched", 0) + issues.get("orphan", 0)) / total_raw, 4),
        "tz_assumed": issues.get("tz_assumed", 0),
        "four_source_coverage": round(cov[0]["n"] / active, 3),
        "recent_runs": [{**dict(r), "stats": unj(r["stats_json"])} for r in runs_],
    }


class KillBody(BaseModel):
    enabled: bool


@router.post("/admin/kill-switch")
def kill_switch(body: KillBody, p: Principal = Depends(current_principal)):
    conn = get_conn()
    cfg = dict(p.config)
    cfg["kill_switch"] = body.enabled
    conn.execute("UPDATE tenants SET config_json=? WHERE id=?", (json.dumps(cfg), p.tenant_id))
    conn.commit()
    return {"kill_switch": body.enabled}
