# Model card — renewal churn

**Model family:** `HistGradientBoostingClassifier` + isotonic calibration
**Task:** probability that an account churns at its next renewal
**Owner:** CS Platform engineering
**Status:** v1 demo build, active for `tnt_northwind` and `tnt_lumen`

This card is written to be argued with. It states what the model is for, what
it was trained on, how it was validated, and where it should not be trusted.

## 1. Intended use

- Rank active accounts by **expected ARR at risk** for a CSM's weekly queue.
- Provide a calibrated probability that can be multiplied by ARR and summed
  into a book-level exposure number.
- Supply reason codes and evidence for a human decision. The model does not
  decide what to say or whether to send.
- Support a backtest of "would the queue have surfaced the renewals we lost?"

**Out of scope:** individual credit/collection decisions, expansion
propensity, pricing, automatic contract action, or any customer-facing message
without human approval. The model is not a causal model: it does not estimate
the effect of an intervention.

## 2. Target and label

- **Positive label:** `outcome = 'churned'` on a settled renewal.
- **Negative label:** `outcome = 'renewed'`.
- **Excluded:** `downgraded` (revenue loss, but not full churn) and `open`
  (outcome not settled). The count of excluded downgrades is stored in
  `metrics_json.downgrades_excluded`.
- **Settled window:** a renewal is usable only if it is at least 30 days in the
  past at training time, so the outcome is known.

## 3. Training data

- Source: synthetic but deliberately messy events from Salesforce, Stripe,
  Zendesk, Gmail and product telemetry. See
  [data-dictionary.md](data-dictionary.md).
- **Rows:** one per `(renewal, monthly snapshot 30–330 days before renewal)`.
  Near-renewal snapshots are weighted more heavily (0.35 + 0.65·exp(−days/150)).
- **Split:** by renewal date, never by row: 60% train, 20% calibration, 20%
  test. Exact date boundaries and row counts are in
  `model_versions.metrics_json.split`.
- **Point-in-time:** every feature uses only facts with `occurred_at <= snapshot`
  and `arrived_at <= snapshot`. Late-arriving events are held back.
- **Identity:** accounts are resolved by domain → contact email → fuzzy name;
  unresolved events are counted, not guessed.

## 4. Features

44 features in six groups. The registry (`app/features/registry.py`) is the
single source of truth for training, serving, and the UI.

| Group | Examples |
|---|---|
| Commercial | ARR, seats, days to renewal, tenure, term, segment |
| Product usage | active users, seat utilisation and change, 90-day trend, telemetry gaps, feature breadth, volatility, admin recency |
| Support | ticket volume, P1/P2 count, open tickets, trend, negative-language share, bad CSAT share, resolution hours |
| Relationship | inbound/outbound email, reply trend, days since customer reply, engaged contacts, single-threaded |
| Billing | failed payments and value, days since failure, seat change, card expiring |
| Data quality | source coverage, sources seen, days since any signal |

`source_coverage` and `days_since_any` are also used to label confidence and to
hard-block outreach when the data is too thin.

## 5. Evaluation

The test window is genuinely forward in time. Two views are reported:

- **All horizons:** every test snapshot, calibrated probability.
- **@90 days:** the closest snapshot to 90 days before renewal, one row per
  renewal — the operational view.

Reported metrics: ROC-AUC, PR-AUC, Brier score, log loss, expected calibration
error (ECE), reliability bins, top-k precision/recall, and two simple
baselines (usage trend and days since inbound). The queue metrics at k=100 and
k=250 are stored alongside the model.

### Northwind Cloud — 5,000 accounts, 36 months

Active model `mdl_851805593cae`, trained 2026-10-30. Dataset hash and artifact
path are in `model_versions`.

| Metric | Value |
|---|---:|
| Training rows | 55,876 |
| Train / calibration / test renewals | 2,685 / 1,858 / 1,925 |
| Train rows / calibration / test rows | 21,092 / 17,044 / 17,740 |
| Split boundaries | train ≤ 2025-09-12 · calib ≤ 2026-03-19 · test ≥ 2026-03-20 |
| ROC-AUC (all test rows) | 0.666 |
| PR-AUC (all test rows) | 0.358 |
| Brier (calibrated) | 0.156 |
| Log loss | 0.488 |
| ECE | 0.032 |
| AUC @ 90 days | 0.715 |
| PR-AUC @ 90 days | 0.438 |
| Brier @ 90 days | 0.147 |
| Top-100 precision @ 90d | 0.71 |
| Usage-trend baseline precision @ 100 | 0.26 |
| Days-since-inbound baseline precision @ 100 | 0.18 |
| Train base rate | 16.4% |
| Red / yellow thresholds | 0.350 / 0.164 |
| Downgrades excluded | 548 |

