"""Playbook. A score is only useful if it resolves to an agreed next action."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Play:
    key: str
    name: str
    channel: str              # task | email
    priority: int
    sla_days: int
    transactional: bool       # safe to auto-send if tenant policy allows
    goal: str


PLAYS: dict[str, Play] = {
    "billing_recovery": Play(
        "billing_recovery", "Billing recovery", "email", 1, 1, True,
        "Resolve the failed payment before it becomes a cancellation."),
    "support_escalation": Play(
        "support_escalation", "Escalation review + RCA", "task", 1, 1, False,
        "Own the open incident, share an RCA, and reset the relationship."),
    "executive_checkin": Play(
        "executive_checkin", "Multi-thread: executive check-in", "email", 1, 2, False,
        "Re-establish a second sponsor before renewal."),
    "adoption_rescue": Play(
        "adoption_rescue", "Adoption rescue", "email", 2, 3, False,
        "Diagnose the usage decline with the customer and agree a success plan."),
    "re_engagement": Play(
        "re_engagement", "Re-engagement", "email", 2, 3, False,
        "Restart the conversation after a long silence."),
    "renewal_prep": Play(
        "renewal_prep", "Renewal business review", "task", 2, 5, False,
        "Run the value review and surface commercial risk early."),
    "expansion_review": Play(
        "expansion_review", "Expansion review", "task", 3, 7, False,
        "Healthy, high-adoption account — look for a growth conversation."),
}


def choose_play(score: dict, reasons: list[dict], features: dict[str, float] | None = None) -> tuple[Play, str]:
    """Deterministic routing over the model's own reason codes.

    Ordering encodes what is urgent vs what is merely true. The chosen play is
    recorded with the score and can be overridden by the CSM.
    """
    rr = score.get("reason_json") or {}
    if isinstance(rr, str):
        import json
        try:
            rr = json.loads(rr)
        except Exception:
            rr = {}
    reasons = rr.get("reasons", reasons)
    feats = {r["feature"]: r for r in reasons}

    def val(name: str, default=0):
        if features and name in features:
            return features[name]
        return (feats.get(name) or {}).get("value", default) or default

    if val("failed_payments_180") >= 1:
        return PLAYS["billing_recovery"], "payment failure on file"

    if val("tickets_urgent_90") >= 2 or val("ticket_negative_share_90") >= 0.5:
        return PLAYS["support_escalation"], "support escalation pattern"

    if val("single_threaded") >= 1 or val("days_since_inbound") >= 45:
        return PLAYS["executive_checkin"], "single-threaded or gone quiet"

    if val("usage_trend_90", 1) < 0.75 or val("usage_util_delta_30", 0) < -0.12:
        return PLAYS["adoption_rescue"], "usage is declining"

    days = score.get("days_to_renewal")
    if days is not None and days <= 120:
        return PLAYS["renewal_prep"], f"renewal in {days} days"

    if val("email_in_90") == 0:
        return PLAYS["re_engagement"], "no customer replies in 90 days"

    if score.get("band") == "green" and val("usage_trend_90") > 1.15:
        return PLAYS["expansion_review"], "healthy expansion signal"

    return PLAYS["renewal_prep"], "renewal risk"


def draft_email(play: Play, account: dict, contact: dict, reasons: list[dict], csm_name: str) -> tuple[str, str]:
    """Template-based, evidence-linked copy. No LLM required.

    If an LLM is configured it may rephrase this after the fact, but the facts
    and the reason codes are always the deterministic output above.
    """
    first = (contact.get("name") or "there").split(" ")[0]
    product = "the platform"
    top = [r for r in reasons if not r.get("imputed")][:2]
    bullets = "".join(f"\n  • {r['label']}: {r['value_display']}" for r in top)
    evidence_line = ""
    for r in top:
        ev = (r.get("evidence") or [])
        if ev:
            evidence_line = f"\nI was looking at {ev[0]['summary'].lower()}, which is what prompted this note."
            break

    if play.key == "billing_recovery":
        subject = f"Action needed: payment method for {account['name']}"
        body = (f"Hi {first},\n\nA recent payment for {account['name']} didn't go through. "
                f"It's usually an expired card rather than anything deliberate, but I'd rather flag it now "
                f"than have it interrupt your team's access.{evidence_line}\n\n"
                f"Could you confirm the billing contact or update the card? I'm happy to walk through it.\n\n"
                f"Best,\n{csm_name}\nCustomer Success")
    elif play.key == "support_escalation":
        subject = f"{account['name']} — escalation follow-up"
        body = (f"Hi {first},\n\nI've been reviewing the support history on your account and want to make sure "
                f"we're owning the recent issues properly. I've asked our team for a written root-cause summary "
                f"and a fix timeline.{evidence_line}\n\n"
                f"Would 30 minutes this week work to walk through it together? I'd rather you hear it from us "
                f"directly.\n\nBest,\n{csm_name}\nCustomer Success")
    elif play.key == "adoption_rescue":
        subject = f"{account['name']} — getting more out of your rollout"
        body = (f"Hi {first},\n\nLooking at your usage over the last few months, adoption has softened "
                f"compared with earlier in the year:{bullets}\n\n"
                f"I'd like to understand what changed and whether a short working session with the team would help. "
                f"If the rollout has shifted priorities, that's useful for me to know too.{evidence_line}\n\n"
                f"Worth 20 minutes next week?\n\nBest,\n{csm_name}\nCustomer Success")
    elif play.key == "re_engagement":
        subject = f"Checking in — {account['name']}"
        body = (f"Hi {first},\n\nIt's been a while since we connected, and I don't want to assume silence means "
                f"everything is fine. How is the team finding things?{evidence_line}\n\n"
                f"If there's something that isn't working, I'd rather hear it now than at renewal.\n\n"
                f"Best,\n{csm_name}\nCustomer Success")
    elif play.key == "executive_checkin":
        subject = f"{account['name']} — who else should be in the room?"
        body = (f"Hi {first},\n\nMost of our recent conversations have been with you, and I'd like to make sure "
                f"we're not depending on a single point of contact. Who else on your side should be part of the "
                f"next review?{evidence_line}\n\n"
                f"Happy to send a short value summary they can read first.\n\nBest,\n{csm_name}\nCustomer Success")
    else:
        subject = f"{account['name']} — renewal business review"
        body = (f"Hi {first},\n\nYour renewal is coming up and I'd like to run a short business review before we "
                f"talk commercial terms — what's working, what isn't, and what you need next.{evidence_line}\n\n"
                f"30 minutes in the next two weeks?\n\nBest,\n{csm_name}\nCustomer Success")
    return subject, body


def task_description(play: Play, account: dict, reasons: list[dict], trigger: str) -> str:
    lines = [f"{play.goal}", f"Trigger: {trigger}."]
    lines.append("Model reasons:")
    for r in reasons[:4]:
        lines.append(f"  - {r['label']}: {r['value_display']} ({r['impact']})")
    lines.append(f"Account ARR: ${account['arr_cents']/100:,.0f}. Renewal: {account.get('renewal_date') or 'n/a'}.")
    return "\n".join(lines)
