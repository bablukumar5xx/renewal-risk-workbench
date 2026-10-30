# Loom walkthrough script (5–7 minutes)

This is the recording substitute: the environment cannot capture video. Record
in one take at 1440×900 or larger, with the app running at
`http://127.0.0.1:8000`. Speak the **Say** lines; do the **Do** lines.

Before recording:

```bash
cd /Users/bablu/Documents/customer-health-platform
.venv/bin/python scripts/bootstrap.py --fresh   # only if data/artifacts are absent
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open two tabs: the app, and `/docs` (FastAPI's API reference). Zoom the browser
to 100%.

---

## 0:00 — Framing (30s)

**Do:** show the login screen. Do not log in yet.

**Say:**
> This is a renewal-risk workbench, not a dashboard and not an auto-emailer.
> The VP asked for a system that surfaces at-risk renewals from five messy
> systems and "emails red accounts." I built the first three parts and pushed
> back on the fourth. The product is a ranked, explainable queue with a
> recommended play and an approval gate.

## 0:30 — Sign in as Northwind (20s)

**Do:** click **Northwind Cloud** (or paste `chp_demo_northwind_2f8a`). Land on
**Today**.

**Say:**
> Northwind is 5,000 accounts, 36 months, about 287,000 raw events. The key
> resolves to exactly one tenant; there is no tenant ID in any URL. The first
> agent run happened unattended at the frozen as-of, Monday 26 October, 6 a.m.

## 0:50 — The Monday brief (45s)

**Do:** point at the headline cards: red / yellow / green, ARR at risk,
coverage, approvals waiting. Scroll the top of the queue.

**Say:**
> The brief is computed from the latest successful run, not typed into a slide.
> The queue is ranked by calibrated probability times ARR, capped to the CSMs'
> capacity. A red account with no data is not painted red: it is marked low
> confidence and kept out of outreach until coverage clears the tenant minimum.
> The number at risk is the sum of expected ARR at risk for red and yellow
> accounts inside the 330-day horizon. Every one of those numbers is a sum over
> `scores` rows.

## 1:35 — One account, end to end (90s)

**Do:** click the top account. Walk the drawer top to bottom: score and band,
top reasons, feature table, contacts and consent, actions, renewal history,
source counts. Click a **reason's evidence** item. Then open **Lineage** (or
copy the fact ID and open `/api/lineage/fact/{id}`).

**Say:**
> This is the part that matters. The score is a model output: gradient-boosted
> trees, isotonic-calibrated, trained on settled renewals with point-in-time
> features. The probability is not a vibe; it is a number from a versioned
> artifact. The reasons are occlusion attributions: reset this feature to the
> training median and measure the change in log-odds. They are approximate and
> labelled that way.
>
> Behind each reason is the actual record: this failed payment, this support
> ticket, this usage rollup. The lineage endpoint walks fact → raw event and
> shows both event time and arrival time, so we can prove the Monday run did
> not use Tuesday's data.

## 3:05 — Actions and policy (70s)

**Do:** go to **Actions**. Filter to **Awaiting approval**. Open one email
draft. Show the policy trace: consent, coverage, ARR ceiling, cooldown, caps,
auto-send policy. Approve it. Then open **Outbox** and show the sent row, then
the action's event history.

**Say:**
> The agent drafts; a human decides. Only the kill switch, missing consent, and
> insufficient source coverage are hard blocks. An ARR ceiling or a cooldown
> forces approval but does not stop a human from sending. That distinction is
> deliberate: guardrails should make me think, not silently override me.
>
> Approving writes three things atomically: the action event, the outbox row
> with a provider message ID and the policy trace, and the audit log. Nothing
> is "sent" in the UI without a row behind it.

**Do:** switch to **Lumen Analytics** using the tenant switcher (key
`chp_demo_lumen_9c14`). Open **Actions**, filter **Sent**, and show a
transactional auto-send.

**Say:**
> Lumen has auto-send enabled, but only for the transactional allowlist —
> billing recovery under the ARR ceiling, consent, caps, and cooldown. Even
> there, non-transactional plays still need a human. This tenant exists to prove
> the policy path, not to make auto-send the default.

## 4:15 — The unattended run (45s)

**Do:** go to **Runs**. Open the latest run. Show the five steps and their
stats. Then go to **Pipeline** and show raw→facts counts, duplicate rate,
unmatched rate, timezone assumptions, four-source coverage.

**Say:**
> This is the agentic part: a scheduler calls the same `run_agent` as the CLI,
> with no human trigger. Ingest, score, detect change, route, digest. Re-running
> the same window is a no-op; a later run in the same week cannot create a
> second action for an account that already has one. Late-arriving events are
> held back and picked up by the next run, not backfilled into the past.
>
> The pipeline screen is the honest view of the data: 287,000 raw events became
> 272,000 facts, with duplicates and unmatched records counted rather than
> hidden.

## 5:00 — Model health (45s)

**Do:** go to **Model**. Show the active version, AUC, PR-AUC, Brier, ECE,
90-day view, top features, and the temporal split dates. Open `/api/model/health`
in the API tab if time.

**Say:**
> The split is by renewal date, not by row, so the same contract cannot appear
> in train and test at different ages. The test window is genuinely forward in
> time. Calibration matters more than AUC here because the queue multiplies the
> probability by ARR; an overconfident 0.8 is a real budget error.
>
> The top features are the ones the model actually uses, not the ones that make
> a nice story.

## 5:45 — What I did not build / what breaks (45s)

**Do:** return to the README section **What breaks first at 10×** on screen, or
show the slide.

**Say:**
> Deliberate skips: no causal uplift model, no expansion scoring, no meeting
> notes, no real email provider, no automated retraining. The value estimate is
> a stated assumption, not a measured lift.
>
> At ten times the data, SQLite full recompute breaks first, then per-account
> occlusion explanations. The fix is incremental feature aggregates, a columnar
> store, and batched explanations — not a bigger laptop. Those are named in the
> README with the tradeoff.

## 6:30 — Close (15s)

**Say:**
> The one-line version: automate the internal work, make every number
> traceable, and keep the customer conversation behind a human until the
> playbook and consent policy are agreed. That is the product I would ship
> first.

**Do:** stop recording.
