"""Score every active account as of a moment, with per-feature attribution.

Attribution is occlusion: reset one feature to the training median and measure
the change in model log-odds. It is model-agnostic, cheap to batch, and — unlike
a story generated after the fact — every number is reproducible from the
artifact. It is approximate, not Shapley; we say so in the UI.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from ..config import iso
from ..db import insert_many, j, tx
from ..features.builder import feature_hash, load_accounts, load_facts, recent_signals, snapshot_features
from ..features.registry import FEATURE_META, FEATURE_WHY, FEATURES
from .train import load_active_model

EVIDENCE_TYPES = {
    "usage": ["usage", "feature_adopted", "admin_seen"],
    "tickets": ["ticket", "ticket_solved", "ticket_comment"],
    "email": ["email_in", "email_out"],
    "billing": ["payment_failed", "invoice", "payment_paid", "seat_change", "payment_method_expiring"],
    "commercial": ["renewal", "account", "owner_change", "seat_change"],
    "quality": ["usage", "ticket", "email_in", "payment_paid", "account"],
}


def _logit(p):
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def _feature_group(feature: str) -> str:
    if feature.startswith(("usage_", "days_since_usage")):
        return "usage"
    if feature.startswith(("ticket", "resolution", "days_since_ticket")):
        return "tickets"
    if feature.startswith(("email_", "days_since_inbound", "engaged", "single_threaded")):
        return "email"
    if feature.startswith(("failed_", "days_since_failure", "seat_change", "pm_expiring")):
        return "billing"
    if feature.startswith(("source_", "sources_", "days_since_any")):
        return "quality"
    return "commercial"


def _fmt_value(feature: str, v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "no data"
    meta = FEATURE_META.get(feature, {})
    unit = meta.get("unit", "")
    if v == int(v) and abs(v) < 10000 and unit in ("count", "seats", "days", "months", "weeks", "people", "0/1"):
        return f"{int(v)}"
    if unit in ("ratio", "share"):
        return f"{v:.2f}"
    if unit == "$k":
        return f"${v:,.0f}k"
    if unit == "log":
        return f"{v:.2f}"
    return f"{v:.2f}"


def score_tenant(conn, tenant_id: str, as_of: datetime, run_id: str) -> dict:
    model_row, art = load_active_model(conn, tenant_id)
    accounts = load_accounts(conn, tenant_id)
    active = accounts[accounts["status"] == "active"].copy()
    if active.empty:
        return {"scored": 0}
    facts = load_facts(conn, tenant_id, as_of)
    feats = snapshot_features(facts, accounts, as_of).reindex(pd.Index(active["account_id"], name="account_id"))

    acct = active.set_index("account_id")
    renewal = acct["renewal_date"]
    days = (renewal - pd.Timestamp(as_of)).dt.total_seconds().div(86400).round()
    feats["days_to_renewal"] = days.reindex(feats.index).clip(lower=-30, upper=330)

    X = feats[FEATURES].astype(float)
    model = art["model"]
    calibrator = art["calibrator"]
    p = calibrator.predict(model.predict_proba(X)[:, 1])
    p = np.clip(p, 0.0005, 0.995)

    thresholds = art["thresholds"]
    band = np.where(p >= thresholds["red"], "red", np.where(p >= thresholds["yellow"], "yellow", "green"))
    arr = acct["arr_cents"].reindex(feats.index).fillna(0).astype(float)
    expectation = (p * arr).round().astype(int).to_numpy()

    coverage = feats["source_coverage"].fillna(0).to_numpy()
    stale = feats["days_since_any"].fillna(999).to_numpy()
    confidence = np.where((coverage >= 0.8) & (stale <= 21), "high",
                          np.where(coverage >= 0.5, "medium", "low"))
    in_horizon = (days.reindex(feats.index) <= 330).to_numpy()

    # --- occlusion attribution, batched per feature ---------------------
    med = pd.Series(art["medians"]).reindex(FEATURES).astype(float)
    base_row = pd.DataFrame([med.to_numpy()], columns=FEATURES)
    p_base = float(calibrator.predict(model.predict_proba(base_row)[:, 1])[0])
    base_logit = float(_logit(p_base))
    contrib = pd.DataFrame(index=X.index, columns=FEATURES, dtype=float)
    for f in FEATURES:
        Xf = X.copy()
        Xf[f] = med[f]
        pf = calibrator.predict(model.predict_proba(Xf)[:, 1])
        contrib[f] = _logit(p) - _logit(pf)

    # evidence is expensive per account; attach it to the accounts a CSM will
    # actually see, and compute it lazily for the rest on drill-down.
    actionable = set(pd.Index(feats.index)[(band != "green") | (expectation > np.quantile(expectation, 0.9))])
    evidence: dict[str, dict] = {}
    for aid in actionable:
        try:
            evidence[aid] = recent_signals(conn, tenant_id, aid, as_of)
        except Exception:
            evidence[aid] = {}

    score_rows = []
    feature_rows = []
    reason_by_account: dict[str, list] = {}
    created = iso(datetime.now(timezone.utc))
    for i, aid in enumerate(feats.index):
        row = {
            "feature": {
                "usage_trend_90": "Usage trend", "usage_util_delta_30": "Seat utilisation change",
                "usage_util_30": "Seat utilisation", "failed_payments_180": "Failed payments",
                "days_since_inbound": "Customer silence", "tickets_urgent_90": "Urgent tickets",
                "ticket_negative_share_90": "Negative ticket tone", "single_threaded": "Single-threaded",
                "seat_change_180": "Seat reduction", "arr_k": "Contract size",
                "days_to_renewal": "Renewal proximity", "usage_weeks_90": "Telemetry gaps",
                "ticket_bad_csat_share_180": "Bad CSAT", "email_in_trend_45": "Reply trend",
            }
        }
        srow = contrib.loc[aid].dropna()
        if srow.empty:
            top = []
        else:
            top = list(srow.abs().sort_values(ascending=False).head(8).index)
        reasons = []
        for f in top:
            v = X.loc[aid, f]
            c = float(contrib.loc[aid, f])
            if abs(c) < 0.02:
                continue
            imputed = bool(pd.isna(v))
            group = _feature_group(f)
            ev = (evidence.get(aid, {}).get(k) for k in EVIDENCE_TYPES[group]) if aid in evidence else []
            ev_flat = [x for lst in ev if lst for x in lst][:3]
            reasons.append({
                "feature": f,
                "label": FEATURE_META.get(f, {}).get("label", f),
                "group": FEATURE_META.get(f, {}).get("group", ""),
                "contribution": round(c, 3),
                "impact": "increases risk" if c > 0 else "lowers risk",
                "value": None if pd.isna(v) else round(float(v), 4),
                "value_display": _fmt_value(f, v),
                "imputed": imputed,
                "why": FEATURE_WHY.get(f, ""),
                "evidence": [] if imputed else ev_flat,
            })
        reasons = reasons[:6]
        reason_by_account[aid] = reasons

        score_id = f"sc_{run_id}_{aid}"
        score_rows.append((
            score_id, tenant_id, run_id, aid, iso(as_of), model_row["id"], float(p[i]), str(band[i]),
            int(expectation[i]), float(coverage[i]), str(confidence[i]),
            int(days.reindex(feats.index)[aid]) if not pd.isna(days.reindex(feats.index)[aid]) else None,
            j({"reasons": reasons, "in_horizon": bool(in_horizon[i]),
               "band_thresholds": thresholds}), feature_hash(X.loc[aid]), created,
        ))
        for f in FEATURES:
            v = X.loc[aid, f]
            feature_rows.append((
                score_id, f, None if pd.isna(v) else float(v), int(pd.isna(v)),
                float(contrib.loc[aid, f]), "[]",
            ))

    with tx(conn):
        insert_many(conn, "scores", [
            "id", "tenant_id", "run_id", "account_id", "as_of", "model_version", "prob", "band",
            "expected_arr_at_risk_cents", "coverage", "confidence", "days_to_renewal", "reason_json",
            "feature_hash", "created_at",
        ], score_rows)
        insert_many(conn, "score_features", [
            "score_id", "feature", "value", "imputed", "contribution", "evidence_json"], feature_rows)

    return {
        "scored": len(score_rows), "red": int((band == "red").sum()), "yellow": int((band == "yellow").sum()),
        "green": int((band == "green").sum()), "model_version": model_row["id"],
        "expected_arr_at_risk_cents": int(expectation.sum()),
        "outside_horizon": int((~in_horizon).sum()),
    }
