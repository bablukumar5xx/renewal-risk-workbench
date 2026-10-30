# Data dictionary and lineage

The system keeps three layers on purpose: **raw** (what the source sent),
**facts** (what we normalised), and **derived** (renewals, scores, actions).
Nothing CSM-visible bypasses the middle layer.

```
raw_events ──1:1──▶ facts ──▶ renewals
   │                 │
   │                 └──▶ feature vector (as of run time) ──▶ scores ──▶ actions ──▶ outbox
   └── lineage endpoint: /api/lineage/raw/{raw_event_id}  ◀── /api/lineage/fact/{fact_id}
```

Every fact has a `raw_event_id`. Every score stores the exact feature vector
(`score_features`) and a `feature_hash`. Every action stores the score that
created it and the policy trace that allowed or gated it.

## Tenant and identity tables

### `tenants`
| Column | Meaning |
|---|---|
| `id` | Stable tenant ID (`tnt_northwind`, `tnt_lumen`) |
| `slug` | URL/login label |
| `api_key_hash` | SHA-256 of the bearer key; the plaintext key is never stored |
| `api_key_hint` | Last four characters, for support |
| `config_json` | Policy: auto-send, ARR ceiling, caps, cooldown, coverage minimum, kill switch, queue capacity |

### `dim_accounts`
One row per `(tenant_id, account_id)`. `renewal_date`, `arr_cents`, `seats`,
`term_months`, `owner_email`, `status` (`active`/`churned`), `churned_at`.
These are the denominator for the queue and the label source for training.

### `contacts`
`role` is `champion | exec_sponsor | admin | user | billing`. `consent_email`,
`consent_source`, and `opted_out_at` drive the hard consent block. The eligible
contact is chosen champion → exec sponsor → admin → other, primary first.

## Landing and canonical layer

### `raw_events`
Immutable. Payloads keep the source shape.

| Column | Meaning |
|---|---|
| `source` | `salesforce`, `stripe`, `zendesk`, `gmail`, `product` |
| `event_type` | Source event name, e.g. `invoice.payment_failed`, `ticket.created` |
| `natural_key` | Source-stable identity used to deduplicate re-sends |
| `account_external_id`, `account_name_raw`, `domain_raw` | Resolution hints as received |
| `occurred_at` | Event time in UTC |
| `arrived_at` | When it reached us; the point-in-time gate |
| `tz_assumed` | 1 when a naive timestamp had to be treated as UTC |
| `payload_json`, `batch_id`, `duplicate_of` | Raw body and ingestion provenance |

### `facts`
One row per normalised source observation. `fact_type` values:

| Source | Fact types |
|---|---|
| Salesforce | `account`, `owner_change`, `renewal` |
| Stripe | `invoice`, `payment_failed`, `payment_paid`, `payment_method_expiring`, `seat_change` |
| Zendesk | `ticket`, `ticket_comment`, `ticket_solved` |
| Gmail | `email_in`, `email_out` |
| Product | `usage`, `feature_adopted`, `admin_seen` |

`amount_cents`, `quantity`, `score`, `status`, `text`, and `attrs_json` carry
the source-specific payload. `occurred_at` and `arrived_at` are retained so
both event-time and knowledge-time questions can be answered.

### `renewals`
Materialised from Salesforce renewal opportunities, in event-time order.
`outcome` is `renewed | churned | downgraded | open`; `settled` is 0 until the
outcome is final. `source_fact_id` links back to the canonical fact. The model
uses only settled renewals at least 30 days old at training time.

### `processed_events`, `canonical_keys`
Consumption ledger. A raw event is canonicalised at most once, and a natural
key produces at most one fact. This is what makes re-running ingestion safe.

### `ingest_runs`, `ingest_issues`
Per-batch counts and quality defects: `orphan`, `unmatched`, `missing_field`,
`tz_assumed`, `duplicate`. The pipeline screen reads these directly; it does
not infer quality from row counts.

## Model and decision layer

### `model_versions`
Version, algorithm, training/calibration/test date ranges, feature list,
hyperparameters, full metrics JSON, artifact path, dataset hash, code version,
active flag, base rate. Exactly one active model per tenant.

### `scores`
One row per active account per scored run. `prob` is calibrated
`P(churn at next renewal)`; `band` is derived from the model artifact's
thresholds; `expected_arr_at_risk_cents = round(prob × arr_cents)`;
`coverage` is the share of the five sources seen in 180 days; `confidence` is a
presentation label (high/medium/low) derived from coverage and recency;
`reason_json` holds the reason codes plus `in_horizon` and the exact band
thresholds used.

### `score_features`
The full feature vector for each score: `value`, `imputed`, `contribution`,
`evidence_json`. `contribution` is the change in log-odds when that feature is
reset to the training median (occlusion attribution). It is approximate; it is
not a Shapley value and the UI says so.

### `agent_runs`, `agent_steps`
Run trigger (`schedule | manual | cli`), `as_of`, status, model version, and a
step-by-step record: `ingest → score → detect_change → route_actions → digest`.
Each step stores its own stats, so a failed run shows where it stopped.

### `actions`
The unit of work. `status` is `proposed | approved | rejected | sent | blocked |
completed`; `channel` is `task | email`. `trigger_reason` records the transition
and the play rationale. `policy_json` holds the entire policy decision and
trace. `idempotency_key` is `account:play:ISO-week`, so a later run cannot
duplicate outreach for the same play in the same week.

### `action_events`, `outbox`, `audit_log`
`action_events` is the append-only action history (`created`, `approved`,
`rejected`, `sent`, `retriggered`). `outbox` is the delivery record: one row
per send, with `provider` (`simulated` or `smtp`), the provider
`message_id`, consent source, and policy trace. The default transport is
simulated; set `CHP_EMAIL_TRANSPORT=smtp` and the `CHP_SMTP_*` variables to
deliver through a relay. `audit_log` records security-relevant events
(`email_sent`, `email_approved`).

## Identity resolution

Resolve in this order, recording which rule fired:

1. **Domain** — exact normalised domain against `dim_accounts.domain`
   (also accepts a domain found inside an email).
2. **Contact email** — exact email against `contacts.email`.
3. **Name** — normalised exact match, then fuzzy similarity ≥ 0.93.

Unresolved events become `unmatched` (or `orphan` when no hints at all) and
are counted in `ingest_issues`; they are not silently attached to a guessed
account.

## Time semantics

- `occurred_at` is when the thing happened in the customer's world.
- `arrived_at` is when our pipeline learned about it.
- A run at `as_of` reads only rows satisfying **both** `occurred_at <= as_of`
  and `arrived_at <= as_of`.
- A raw event whose `occurred_at` is in the past but whose `arrived_at` is after
  `as_of` is *held back*, not backfilled into history. The next run picks it up.

This is why the Thursday demo run can ingest late data without changing what
the Monday run knew.
