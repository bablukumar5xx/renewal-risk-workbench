"""Feature definitions. One registry drives the model, the UI and the docs.

Every feature is computed as-of a timestamp using only facts that had *arrived*
by then, so backtests and live scores use the same code path.
"""
from __future__ import annotations

FEATURES: list[str] = [
    # commercial
    "arr_k", "seats", "days_to_renewal", "tenure_months", "term_months",
    "segment_enterprise", "segment_midmarket",
    # product usage
    "usage_users_30", "usage_util_30", "usage_util_delta_30", "usage_trend_90",
    "usage_sessions_30", "usage_api_30", "usage_weeks_90", "usage_breadth_90",
    "usage_cv_90", "days_since_usage", "usage_util_180", "usage_util_vs_baseline",
    "days_since_admin_seen",
    # support
    "tickets_90", "tickets_urgent_90", "tickets_open_90", "ticket_trend_45",
    "ticket_negative_share_90", "ticket_bad_csat_share_180", "resolution_hours_90",
    "days_since_ticket", "tickets_per_month_180",
    # relationship
    "email_in_90", "email_out_90", "email_in_trend_45", "days_since_inbound",
    "engaged_contacts_180", "single_threaded", "email_reply_ratio_90",
    # billing
    "failed_payments_180", "failed_amount_180", "days_since_failure", "seat_change_180",
    "pm_expiring_90",
    # data quality / coverage
    "source_coverage", "sources_180", "days_since_any",
]

FEATURE_META: dict[str, dict] = {
    "arr_k": {"label": "ARR ($k)", "group": "Commercial", "unit": "$k"},
    "seats": {"label": "Licensed seats", "group": "Commercial", "unit": "seats"},
    "days_to_renewal": {"label": "Days to renewal", "group": "Commercial", "unit": "days"},
    "tenure_months": {"label": "Tenure", "group": "Commercial", "unit": "months"},
    "term_months": {"label": "Contract term", "group": "Commercial", "unit": "months"},
    "segment_enterprise": {"label": "Enterprise segment", "group": "Commercial", "unit": "0/1"},
    "segment_midmarket": {"label": "Mid-market segment", "group": "Commercial", "unit": "0/1"},
    "usage_users_30": {"label": "Active users (30d avg)", "group": "Product usage", "unit": "users"},
    "usage_util_30": {"label": "Seat utilisation", "group": "Product usage", "unit": "ratio"},
    "usage_util_delta_30": {"label": "Utilisation change (30d)", "group": "Product usage", "unit": "ratio"},
    "usage_trend_90": {"label": "Usage trend (30d vs prior 60d)", "group": "Product usage", "unit": "ratio"},
    "usage_sessions_30": {"label": "Sessions (30d, log)", "group": "Product usage", "unit": "log"},
    "usage_api_30": {"label": "API calls (30d, log)", "group": "Product usage", "unit": "log"},
    "usage_weeks_90": {"label": "Weeks with telemetry (90d)", "group": "Product usage", "unit": "weeks"},
    "usage_breadth_90": {"label": "Features adopted (90d)", "group": "Product usage", "unit": "count"},
    "usage_cv_90": {"label": "Usage volatility (90d)", "group": "Product usage", "unit": "CV"},
    "days_since_usage": {"label": "Days since usage", "group": "Product usage", "unit": "days"},
    "usage_util_180": {"label": "Seat utilisation (180d baseline)", "group": "Product usage", "unit": "ratio"},
    "usage_util_vs_baseline": {"label": "Utilisation vs own baseline", "group": "Product usage", "unit": "ratio"},
    "days_since_admin_seen": {"label": "Days since admin session", "group": "Product usage", "unit": "days"},
    "tickets_90": {"label": "Tickets (90d, log)", "group": "Support", "unit": "log"},
    "tickets_urgent_90": {"label": "P1/P2 tickets (90d)", "group": "Support", "unit": "count"},
    "tickets_open_90": {"label": "Unresolved tickets (90d)", "group": "Support", "unit": "count"},
    "ticket_trend_45": {"label": "Ticket trend (45d)", "group": "Support", "unit": "delta"},
    "ticket_negative_share_90": {"label": "Negative ticket share", "group": "Support", "unit": "share"},
    "ticket_bad_csat_share_180": {"label": "Bad CSAT share (180d)", "group": "Support", "unit": "share"},
    "resolution_hours_90": {"label": "Avg resolution time", "group": "Support", "unit": "hours"},
    "days_since_ticket": {"label": "Days since ticket", "group": "Support", "unit": "days"},
    "tickets_per_month_180": {"label": "Tickets per month (180d)", "group": "Support", "unit": "count"},
    "email_in_90": {"label": "Inbound emails (90d)", "group": "Relationship", "unit": "count"},
    "email_out_90": {"label": "Outbound emails (90d)", "group": "Relationship", "unit": "count"},
    "email_in_trend_45": {"label": "Inbound trend (45d)", "group": "Relationship", "unit": "ratio"},
    "days_since_inbound": {"label": "Days since customer reply", "group": "Relationship", "unit": "days"},
    "engaged_contacts_180": {"label": "Engaged contacts", "group": "Relationship", "unit": "people"},
    "single_threaded": {"label": "Single-threaded", "group": "Relationship", "unit": "0/1"},
    "email_reply_ratio_90": {"label": "Reply ratio (90d)", "group": "Relationship", "unit": "ratio"},
    "failed_payments_180": {"label": "Failed payments (180d)", "group": "Billing", "unit": "count"},
    "failed_amount_180": {"label": "Failed payment value (log)", "group": "Billing", "unit": "log"},
    "days_since_failure": {"label": "Days since payment failure", "group": "Billing", "unit": "days"},
    "seat_change_180": {"label": "Seat change (180d)", "group": "Billing", "unit": "ratio"},
    "pm_expiring_90": {"label": "Card expiring", "group": "Billing", "unit": "0/1"},
    "source_coverage": {"label": "Source coverage", "group": "Data quality", "unit": "share"},
    "sources_180": {"label": "Sources with data", "group": "Data quality", "unit": "count"},
    "days_since_any": {"label": "Days since any signal", "group": "Data quality", "unit": "days"},
}

