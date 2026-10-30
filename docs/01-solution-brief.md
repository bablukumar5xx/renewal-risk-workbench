# Renewal Risk Workbench — solution brief

**Prepared for:** VP Customer Success · **Author:** CS Platform engineering · **Status:** build proposal (v1 scope)

---

## The problem is not a missing dashboard

CSMs do not lack signals. They lack a **finite, ranked, explainable list of what to do next**, produced from signals they cannot triangulate by hand across 120 accounts and five systems. A red/yellow/green column adds a sixth thing to read on Monday. The two lost renewals were not missed because nobody looked at a dashboard; they were missed because nothing connected a usage decline in March to a single-threaded relationship in May to a renewal in August, and no one had a reason to open that account.

The real job to be done: **"Of the ~120 accounts I own, which 8 conversations will protect the most revenue this week, and what exactly should I say?"** — answered before the CSM opens the laptop.

## What I would build

A **renewal-risk workbench** with four parts:

1. **A fitted, calibrated model** — `P(churn at next renewal)` per account, trained on three years of historical renewal outcomes, using point-in-time features from Salesforce, Stripe, Zendesk, Gmail and product telemetry. Gradient-boosted trees, temporally validated, isotonic-calibrated. Not an LLM judgement.
2. **A Monday queue ranked by expected ARR at risk** (`P(churn) × ARR`), with the **top reasons**, the **raw records** behind each reason, a **data-coverage** badge, and a **recommended play**.
3. **An agent that does the unpaid work** — nightly ingest, rescore, transition detection, queue/task creation, briefing assembly, and draft outreach. It runs on a schedule with no human trigger. **External email is approval-gated by default.** Only transactional, allowlisted plays (e.g. dunning for a failed payment) may auto-send, under consent, frequency, ARR and escalation guardrails.
4. **Full lineage** — every number a CSM sees resolves to `run → model version → feature value → contributing evidence → raw source record`, with both event time and arrival time recorded.

## Where I disagree with the request

**1. "If an account goes red, email them" — no.** A risk score is a *model output about us*, not a message to the customer. Auto-emailing a red account tells a customer we think they are leaving, at the moment they are most fragile. It also leaks churn modelling to the account, creates legal exposure (consent, CAN-SPAM/GDPR), and has no theory of change: a generic "we noticed you're unhappy" email is a false-positive generator. I would automate the *internal* response, and gate customer-facing comms behind a human with a one-click draft.

**2. "Red / yellow / green" hides the two things that matter** — *how sure are we* and *how much is at stake*. I keep the colour as a glanceable summary but the queue sorts on expected ARR at risk, and every row carries a separate confidence badge; accounts with thin data are marked low-confidence and kept out of outreach rather than painted red on no evidence.

**3. "Nothing slips" is the wrong success metric.** Scores always fire. The metric is **% of at-risk ARR surfaced ≥ 90 days before renewal with an action taken**, plus a guardrail on false-positive outreach. Otherwise we optimise for alarm volume.

**4. Trigger on change, not on state.** A stable red account should not generate a new email every night. The agent is edge-triggered (band transition or risk jump), with cooldowns and idempotency keys.

## What I would build first

**Weeks 1–3: the model + the queue + the evidence trail** for one tenant, annual renewals only, 330-day horizon. No outreach automation at all. This is the part that must be trusted; everything else is cheap once it is.

Then: approval-gated drafts and the scheduled agent (weeks 4–5), then the transactional auto-send allowlist. Expansion plays, meeting-note ingestion and multi-language are explicitly out of v1.

## What I need from the business

- **Renewal date, ARR and contract term as first-class, clean fields** — not inferred from opportunity stages. Without these there is no risk denominator.
- **A churn definition and a settled-outcome window** (e.g. "no renewal 30 days after end date"), plus the two lost accounts' timelines as a labelled exemplar.
- **Consent + contact policy**: which contacts may be emailed, by whom, and what the legal entity/opt-out language must be.
- **The playbook and its owners** — what intervention is expected for each reason code, SLA, and escalation path. A score without an agreed action is trivia.
- **A capacity number** (CSMs can absorb N interventions/week) so the queue is cut to fit, not merely sorted.
- **Feedback capture**: did the CSM act, and did the account renew? Without this the model cannot be retrained or honestly evaluated.

## How I will know it works

Temporal holdout (train through Sep 2025, calibrate to Mar 2026, test from Mar 2026): ROC-AUC and PR-AUC vs a usage-trend baseline, Brier score and a reliability curve, **precision of the top-100 ARR-at-risk queue**, and a backtest of realised churn recall by band. Plus invariant tests: point-in-time correctness (late-arriving events must not leak), tenant isolation, idempotent agent runs, and guardrail enforcement.

## What breaks first at 10× (50k accounts / 3.1M events)

Nightly full recompute in SQLite, then per-account occlusion explanations. The fix is incremental feature aggregates (event-time stream + materialised windows), a columnar store, and batch/vectorised explanations — plus queue partitioning by CSM pod. Named in the README with the tradeoff.
