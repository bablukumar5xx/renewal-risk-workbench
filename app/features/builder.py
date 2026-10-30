"""As-of feature computation.

`snapshot_features` is the single implementation used by training, backtesting
and live scoring. It takes the already-ingested fact set and a timestamp, and
filters on both `occurred_at <= as_of` and `arrived_at <= as_of`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from ..config import iso
from ..db import rows
from .registry import FEATURES, NEGATIVE_TERMS

NEG_RE = re.compile("|".join(re.escape(t) for t in NEGATIVE_TERMS), re.I)
CAP_DAYS = 400.0


def load_facts(conn, tenant_id: str, as_of: datetime) -> pd.DataFrame:
    df = pd.read_sql_query(
        """SELECT id, account_id, source, fact_type, occurred_at, arrived_at, amount_cents,
                  quantity, score, status, text, attrs_json
           FROM facts WHERE tenant_id = ? AND arrived_at <= ? AND occurred_at <= ?""",
        conn, params=(tenant_id, iso(as_of), iso(as_of)),
    )
    if df.empty:
        return df
    df["occurred_at"] = pd.to_datetime(df["occurred_at"], utc=True)
    df["arrived_at"] = pd.to_datetime(df["arrived_at"], utc=True)
    df["amount_cents"] = pd.to_numeric(df["amount_cents"], errors="coerce")
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    df["is_negative"] = df["text"].fillna("").str.contains(NEG_RE, regex=True)
    return df


def load_accounts(conn, tenant_id: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        """SELECT account_id, name, domain, segment, industry, region, arr_cents, seats,
                  owner_email, owner_name, renewal_date, contract_start, term_months, status,
                  churned_at, created_at
           FROM dim_accounts WHERE tenant_id = ?""", conn, params=(tenant_id,))
    for col in ("renewal_date", "contract_start", "churned_at", "created_at"):
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    return df


def _index(s: pd.Series, index: pd.Index) -> pd.Series:
    return s.reindex(index)


def _days_since(f: pd.DataFrame, mask, index: pd.Index, as_of: pd.Timestamp, cap: float = CAP_DAYS) -> pd.Series:
    sub = f[mask]
    if sub.empty:
        return pd.Series(np.nan, index=index)
    last = sub.groupby("account_id")["occurred_at"].max()
    return (as_of - last).dt.total_seconds().div(86400).clip(upper=cap).reindex(index)


def _count(f, mask, index) -> pd.Series:
    return f[mask].groupby("account_id").size().reindex(index)


def _sum(f, mask, col, index, log=False) -> pd.Series:
    s = f[mask].groupby("account_id")[col].sum(min_count=1).reindex(index)
    return np.log1p(s.clip(lower=0)) if log else s


def _mean(f, mask, col, index) -> pd.Series:
    return f[mask].groupby("account_id")[col].mean().reindex(index)


def _window(f: pd.DataFrame, types, days: int, age: pd.Series) -> pd.DataFrame:
    if isinstance(types, str):
        types = [types]
    return f[f["fact_type"].isin(types) & (age < days)]


def point_in_time_state(f: pd.DataFrame, accounts: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """ARR / seats / status as they were known at `as_of`, from facts only."""
    idx = pd.Index(accounts["account_id"], name="account_id")
    base = f[f["fact_type"] == "account"].sort_values("occurred_at").groupby("account_id").first()
    arr = base["amount_cents"].reindex(idx)
    seats = base["quantity"].reindex(idx)

    ren = f[(f["fact_type"] == "renewal") & (f["status"] != "open")].sort_values("occurred_at")
    if not ren.empty:
        last = ren.groupby("account_id").last()
        renewed = last["status"] != "churned"
        arr = last["amount_cents"].where(renewed, 0).reindex(idx).fillna(arr)
        seats = last["quantity"].where(renewed).reindex(idx).fillna(seats)

    sc = f[f["fact_type"] == "seat_change"].sort_values("occurred_at")
    if not sc.empty:
        seats = sc.groupby("account_id").last()["quantity"].reindex(idx).fillna(seats)

    out = accounts.set_index("account_id").loc[idx].copy()
    out["arr_cents"] = arr.fillna(out["arr_cents"])
    out["seats"] = seats.fillna(out["seats"])
    return out


def snapshot_features(f: pd.DataFrame, accounts: pd.DataFrame, as_of) -> pd.DataFrame:
    """Return one row per account with every feature, as of `as_of`."""
    as_of = pd.Timestamp(as_of)
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize("UTC")
    f = f[(f["occurred_at"] <= as_of) & (f["arrived_at"] <= as_of)] if not f.empty else f
    acct = point_in_time_state(f, accounts, as_of)
    created = pd.to_datetime(acct["created_at"], utc=True)
    acct = acct[created <= as_of]
    idx = acct.index

    if f.empty:
        out = pd.DataFrame(index=idx, columns=FEATURES, dtype=float)
        out["source_coverage"] = 0.0
        out["sources_180"] = 0.0
        return out

    f = f[f["account_id"].isin(idx)]
    age = (as_of - f["occurred_at"]).dt.total_seconds().div(86400)
    out = pd.DataFrame(index=idx)

    # commercial
    out["arr_k"] = acct["arr_cents"].astype(float) / 100_000.0
    out["seats"] = acct["seats"].astype(float)
    out["tenure_months"] = ((as_of - created).dt.days / 30.44).clip(lower=0)
    out["term_months"] = acct["term_months"].astype(float)
    out["segment_enterprise"] = (acct["segment"] == "Enterprise").astype(float)
    out["segment_midmarket"] = (acct["segment"] == "Mid-Market").astype(float)

    # usage
    u30 = _window(f, "usage", 30, age)
    u_prev = f[(f["fact_type"] == "usage") & (age >= 30) & (age < 60)]
    u90 = _window(f, "usage", 90, age)
    users30 = _mean(f, (f["fact_type"] == "usage") & (age < 30), "quantity", idx)
    usersprev = _mean(f, (f["fact_type"] == "usage") & (age >= 30) & (age < 60), "quantity", idx)
    users60_90 = _mean(f, (f["fact_type"] == "usage") & (age >= 30) & (age < 90), "quantity", idx)
    out["usage_users_30"] = users30
    out["usage_util_30"] = (users30 / acct["seats"].replace(0, np.nan)).clip(0, 1.5)
    util_prev = (usersprev / acct["seats"].replace(0, np.nan)).clip(0, 1.5)
    out["usage_util_delta_30"] = out["usage_util_30"] - util_prev
    out["usage_trend_90"] = (users30 / users60_90.replace(0, np.nan)).clip(0, 3)
    # sessions/api live inside attrs; parse once for the last 90 days
    if not u90.empty:
        parsed = u90["attrs_json"].map(json.loads)
        u90 = u90.assign(sessions=parsed.map(lambda d: d.get("sessions") or 0),
                         api_calls=parsed.map(lambda d: d.get("api_calls") or 0),
                         n_features=parsed.map(lambda d: len(d.get("features") or [])))
        out["usage_sessions_30"] = np.log1p(u90[u90["occurred_at"] >= as_of - pd.Timedelta(days=30)]
                                           .groupby("account_id")["sessions"].sum()).reindex(idx)
        out["usage_api_30"] = np.log1p(u90[u90["occurred_at"] >= as_of - pd.Timedelta(days=30)]
                                       .groupby("account_id")["api_calls"].sum()).reindex(idx)
        out["usage_breadth_90"] = u90.groupby("account_id")["n_features"].max().reindex(idx)
        means = u90.groupby("account_id")["quantity"].mean()
        stds = u90.groupby("account_id")["quantity"].std()
        out["usage_cv_90"] = (stds / means.replace(0, np.nan)).reindex(idx)
    else:
        out["usage_sessions_30"] = np.nan
        out["usage_api_30"] = np.nan
        out["usage_breadth_90"] = np.nan
        out["usage_cv_90"] = np.nan
    out["usage_weeks_90"] = _count(f, (f["fact_type"] == "usage") & (age < 90), idx)
    out["days_since_usage"] = _days_since(f, f["fact_type"] == "usage", idx, as_of)
    users180 = _mean(f, (f["fact_type"] == "usage") & (age < 180), "quantity", idx)
    out["usage_util_180"] = (users180 / acct["seats"].replace(0, np.nan)).clip(0, 1.5)
    out["usage_util_vs_baseline"] = out["usage_util_30"] - out["usage_util_180"]
    out["days_since_admin_seen"] = _days_since(f, f["fact_type"] == "admin_seen", idx, as_of)

    # support
    t = f["fact_type"].isin(["ticket"])
    out["tickets_90"] = np.log1p(_count(f, t & (age < 90), idx))
    out["tickets_urgent_90"] = _count(f, t & (age < 90) & f["attrs_json"].str.contains(
        '"priority": ?"(?:urgent|high)"', regex=True, na=False), idx)
    out["tickets_open_90"] = _count(f, t & (age < 90) & f["status"].isin(["open", "pending"]), idx)
    last45 = _count(f, t & (age < 45), idx)
    prev45 = _count(f, t & (age >= 45) & (age < 90), idx)
    out["ticket_trend_45"] = np.log((last45.fillna(0) + 1) / (prev45.fillna(0) + 1))
    neg = _count(f, t & (age < 90) & f["is_negative"], idx)
    out["ticket_negative_share_90"] = (neg / _count(f, t & (age < 90), idx).replace(0, np.nan)).clip(0, 1)
    solved = f["fact_type"] == "ticket_solved"
    bad = _count(f, solved & (age < 180) & (f["status"] == "bad"), idx)
    out["ticket_bad_csat_share_180"] = (bad / _count(f, solved & (age < 180), idx).replace(0, np.nan)).clip(0, 1)
    out["resolution_hours_90"] = _mean(f, solved & (age < 90), "score", idx)
    out["days_since_ticket"] = _days_since(f, t, idx, as_of)
    out["tickets_per_month_180"] = _count(f, t & (age < 180), idx) / 6.0

    # relationship
    ein = f["fact_type"] == "email_in"
    eout = f["fact_type"] == "email_out"
    out["email_in_90"] = _count(f, ein & (age < 90), idx)
    out["email_out_90"] = _count(f, eout & (age < 90), idx)
    in45 = _count(f, ein & (age < 45), idx)
    in_prev45 = _count(f, ein & (age >= 45) & (age < 90), idx)
    out["email_in_trend_45"] = np.log((in45.fillna(0) + 1) / (in_prev45.fillna(0) + 1))
    out["days_since_inbound"] = _days_since(f, ein, idx, as_of)

    engaged = f[(f["fact_type"].isin(["email_in", "email_out"])) & (age < 180)].copy()
    if not engaged.empty:
        parties = engaged["attrs_json"].map(lambda s: (json.loads(s).get("counterparty") or "").lower())
        engaged = engaged.assign(party=parties)
        out["engaged_contacts_180"] = engaged[engaged["party"] != ""].groupby("account_id")["party"].nunique().reindex(idx)
    else:
        out["engaged_contacts_180"] = np.nan
    out["single_threaded"] = (out["engaged_contacts_180"] <= 1).astype(float).where(
        out["engaged_contacts_180"].notna())
    out["email_reply_ratio_90"] = (out["email_in_90"] / (out["email_in_90"] + out["email_out_90"]).replace(0, np.nan)).clip(0, 1)

    # billing
    failed = f["fact_type"] == "payment_failed"
    out["failed_payments_180"] = _count(f, failed & (age < 180), idx)
    out["failed_amount_180"] = _sum(f, failed & (age < 180), "amount_cents", idx, log=True)
    out["days_since_failure"] = _days_since(f, failed, idx, as_of)
    sc = f[(f["fact_type"] == "seat_change") & (age < 180)].sort_values("occurred_at")
    if not sc.empty:
        change = sc.groupby("account_id").last()["quantity"] / acct["seats"].replace(0, np.nan) - 1
        out["seat_change_180"] = change.reindex(idx)
    else:
        out["seat_change_180"] = np.nan
    out["pm_expiring_90"] = (_count(f, (f["fact_type"] == "payment_method_expiring") & (age < 90), idx) > 0).astype(float)
    out.loc[_count(f, (f["fact_type"] == "payment_method_expiring") & (age < 90), idx).isna(), "pm_expiring_90"] = np.nan

    # coverage: distinct sources in 180d, and recency of any signal
    src = f[(age < 180) & f["account_id"].notna()].groupby("account_id")["source"].nunique().reindex(idx)
    out["sources_180"] = src.fillna(0)
    out["source_coverage"] = (out["sources_180"] / 5.0).clip(0, 1)
    out["days_since_any"] = _days_since(f, f["account_id"].notna(), idx, as_of)

    # Filled by the caller: (renewal_date - as_of) for serving, or the known
    # renewal date for a training row. Never derived from a future event.
    out["days_to_renewal"] = np.nan
    return out[FEATURES]


def build_panel(conn, tenant_id: str, snapshots: list[datetime], accounts=None) -> pd.DataFrame:
    """Feature rows for each month-end snapshot. Used for training/backtests."""
    if not snapshots:
        return pd.DataFrame()
    facts = load_facts(conn, tenant_id, max(snapshots))
    accts = accounts if accounts is not None else load_accounts(conn, tenant_id)
    frames = []
    for s in snapshots:
        ts = pd.Timestamp(s)
        f = facts[facts["arrived_at"] <= ts] if not facts.empty else facts
        df = snapshot_features(f, accts, ts)
        df["snapshot_at"] = ts
        df.index.name = "account_id"
        frames.append(df.reset_index())
    return pd.concat(frames, ignore_index=True)


def feature_hash(df_row: pd.Series) -> str:
    import hashlib

    payload = json.dumps({k: (None if pd.isna(df_row.get(k)) else round(float(df_row.get(k)), 6))
                          for k in FEATURES}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def recent_signals(conn, tenant_id: str, account_id: str, as_of: datetime, limit_per_type: int = 3) -> dict:
    """Evidence bundles for the account drawer, resolved from facts."""
    rws = rows(conn, """SELECT * FROM facts WHERE tenant_id=? AND account_id=? AND arrived_at <= ?
                        ORDER BY occurred_at DESC LIMIT 400""", (tenant_id, account_id, iso(as_of)))
    by_type: dict[str, list] = {}
    for r in rws:
        by_type.setdefault(r["fact_type"], []).append(r)
    out = {}
    for typ, items in by_type.items():
        out[typ] = [_evidence_item(r) for r in items[:limit_per_type]]
    return out


def _evidence_item(r) -> dict:
    attrs = json.loads(r["attrs_json"] or "{}")
    summary = {
        "payment_failed": lambda: f"Failed payment {attrs.get('invoice_id', '')} "
                                   f"({attrs.get('failure', 'declined')}, attempt {attrs.get('attempts', '?')})",
        "invoice": lambda: f"Invoice {attrs.get('invoice_id', '')} created",
        "payment_paid": lambda: f"Invoice {attrs.get('invoice_id', '')} paid",
        "ticket": lambda: f"{(attrs.get('priority') or 'no priority').title()} ticket: {attrs.get('subject') or r['text'][:80]}",
        "ticket_solved": lambda: f"Ticket closed — CSAT {r['status'] or 'none'}, {r['score'] or '?'}h",
        "ticket_comment": lambda: f"Support reply: {(r['text'] or '')[:90]}",
        "email_in": lambda: f"Customer email: {attrs.get('subject') or (r['text'] or '')[:80]}",
        "email_out": lambda: f"CSM email: {attrs.get('subject') or (r['text'] or '')[:80]}",
        "usage": lambda: f"{int(r['quantity'] or 0)} active users, {attrs.get('sessions', 0)} sessions",
        "feature_adopted": lambda: f"Adopted {attrs.get('feature')}",
        "seat_change": lambda: f"Seats {attrs.get('before')} → {attrs.get('after')}",
        "payment_method_expiring": lambda: f"Card ending {attrs.get('last4', '?')} expiring",
        "renewal": lambda: f"Renewal {r['status']}",
        "account": lambda: f"Account record: {r['text']}",
        "owner_change": lambda: "CSM owner changed",
        "admin_seen": lambda: "Admin session",
    }.get(r["fact_type"], lambda: r["fact_type"].replace("_", " "))
    return {
        "fact_id": r["id"],
        "raw_event_id": r["raw_event_id"],
        "source": r["source"],
        "fact_type": r["fact_type"],
        "occurred_at": r["occurred_at"],
        "arrived_at": r["arrived_at"],
        "summary": summary(),
        "value": r["amount_cents"] if r["amount_cents"] is not None else r["quantity"],
    }
