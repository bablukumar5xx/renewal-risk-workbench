# Decisions and tradeoffs

This is the short list of choices that shape the system. Each one names the
alternative I rejected and why. The point is to make the design arguable rather
than inevitable.

## 1. Build a workbench, not an auto-emailer

**Decision:** the product produces a ranked queue, a reason code with evidence,
a recommended play, and a draft. Customer-facing email is approval-gated by
default.

**Rejected:** "if red, email the customer." A risk score is a statement about
our uncertainty, not a fact about the customer. Sending it back to them at the
moment they are most fragile is a theory-free intervention, leaks the churn
model, and creates consent/legal exposure. It also optimises for activity
rather than renewals.

**Consequence:** the agent automates the internal work (ingest, score, detect
change, route, draft, brief) and leaves the judgement call with the CSM. The
demo tenant `tnt_lumen` turns on auto-send only for transactional plays under
guardrails, so the policy path is exercised without making auto-send the
default.

## 2. Hard blocks are narrow; everything else forces a human

**Decision:** only three things stop a send outright: the tenant kill switch,
missing email consent, and source coverage below the tenant minimum. ARR
ceiling, cooldown, daily/monthly caps, open P1 incidents, and non-transactional
plays all force approval — a CSM can still approve the send.

**Rejected:** treating every guardrail as a hard block. That makes the system
silently refuse work a human has explicitly authorised, and trains CSMs to
distrust it. It is also wrong operationally: an open P1 is a reason to talk to
the customer, not a reason the tool should forbid it.

**Consequence:** every policy decision carries a rule-by-rule trace in
`actions.policy_json` and `outbox.meta_json.policy_trace`, so "why did this
send / not send" is a query, not a meeting.

## 3. Gradient-boosted trees with isotonic calibration, not an LLM

**Decision:** `HistGradientBoostingClassifier` predicts churn at the next
renewal; an isotonic calibrator maps raw scores to observed frequencies. The
LLM is not in the scoring path.

**Rejected:** an LLM reading account notes and assigning a risk score. It is
not reproducible, cannot be calibrated, cannot be validated temporally, and
changes when the model provider changes. It is also unnecessary: the signal is
in event counts and trends.

**Consequence:** every score is reproducible from a versioned artifact plus a
feature vector. Explanations are occlusion attribution (reset one feature to
the training median, measure the change in log-odds), which is approximate and
labelled as such in the UI, not Shapley values.

## 4. One row per (renewal, monthly snapshot), split by renewal date

**Decision:** training rows are monthly snapshots 30–330 days before a settled
renewal. The train/calibration/test split is by renewal date, never by row.

**Rejected:** splitting by row or by account. Snapshots of the same renewal
would straddle the split boundary and leak the outcome. A random split would
also flatter the model by letting it see the same contract at several ages.

**Consequence:** the evaluation is a genuine forward-in-time test. The split
dates and row counts are stored in `model_versions.metrics_json.split`.

## 5. Point-in-time is enforced in SQL, not in a convention

**Decision:** every serving and training query filters both
`occurred_at <= as_of` and `arrived_at <= as_of`. Facts are canonicalised only
when their raw event has arrived by the run's cut-off. Events that occurred in
the past but arrived after the cut-off are held back until a later run.

**Rejected:** using event time alone, or "latest record wins" merges. Both leak
information that was not available at decision time and make backtests look
better than the live system.

**Consequence:** late-arriving data is a first-class state, visible as
`held_back` in the run stats and ingested by the next run. The generator
deliberately produces out-of-order and duplicate events to keep this honest.

## 6. SQLite for the demo, with a real migration path

**Decision:** one SQLite database in WAL mode, one connection per thread.

**Rejected:** Postgres/ClickHouse for v1. The brief is a working system on a
laptop, and SQLite makes the demo reproducible with no service to install. The
schema still uses explicit tenant keys and set-based queries, so the port is
mechanical.

**What breaks:** write concurrency and full-table recompute. See the 10×
section in the README. The first things to move are feature aggregation and
scoring, not the API.

## 7. Multi-tenancy by bearer key, scoped in every query

**Decision:** the API key is hashed and resolved to exactly one tenant. Tenant
identity comes from the token, never from a URL or request body. Every
tenant-owned table carries `tenant_id`, and every read path filters on it.

**Rejected:** a shared schema with row-level security in application code
only. SQLite has no RLS, so the filter is explicit and tested: an account ID
that exists in two tenants returns each tenant's own row, and a fact/raw ID
from another tenant is a 404.

**Consequence:** `tests/test_tenant_isolation.py` seeds the same account ID in
two tenants and asserts the boundary. Demo keys are listed on the login screen
and in the README; production would use SSO/API-key exchange.

## 8. Edge-triggered actions, one per account per cooldown

**Decision:** the agent acts on band changes, risk jumps, renewal windows, or
first score. Actions are keyed by `(tenant, account, play, ISO week)`, and a
later run in the same cooldown cannot create a second action for an account
that already has an open one.

**Rejected:** daily "still red" reminders. They create noise, train CSMs to
ignore the queue, and make email frequency impossible to govern.

**Consequence:** a stable red account is scored every night but not re-actioned
until the cooldown expires or something changes. A re-run of the exact same
`(tenant, as_of, trigger)` is a no-op that returns the original run.

## 9. The demo world is frozen

**Decision:** `AS_OF = 2026-10-26T06:00:00Z`, with a deterministic generator
seed. Initial arrivals are capped at `AS_OF`; a late pool arrives at
`AS_OF + 2 days`; no event arrives from the future.

**Rejected:** generating against the wall clock. The demo would drift, the
metrics would move, and screenshots would stop matching the README.

**Consequence:** the "Thursday" run is a real second run over the same world
with late data, not a re-labelled first run. Rebuilding is one command and
produces the same numbers.

## 10. Templates for drafts, not an LLM

**Decision:** draft subject/body come from deterministic templates selected by
play, with the top evidence inserted. An LLM can rephrase later, but it is not
required and its output is not the source of truth.

**Rejected:** LLM-generated outreach in the critical path. It adds latency,
cost, non-determinism, and a new failure mode (hallucinated facts) without
improving the decision. The reason codes and evidence are the product; the
prose is commodity.

## 11. Deliberate skips

- **No causal uplift model.** The value estimate is a stated assumption
  (12% relative save lift, $400 intervention cost), not a measured effect.
- **No expansion scoring in v1.** The model predicts churn, not expansion;
  `expansion_review` is a routing rule for healthy accounts, not a model.
- **No meeting notes, call transcripts, or sentiment model beyond a
  deterministic lexicon.** These are high-cost, low-trust inputs for v1.
- **No per-tenant model retraining UI or drift monitor.** Retraining is a CLI
  operation; drift is reported, not automated.
- **No ESP integration beyond SMTP.** Sends default to a side-effect-free
  simulated outbox; `CHP_EMAIL_TRANSPORT=smtp` delivers through a relay, but
  there is no bounce/complaint webhook or provider-side unsubscribe handling.
- **No live screen recording.** The committed walkthrough MP4 is a narrated
  slideshow assembled from the live screenshots, the deck, and macOS
  text-to-speech.