Top permutation-importance features: engaged contacts (0.082), 180-day seat
utilisation (0.021), current seat utilisation (0.014), seat change (0.010),
days to renewal (0.008), days since customer reply (0.005).

### Lumen Analytics — 600 accounts, 24 months

Active model `mdl_04ee06c7fa32`, trained 2026-10-30.

| Metric | Value |
|---|---:|
| Training rows | 3,688 |
| Train / calibration / test renewals | 265 / 97 / 111 |
| Train rows / calibration / test rows | 1,897 / 826 / 965 |
| Split boundaries | train ≤ 2026-03-20 · calib ≤ 2026-06-14 · test ≥ 2026-06-17 |
| ROC-AUC (all test rows) | 0.675 |
| PR-AUC (all test rows) | 0.327 |
| Brier (calibrated) | 0.145 |
| Log loss | 0.580 |
| ECE | 0.066 |
| AUC @ 90 days | 0.764 |
| PR-AUC @ 90 days | 0.451 |
| Brier @ 90 days | 0.131 |
| Precision @ 22 (90d) | 0.36 |
| Usage-trend baseline precision @ 22 | 0.09 |
| Train base rate | 16.5% |
| Red / yellow thresholds | 0.350 / 0.164 |
| Downgrades excluded | 40 |

Lumen is smaller and noisier; its test window is only 111 renewals (21
positives), so the confidence interval around every metric is wide. It is
included to prove multi-tenancy and the auto-send path, not to claim a better
model.


## 6. Calibration and thresholds

Raw tree scores are not probabilities. An isotonic regression is fitted on the
calibration window and applied at serving time. Calibration matters more than
AUC here because the queue multiplies the probability by ARR: a model that is
overconfident at 0.8 creates a real budget error, not just a bad ranking.

Bands are derived from the **training base rate**, not tuned on the test set:

```
red    >= max(0.35, 2.0 × base_rate)
yellow >= max(0.16, base_rate)
```

The exact thresholds are stored in the model artifact and copied into every
score's `reason_json.band_thresholds`, so a later threshold change cannot
rewrite history.

## 7. Explainability

The UI shows the top reason codes by **occlusion attribution**: for each
feature, reset it to the training median, re-score, and measure the change in
log-odds. Positive means the feature increases risk.

This is intentionally not Shapley:

- it is model-agnostic and batchable;
- it can attribute correlated features to each other;
- it is a local sensitivity, not a causal effect;
- the UI labels it "approximate".

Every reason carries the feature value, a display string, an imputed flag, and
the raw evidence records behind it (failed payment, ticket, usage rollup, email)
when available. A reason with no evidence is shown as a feature value, not
dressed up as a story.

## 8. Limitations and risks

- **Synthetic data.** The demo proves method and plumbing, not real-world
  accuracy. The generator encodes a causal structure; a real book will be
  messier and the metrics will move.
- **Selection bias in the label.** Only accounts that reached a renewal are
  labelled. Accounts that churned mid-term, or were removed from the CRM, may
  be missing.
- **Downgrades are excluded.** A renewal at a lower ARR is currently a
  negative-ish outcome that the model does not predict. This understates
  revenue risk.
- **Point-in-time is only as good as arrival timestamps.** If a source
  backfills `arrived_at`, the model can still see the past. The pipeline records
  both times so this is auditable, not invisible.
- **Coverage is not consent.** A high-coverage account can still have no
  contact who consented to email. Policy checks both.
- **No causal claim.** A high score does not mean an intervention will save the
  account. The value estimate in `metrics_json.value` is an assumption.
- **Distribution shift.** The model is trained through the test window and
  served on the latest book; there is no automated drift monitor in v1.

## 9. Monitoring and retraining

- Every run stores the model version, score distribution, band counts, and
  coverage in `agent_runs.stats_json`.
- `model_versions` keeps every artifact and its metrics; exactly one is active
  per tenant.
- Retraining is `python scripts/bootstrap.py --only <slug> --skip-agent` (or
  the training function directly). It is not automatic.
- The honest production loop is: capture whether the CSM acted, then whether
  the account renewed, and retrain on that feedback. That loop is **not** built
  in v1 and is the most important next investment.

## 10. Reproducibility

- Artifact: `artifacts/<tenant>/<model_version>.joblib`, containing the model,
  calibrator, feature list, medians, thresholds, dataset hash, and base rate.
- Metrics: the sibling `.metrics.json` and `model_versions.metrics_json`.
- Code version: `model_versions.code_version` (git SHA when available).
- Data cut-off: `AS_OF = 2026-10-26T06:00:00Z`; late batch at `AS_OF + 2 days`.
- Rebuild: `scripts/bootstrap.py --fresh` with the fixed seeds in
  `app/config.py`. The demo world is frozen so the numbers do not drift.
