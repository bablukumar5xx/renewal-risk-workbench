"""Policy is code: hard blocks vs auto-send-only guardrails."""
from __future__ import annotations

from app.agent import policies
from app.agent.plays import PLAYS


def _inputs(arr_cents=200_000):
    return {
        "account": {"account_id": "acc_policy_probe", "arr_cents": arr_cents},
        "score": {"coverage": 1.0, "prob": 0.6, "band": "red", "days_to_renewal": 60},
        "contact": {"consent_email": 1, "opted_out_at": None, "email": "x@example.com"},
    }


def _cfg(auto=False, ceiling=5_000_000, kill=False):
    return {
        "auto_send_enabled": auto,
        "auto_send_max_arr_cents": ceiling,
        "min_coverage_for_outreach": 0.45,
        "per_account_cooldown_days": 7,
        "per_account_monthly_cap": 3,
        "daily_send_cap": 25,
        "kill_switch": kill,
    }


def test_default_is_approval_not_auto_send(world):
    d = policies.evaluate(world["conn"], "tnt_test", _cfg(auto=False),
                          play=PLAYS["billing_recovery"], channel="email", **_inputs())
    assert d["allowed"] is True
    assert d["requires_approval"] is True
    assert d["auto_sent"] is False
    assert d["blocked_reason"] is None


def test_arr_ceiling_forces_approval_but_does_not_block_a_human(world):
    d = policies.evaluate(world["conn"], "tnt_test", _cfg(auto=True, ceiling=50_000),
                          play=PLAYS["billing_recovery"], channel="email", **_inputs(arr_cents=900_000))
    assert d["allowed"] is True
    assert d["requires_approval"] is True
    assert d["auto_sent"] is False
    assert any(r["rule"] == "arr_ceiling" and not r["ok"] for r in d["trace"])


def test_allowlisted_transactional_play_can_auto_send(world):
    d = policies.evaluate(world["conn"], "tnt_test", _cfg(auto=True),
                          play=PLAYS["billing_recovery"], channel="email", **_inputs())
    assert d["allowed"] is True
    assert d["auto_sent"] is True
    assert d["requires_approval"] is False


def test_non_transactional_play_always_needs_a_human(world):
    d = policies.evaluate(world["conn"], "tnt_test", _cfg(auto=True),
                          play=PLAYS["re_engagement"], channel="email", **_inputs())
    assert d["allowed"] is True
    assert d["requires_approval"] is True
    assert d["auto_sent"] is False


def test_missing_consent_is_a_hard_block(world):
    d = policies.evaluate(world["conn"], "tnt_test", _cfg(auto=False),
                          play=PLAYS["billing_recovery"], channel="email",
                          account=_inputs()["account"], score=_inputs()["score"],
                          contact=None)
    assert d["allowed"] is False
    assert d["blocked_reason"]


def test_kill_switch_stops_everything(world):
    d = policies.evaluate(world["conn"], "tnt_test", _cfg(auto=True, kill=True),
                          play=PLAYS["billing_recovery"], channel="email", **_inputs())
    assert d["allowed"] is False
    assert d["blocked_reason"]


def test_internal_tasks_are_never_gated_by_outreach_rules(world):
    d = policies.evaluate(world["conn"], "tnt_test", _cfg(auto=True, ceiling=0),
                          play=PLAYS["renewal_prep"], channel="task", **_inputs(arr_cents=900_000))
    assert d["allowed"] is True
    assert d["requires_approval"] is False
