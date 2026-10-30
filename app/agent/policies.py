"""Guardrails. Policy is code, evaluated at send time, and every rule is logged.

The default posture is: automate the internal work, gate the external message.
Auto-send exists only for transactional plays, and only when every rule passes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..config import iso
from ..db import one, scalar


def evaluate(conn, tenant_id: str, cfg: dict, *, account: dict, score: dict, play,
             contact: dict | None, channel: str) -> dict:
    trace: list[dict] = []
    now = datetime.now(timezone.utc)
    now_s = iso(now)
    hard_block = None
    auto_ok = True

    def rule(name: str, ok: bool, detail: str, blocking: bool = True) -> bool:
        """blocking=True means the message cannot be sent at all.
        blocking=False is an auto-send guardrail: failing it only forces a human
        decision, it does not stop a CSM from approving the send."""
        nonlocal hard_block, auto_ok
        trace.append({"rule": name, "ok": bool(ok), "detail": detail, "blocking": blocking})
        if not ok:
            if blocking and hard_block is None:
                hard_block = detail
            else:
                auto_ok = False
        return ok

    if channel == "task":
        # internal work is safe; only the kill switch stops it
        rule("kill_switch", not cfg.get("kill_switch"), "tenant kill switch is off")
        return {
            "allowed": hard_block is None, "requires_approval": False, "blocked_reason": hard_block,
            "trace": trace, "auto_sent": False,
        }

    rule("kill_switch", not cfg.get("kill_switch"), "tenant kill switch is off")
    rule("score_coverage", (score.get("coverage") or 0) >= cfg.get("min_coverage_for_outreach", 0.45),
         f"source coverage {(score.get('coverage') or 0):.0%} vs minimum "
         f"{cfg.get('min_coverage_for_outreach', 0.45):.0%}")
    rule("consent", bool(contact) and bool(contact.get("consent_email")) and not contact.get("opted_out_at"),
         "contact has email consent" if contact else "no eligible contact found")

    open_p1 = scalar(conn, """SELECT COUNT(*) FROM facts WHERE tenant_id=? AND account_id=?
                              AND fact_type='ticket' AND status IN ('open','pending')
                              AND attrs_json LIKE '%"priority":"urgent"%'
                              AND occurred_at >= ?""",
                     (tenant_id, account["account_id"], iso(now - timedelta(days=30)))) or 0
    rule("open_p1_incident", open_p1 == 0, f"{open_p1} open urgent ticket(s) in last 30 days", blocking=False)

    cooldown = int(cfg.get("per_account_cooldown_days", 7))
    recent = scalar(conn, """SELECT MAX(sent_at) FROM outbox WHERE tenant_id=? AND account_id=? AND status='sent'""",
                    (tenant_id, account["account_id"]))
    if recent:
        gap = (now - datetime.fromisoformat(recent.replace("Z", "+00:00"))).days
        rule("cooldown", gap >= cooldown, f"last send {gap}d ago (cooldown {cooldown}d)", blocking=False)
    else:
        rule("cooldown", True, "no previous send", blocking=False)

    month_cap = int(cfg.get("per_account_monthly_cap", 3))
    sent_30 = scalar(conn, """SELECT COUNT(*) FROM outbox WHERE tenant_id=? AND account_id=? AND status='sent'
                              AND sent_at >= ?""",
                     (tenant_id, account["account_id"], iso(now - timedelta(days=30)))) or 0
    rule("per_account_monthly_cap", sent_30 < month_cap, f"{sent_30}/{month_cap} messages in 30 days", blocking=False)

    daily_cap = int(cfg.get("daily_send_cap", 25))
    sent_today = scalar(conn, """SELECT COUNT(*) FROM outbox WHERE tenant_id=? AND status='sent' AND sent_at >= ?""",
                        (tenant_id, now_s[:10] + "T00:00:00Z")) or 0
    rule("daily_cap", sent_today < daily_cap, f"{sent_today}/{daily_cap} sends today", blocking=False)

    under_arr = int(account["arr_cents"]) <= int(cfg.get("auto_send_max_arr_cents", 0))
    rule("arr_ceiling", under_arr, f"ARR ${account['arr_cents']/100:,.0f} vs auto-send ceiling "
                                   f"${cfg.get('auto_send_max_arr_cents', 0)/100:,.0f}", blocking=False)

    auto_possible = bool(cfg.get("auto_send_enabled")) and bool(play.transactional) and under_arr
    rule("auto_send_policy", auto_possible,
         "tenant allows auto-send for transactional plays" if cfg.get("auto_send_enabled")
         else "tenant auto-send is disabled; human approval required", blocking=False)

    requires_approval = hard_block is not None or not auto_ok or not auto_possible
    return {
        "allowed": hard_block is None,
        "requires_approval": requires_approval,
        "blocked_reason": hard_block,
        "trace": trace,
        "auto_sent": hard_block is None and auto_ok and auto_possible,
    }


def eligible_contact(conn, tenant_id: str, account_id: str) -> dict | None:
    rows_ = conn.execute("""SELECT * FROM contacts WHERE tenant_id=? AND account_id=?
                            AND active=1 AND consent_email=1 AND opted_out_at IS NULL
                            ORDER BY CASE role WHEN 'champion' THEN 0 WHEN 'exec_sponsor' THEN 1
                                     WHEN 'admin' THEN 2 ELSE 3 END, is_primary DESC LIMIT 1""",
                         (tenant_id, account_id)).fetchall()
    return dict(rows_[0]) if rows_ else None