# Human-readable explanation used in the account drawer.
FEATURE_WHY: dict[str, str] = {
    "usage_util_30": "fraction of licensed seats active in the last 30 days",
    "usage_util_delta_30": "change in seat utilisation vs the previous 30 days",
    "usage_trend_90": "recent usage relative to the prior two months",
    "usage_weeks_90": "weeks with product telemetry in the last 90 days (gaps are common)",
    "usage_breadth_90": "distinct features adopted in the last 90 days",
    "days_since_usage": "days since the last product telemetry",
    "tickets_urgent_90": "high and urgent Zendesk tickets in the last 90 days",
    "tickets_open_90": "tickets still open or pending from the last 90 days",
    "ticket_negative_share_90": "share of tickets whose text matches negative language",
    "ticket_bad_csat_share_180": "share of bad CSAT responses in the last 180 days",
    "email_in_trend_45": "customer replies in the last 45 days vs the prior 45",
    "days_since_inbound": "days since anyone at the customer replied to us",
    "single_threaded": "only one customer contact has engaged in 180 days",
    "failed_payments_180": "failed Stripe charges in the last 180 days",
    "days_since_failure": "days since the most recent failed charge",
    "seat_change_180": "net seat change from subscription updates",
    "source_coverage": "share of the five source systems with data in 180 days",
    "days_since_any": "days since any source produced a record",
    "usage_util_180": "seat utilisation averaged over 180 days — the account's own baseline",
    "usage_util_vs_baseline": "current utilisation compared with the account's 180-day baseline",
    "days_since_admin_seen": "days since an administrator last signed in",
    "tickets_per_month_180": "ticket volume normalised per month over 180 days",
    "email_reply_ratio_90": "customer replies as a share of all email in the last 90 days",
    "arr_k": "annual recurring revenue at the current contract value",
    "seats": "licensed seats on the current subscription",
    "days_to_renewal": "calendar days until the next renewal date",
    "tenure_months": "months since the account was first sold",
    "term_months": "length of the current contract term",
    "segment_enterprise": "account is in the enterprise segment",
    "segment_midmarket": "account is in the mid-market segment",
    "usage_users_30": "average daily active users in the last 30 days",
    "usage_sessions_30": "product sessions in the last 30 days (log-scaled)",
    "usage_api_30": "API calls in the last 30 days (log-scaled)",
    "usage_cv_90": "week-to-week volatility of usage over 90 days — erratic adoption is a risk signal",
    "tickets_90": "support tickets in the last 90 days (log-scaled)",
    "ticket_trend_45": "ticket volume in the last 45 days vs the prior 45",
    "resolution_hours_90": "average time to resolve tickets in the last 90 days",
    "days_since_ticket": "days since the last support ticket",
    "email_in_90": "customer-initiated emails in the last 90 days",
    "email_out_90": "CSM-initiated emails in the last 90 days",
    "engaged_contacts_180": "distinct customer contacts who engaged in the last 180 days",
    "failed_amount_180": "value of failed charges in the last 180 days (log-scaled)",
    "pm_expiring_90": "the card on file expires within 90 days",
    "sources_180": "number of the five source systems that produced data in 180 days",
}

# Deterministic lexicon used for ticket sentiment. An optional LLM tagger can
# enrich this (see app/ingest/enrich.py) but it is never required and never
# feeds the score directly.
NEGATIVE_TERMS = [
    "unusable", "blocking", "failing", "failed", "wrong", "frustrated", "escalate",
    "outage", "cancel", "refund", "500", "timeout", "broken", "urgent", "asap",
    "still waiting", "no response", "downgrade", "competitor", "churn",
]

WINDOWS = {"30d": 30, "45d": 45, "90d": 90, "180d": 180}
