"""The unattended loop.

ingest -> score -> detect change -> route to a play -> apply policy -> act.

Every step writes a row. Two idempotency rules keep repeated schedules safe:

  * the same (tenant, as_of, trigger) is a no-op that returns the original run;
  * actions are keyed by (tenant, account, play, ISO week), not by run id or
    trigger, so a later run in the same week cannot double-send.

Change detection always compares against an *earlier* as-of, never a re-run of the
same window, so a retry cannot invent a fake transition.
"""
from __future__ import annotations

import hashlib
import json
import traceback
from datetime import datetime, timedelta, timezone

from ..config import iso
from ..db import insert_many, j, one, rows, scalar, tx, unj
from ..ingest.canonicalize import canonicalize
from ..model.scoring import score_tenant
from . import policies
from .plays import choose_play, draft_email, task_description

BAND_RANK = {"green": 0, "yellow": 1, "red": 2}


def _id(prefix: str, *parts) -> str:
    return prefix + "_" + hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _step(conn, run_id: str, seq: int, name: str) -> str:
    sid = _id("stp", run_id, seq)
    conn.execute("""INSERT INTO agent_steps (id, run_id, seq, name, status, started_at, stats_json)
                    VALUES (?, ?, ?, ?, 'running', ?, '{}')""",
                 (sid, run_id, seq, name, iso(datetime.now(timezone.utc))))
    conn.commit()
    return sid


def _finish_step(conn, sid: str, status: str, stats: dict) -> None:
    conn.execute("UPDATE agent_steps SET status=?, finished_at=?, stats_json=? WHERE id=?",
                 (status, iso(datetime.now(timezone.utc)), j(stats), sid))
    conn.commit()


def _note_retrigger(conn, tenant_id: str, action_id: str, run_id: str, trigger: dict,
                    score_row, as_of) -> None:
    """Record that a later run re-derived an action we deliberately did not duplicate."""
    conn.execute("""INSERT OR IGNORE INTO action_events
                    (id, tenant_id, action_id, at, actor, event, detail_json)
                    VALUES (?, ?, ?, ?, 'agent', 'retriggered', ?)""",
                 (_id("aev", action_id, "retrigger", iso(as_of), trigger["type"], score_row["band"]),
                  tenant_id, action_id, iso(as_of),
                  j({"trigger": trigger["type"], "detail": trigger["detail"],
                     "band": score_row["band"], "prob": score_row["prob"], "run_id": run_id})))
    conn.commit()


def run_agent(conn, tenant_id: str, trigger: str = "schedule", as_of: datetime | None = None) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    run_id = _id("run", tenant_id, iso(as_of), trigger, datetime.now(timezone.utc).timestamp())
    tenant = one(conn, "SELECT * FROM tenants WHERE id=?", (tenant_id,))
    if not tenant:
        raise ValueError(f"unknown tenant {tenant_id}")

    # Scheduler retries and repeated CLI invocations for the same window must not
    # create a second run, a second score set, or a second round of actions.
    existing = one(conn, """SELECT * FROM agent_runs WHERE tenant_id=? AND as_of=? AND trigger=?
                            AND status='success' ORDER BY started_at DESC LIMIT 1""",
                   (tenant_id, iso(as_of), trigger))
    if existing:
        return {**unj(existing["stats_json"]), "run_id": existing["id"],
                "status": "success", "deduplicated": True}

    cfg = unj(tenant["config_json"])
    conn.execute("""INSERT INTO agent_runs (id, tenant_id, trigger, as_of, started_at, status, stats_json)
                    VALUES (?, ?, ?, ?, ?, 'running', '{}')""",
                 (run_id, tenant_id, trigger, iso(as_of), iso(datetime.now(timezone.utc))))
    conn.commit()

    stats: dict = {"as_of": iso(as_of), "trigger": trigger}
    seq = 0
    try:
        # 1. ingest -------------------------------------------------------
        seq += 1
        sid = _step(conn, run_id, seq, "ingest")
        ing = canonicalize(conn, tenant_id, as_of)
        stats["ingest"] = ing
        _finish_step(conn, sid, "success", ing)

        # 2. score --------------------------------------------------------
        seq += 1
        sid = _step(conn, run_id, seq, "score")
        sc = score_tenant(conn, tenant_id, as_of, run_id)
        stats["score"] = sc
        conn.execute("UPDATE agent_runs SET model_version=? WHERE id=?", (sc.get("model_version"), run_id))
        conn.commit()
        _finish_step(conn, sid, "success", sc)

        # 3. change detection --------------------------------------------
        seq += 1
        sid = _step(conn, run_id, seq, "detect_change")
        prev_run = one(conn, """SELECT id FROM agent_runs WHERE tenant_id=? AND status='success'
                                AND id != ? AND as_of < ? ORDER BY started_at DESC LIMIT 1""",
                       (tenant_id, run_id, iso(as_of)))
        prev = {}
        if prev_run:
            for r in rows(conn, "SELECT account_id, prob, band FROM scores WHERE tenant_id=? AND run_id=?",
                          (tenant_id, prev_run["id"])):
                prev[r["account_id"]] = (r["prob"], r["band"])
        current = rows(conn, "SELECT * FROM scores WHERE tenant_id=? AND run_id=? ORDER BY expected_arr_at_risk_cents DESC",
                       (tenant_id, run_id))
        transitions: dict[str, dict] = {}
        jump = float(cfg.get("risk_jump_threshold", 0.15))
        for r in current:
            p, b = prev.get(r["account_id"], (None, None))
            trig = None
            if p is None:
                trig = ("initial_review", "first scored run")
            elif BAND_RANK[r["band"]] > BAND_RANK.get(b, 0):
                trig = ("band_change", f"{b} → {r['band']}")
            elif r["prob"] - p >= jump:
                trig = ("risk_jump", f"+{r['prob']-p:.0%} since last run")
            elif r["band"] in ("red", "yellow") and r["days_to_renewal"] is not None and r["days_to_renewal"] <= 90:
                trig = ("renewal_window", f"renewal in {r['days_to_renewal']} days")
            if trig:
                transitions[r["account_id"]] = {"type": trig[0], "detail": trig[1], "score": dict(r)}
        stats["transitions"] = {t: sum(1 for v in transitions.values() if v["type"] == t)
                                for t in {v["type"] for v in transitions.values()}}
        _finish_step(conn, sid, "success", {"transition_count": len(transitions), **stats["transitions"]})

        # 4. route + policy + act ----------------------------------------
        seq += 1
        sid = _step(conn, run_id, seq, "route_actions")
        capacity = int(cfg.get("queue_capacity", 100))
        risky = [dict(r) for r in current if r["band"] in ("red", "yellow")]
        # the queue is bounded by CSM capacity, then transitions are unioned in
        selected = {r["account_id"]: r for r in risky[:capacity]}
        for aid, t in transitions.items():
            if aid not in selected and t["score"]["band"] in ("red", "yellow") and len(selected) < capacity * 2:
                selected[aid] = t["score"]

        # Routing looks at the whole feature row, not just the six reasons shown
        # in the drawer: a failed payment can be material to the play even when
        # it is not the largest contributor to the score.
        feature_values: dict[str, dict[str, float]] = {}
        for fr in rows(conn, """SELECT sf.score_id, sf.feature, sf.value FROM score_features sf
                                JOIN scores s ON s.id=sf.score_id
                                WHERE s.tenant_id=? AND s.run_id=?""", (tenant_id, run_id)):
            if fr["value"] is not None:
                feature_values.setdefault(fr["score_id"], {})[fr["feature"]] = float(fr["value"])

        action_stats = {"candidates": len(selected), "created": 0, "duplicates": 0, "blocked": 0,
                        "auto_sent": 0, "drafts": 0, "tasks": 0, "by_play": {}}
        for aid, sc_row in selected.items():
            t = transitions.get(aid, {"type": "queue_carryover", "detail": "ranked in top of book"})
            acct = one(conn, "SELECT * FROM dim_accounts WHERE tenant_id=? AND account_id=?", (tenant_id, aid))
            if not acct:
                continue
            acct = dict(acct)
            reasons = unj(sc_row["reason_json"], {}).get("reasons", [])
            play, why = choose_play(dict(sc_row), reasons, feature_values.get(sc_row["id"], {}))
            action_stats["by_play"][play.key] = action_stats["by_play"].get(play.key, 0) + 1

            week = f"{as_of.isocalendar().year}-W{as_of.isocalendar().week:02d}"
            # One action per account, play and ISO week. Trigger type and band are
            # recorded on the action, never in the key: a band change or a second
            # run later in the week must not create a second email.
            key = f"{aid}:{play.key}:{week}"
            existing = one(conn, "SELECT id FROM actions WHERE tenant_id=? AND idempotency_key=?",
                           (tenant_id, key))
            if existing:
                action_stats["duplicates"] += 1
                _note_retrigger(conn, tenant_id, existing["id"], run_id, t, sc_row, as_of)
                continue

            # One open action per account per cooldown, whatever the play. A later
            # run can legitimately route to a different play as features age; the
            # CSM should still see one thing to do, not two emails in a week.
            cooldown_days = int(cfg.get("per_account_cooldown_days", 7))
            recent = one(conn, """SELECT x.id FROM actions x JOIN agent_runs r ON r.id=x.run_id
                                  WHERE x.tenant_id=? AND x.account_id=? AND x.run_id != ?
                                    AND x.status IN ('proposed','approved','sent','completed')
                                    AND r.as_of >= ? AND r.as_of <= ?
                                  ORDER BY r.as_of DESC LIMIT 1""",
                         (tenant_id, aid, run_id, iso(as_of - timedelta(days=cooldown_days)), iso(as_of)))
            if recent:
                action_stats["duplicates"] += 1
                _note_retrigger(conn, tenant_id, recent["id"], run_id, t, sc_row, as_of)
                continue

            contact = policies.eligible_contact(conn, tenant_id, aid) if play.channel == "email" else None
            decision = policies.evaluate(conn, tenant_id, cfg, account=acct, score=dict(sc_row), play=play,
                                         contact=contact, channel=play.channel)
            if play.channel == "email" and contact:
                subject, body = draft_email(play, acct, contact, reasons, acct["owner_name"] or "Your CSM")
            elif play.channel == "task":
                subject = f"[{play.name}] {acct['name']}"
                body = task_description(play, acct, reasons, t["detail"])
            else:
                subject, body = f"{play.name} — {acct['name']}", ""

            status = "proposed"
            if decision["blocked_reason"]:
                status = "blocked"
            elif decision["auto_sent"]:
                status = "sent"

            action_id = _id("act", tenant_id, key)
            now_s = iso(datetime.now(timezone.utc))
            due = iso(as_of + timedelta(days=play.sla_days))
            conn.execute("""INSERT INTO actions (id, tenant_id, account_id, score_id, run_id, play_key, play_name,
                            channel, status, priority, due_at, assigned_to, trigger_reason, draft_subject, draft_body,
                            policy_json, idempotency_key, blocked_reason, created_at, updated_at, sent_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (action_id, tenant_id, aid, sc_row["id"], run_id, play.key, play.name, play.channel,
                          status, play.priority, due, acct["owner_email"], f"{t['detail']} · {why}",
                          subject, body, j({"decision": decision, "play_goal": play.goal,
                                            "score_prob": sc_row["prob"], "band": sc_row["band"],
                                            "policy_version": "v1"}),
                          key, decision["blocked_reason"], now_s, now_s,
                          now_s if status == "sent" else None))
            conn.execute("""INSERT INTO action_events (id, tenant_id, action_id, at, actor, event, detail_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                         (_id("aev", action_id, "created"), tenant_id, action_id, now_s,
                          "agent" if trigger != "manual" else "user", "created",
                          j({"status": status, "trigger": t["type"], "play": play.key,
                             "auto_sent": decision["auto_sent"]})))
            action_stats["created"] += 1
            if status == "blocked":
                action_stats["blocked"] += 1
            if play.channel == "email":
                action_stats["drafts"] += 1
            else:
                action_stats["tasks"] += 1

            if status == "sent" and contact:
                _deliver(conn, tenant_id, action_id, contact, subject, body, decision, actor="agent")
                action_stats["auto_sent"] += 1
            conn.commit()

        stats["actions"] = action_stats
        _finish_step(conn, sid, "success", action_stats)

        seq += 1
        sid = _step(conn, run_id, seq, "digest")
        digest = build_digest(conn, tenant_id, run_id)
        stats["digest"] = {"arr_at_risk_cents": digest["arr_at_risk_cents"],
                           "queue": int(digest["totals"].get("red", 0)) + int(digest["totals"].get("yellow", 0)),
                           "awaiting_approval": digest["awaiting_approval"]}
        _finish_step(conn, sid, "success", stats["digest"])

        conn.execute("UPDATE agent_runs SET status='success', finished_at=?, stats_json=? WHERE id=?",
                     (iso(datetime.now(timezone.utc)), j(stats), run_id))
        conn.commit()
        return {"run_id": run_id, "status": "success", **stats}
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        conn.rollback()
        conn.execute("UPDATE agent_runs SET status='failed', finished_at=?, error=?, stats_json=? WHERE id=?",
                     (iso(datetime.now(timezone.utc)), f"{type(exc).__name__}: {exc}",
                      j({**stats, "traceback": traceback.format_exc()[-2000:]}), run_id))
        conn.commit()
        raise


def _deliver(conn, tenant_id: str, action_id: str, contact: dict, subject: str, body: str,
             decision: dict, actor: str) -> str:
    now_s = iso(datetime.now(timezone.utc))
    outbox_id = _id("out", action_id, now_s)
    provider_id = "sim_" + hashlib.md5((action_id + now_s).encode()).hexdigest()[:16]
    conn.execute("""INSERT INTO outbox (id, tenant_id, action_id, account_id, to_email, subject, body, status,
                    provider, provider_message_id, sent_at, meta_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'sent', 'simulated', ?, ?, ?, ?)""",
                 (outbox_id, tenant_id, action_id,
                  scalar(conn, "SELECT account_id FROM actions WHERE id=?", (action_id,)),
                  contact["email"], subject, body, provider_id, now_s,
                  j({"policy_trace": decision["trace"], "contact_id": contact["contact_id"],
                     "consent_source": contact.get("consent_source")}), now_s))
    conn.execute("UPDATE actions SET status='sent', sent_at=?, updated_at=?, decided_by=?, decided_at=? WHERE id=?",
                 (now_s, now_s, actor, now_s, action_id))
    conn.execute("""INSERT INTO action_events (id, tenant_id, action_id, at, actor, event, detail_json)
                    VALUES (?, ?, ?, ?, ?, 'sent', ?)""",
                 (_id("aev", action_id, "sent", now_s), tenant_id, action_id, now_s, actor,
                  j({"provider_message_id": provider_id, "to": contact["email"]})))
    conn.execute("""INSERT INTO audit_log (id, tenant_id, at, actor, action, object_type, object_id, detail_json)
                    VALUES (?, ?, ?, ?, 'email_sent', 'action', ?, ?)""",
                 (_id("aud", action_id, now_s), tenant_id, now_s, actor, action_id,
                  j({"to": contact["email"], "auto": actor == "agent"})))
    return outbox_id


def approve_action(conn, tenant_id: str, action_id: str, actor: str) -> dict:
    a = one(conn, "SELECT * FROM actions WHERE tenant_id=? AND id=?", (tenant_id, action_id))
    if not a:
        raise ValueError("action not found")
    if a["status"] not in ("proposed", "approved"):
        raise ValueError(f"action is {a['status']}, cannot approve")
    tenant = one(conn, "SELECT config_json FROM tenants WHERE id=?", (tenant_id,))
    cfg = unj(tenant["config_json"])
    acct = dict(one(conn, "SELECT * FROM dim_accounts WHERE tenant_id=? AND account_id=?",
                    (tenant_id, a["account_id"])))
    score = dict(one(conn, "SELECT * FROM scores WHERE tenant_id=? AND id=?", (tenant_id, a["score_id"])))
    contact = policies.eligible_contact(conn, tenant_id, a["account_id"])
    from .plays import PLAYS
    play = PLAYS.get(a["play_key"])
    decision = policies.evaluate(conn, tenant_id, cfg, account=acct, score=score, play=play,
                                 contact=contact, channel=a["channel"])
    if not decision["allowed"]:
        conn.execute("UPDATE actions SET status='blocked', blocked_reason=?, updated_at=? WHERE id=?",
                     (decision["blocked_reason"], iso(datetime.now(timezone.utc)), action_id))
        conn.commit()
        raise ValueError(f"blocked by policy: {decision['blocked_reason']}")

    if a["channel"] == "task":
        conn.execute("""UPDATE actions SET status='approved', decided_by=?, decided_at=?, updated_at=?,
                        policy_json=? WHERE id=?""",
                     (actor, iso(datetime.now(timezone.utc)), iso(datetime.now(timezone.utc)),
                      j({"decision": decision, "approval": actor}), action_id))
        conn.execute("""INSERT INTO action_events (id, tenant_id, action_id, at, actor, event, detail_json)
                        VALUES (?, ?, ?, ?, ?, 'approved', ?)""",
                     (_id("aev", action_id, "approved", actor), tenant_id, action_id,
                      iso(datetime.now(timezone.utc)), actor, j({})))
        conn.commit()
        return {"status": "approved"}

    if not contact:
        raise ValueError("no consented contact")
    conn.execute("""INSERT INTO action_events (id, tenant_id, action_id, at, actor, event, detail_json)
                    VALUES (?, ?, ?, ?, ?, 'approved', ?)""",
                 (_id("aev", action_id, "approved", actor), tenant_id, action_id,
                  iso(datetime.now(timezone.utc)), actor, j({"channel": a["channel"]})))
    _deliver(conn, tenant_id, action_id, contact, a["draft_subject"], a["draft_body"], decision, actor=actor)
    conn.execute("""INSERT INTO audit_log (id, tenant_id, at, actor, action, object_type, object_id, detail_json)
                    VALUES (?, ?, ?, ?, 'email_approved', 'action', ?, ?)""",
                 (_id("aud", action_id, "approved"), tenant_id, iso(datetime.now(timezone.utc)), actor,
                  action_id, j({"decision": decision})))
    conn.commit()
    return {"status": "sent"}


def build_digest(conn, tenant_id: str, run_id: str | None = None) -> dict:
    if not run_id:
        r = one(conn, "SELECT id FROM agent_runs WHERE tenant_id=? AND status='success' ORDER BY started_at DESC LIMIT 1",
                (tenant_id,))
        run_id = r["id"] if r else None
    if not run_id:
        return {"accounts": [], "arr_at_risk_cents": 0, "awaiting_approval": 0, "totals": {}}
    scores = rows(conn, """SELECT s.*, a.name, a.segment, a.owner_name, a.owner_email, a.renewal_date, a.arr_cents
                           FROM scores s JOIN dim_accounts a ON a.tenant_id=s.tenant_id AND a.account_id=s.account_id
                           WHERE s.tenant_id=? AND s.run_id=?
                             AND json_extract(s.reason_json, '$.in_horizon') = 1
                           ORDER BY s.expected_arr_at_risk_cents DESC""",
                  (tenant_id, run_id))
    accounts = []
    for s in scores[:40]:
        reasons = unj(s["reason_json"], {}).get("reasons", [])
        accounts.append({
            "account_id": s["account_id"], "name": s["name"], "segment": s["segment"],
            "owner_name": s["owner_name"], "renewal_date": s["renewal_date"], "arr_cents": s["arr_cents"],
            "prob": round(s["prob"], 4), "band": s["band"],
            "expected_arr_at_risk_cents": s["expected_arr_at_risk_cents"],
            "coverage": round(s["coverage"], 2), "confidence": s["confidence"],
            "days_to_renewal": s["days_to_renewal"],
            "top_reason": next((r["label"] for r in reasons if not r.get("imputed")),
                               reasons[0]["label"] if reasons else None),
            "in_horizon": unj(s["reason_json"], {}).get("in_horizon", True),
        })
    total = sum(s["expected_arr_at_risk_cents"] for s in scores if s["band"] in ("red", "yellow"))
    counts = {b: sum(1 for s in scores if s["band"] == b) for b in ("red", "yellow", "green")}
    awaiting = scalar(conn, """SELECT COUNT(*) FROM actions WHERE tenant_id=? AND status='proposed'
                               AND channel='email'""", (tenant_id,)) or 0
    book = one(conn, """SELECT COUNT(*) n, COALESCE(SUM(arr_cents),0) arr FROM dim_accounts
                        WHERE tenant_id=? AND status='active'""", (tenant_id,)) or {"n": 0, "arr": 0}
    # "covered" means the account has enough source coverage to be actionable,
    # weighted by ARR. Denominator is the in-horizon book: the part we actually
    # put in front of a CSM.
    min_cov = float(unj(one(conn, "SELECT config_json FROM tenants WHERE id=?", (tenant_id,))["config_json"], {})
                    .get("min_coverage_for_outreach", 0.45))
    horizon_arr = sum(s["arr_cents"] for s in scores)
    horizon_accounts = len(scores)
    covered_arr = sum(s["arr_cents"] for s in scores if (s["coverage"] or 0) >= min_cov)
    run = one(conn, "SELECT stats_json FROM agent_runs WHERE id=?", (run_id,))
    return {"run_id": run_id, "accounts": accounts, "arr_at_risk_cents": total,
            "awaiting_approval": awaiting, "totals": counts,
            "book_arr_cents": int(book["arr"]), "active_accounts": int(book["n"]),
            "horizon_arr_cents": horizon_arr, "horizon_accounts": horizon_accounts,
            "coverage_pct": round(covered_arr / horizon_arr, 4) if horizon_arr else 0.0,
            "run_stats": unj(run["stats_json"]) if run else {}}
