"""Model contract: temporal split, horizon, calibration, and stored artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from app.db import one, rows, scalar, unj
from app.features.registry import FEATURES
from app.model.scoring import load_active_model
from app.model.train import HORIZON_MAX, HORIZON_MIN, split_by_renewal_dates, training_rows
from tests.conftest import TENANT


def test_split_is_by_renewal_date_not_by_row():
    dates = pd.date_range("2024-01-01", periods=20, freq="MS", tz="UTC")
    base = pd.DataFrame({"renewal_id": [f"ren_{i}" for i in range(20)], "renewal_date": dates})
    # Several snapshots per renewal must stay on the same side of every split.
    df = pd.concat([base, base, base], ignore_index=True)
    train, calib, test = split_by_renewal_dates(df)
    assert set(train["renewal_id"]).isdisjoint(set(test["renewal_id"]))
    assert set(train["renewal_id"]).isdisjoint(set(calib["renewal_id"]))
    assert set(calib["renewal_id"]).isdisjoint(set(test["renewal_id"]))
    assert train["renewal_date"].max() < calib["renewal_date"].min()
    assert calib["renewal_date"].max() < test["renewal_date"].min()


def test_training_rows_only_keep_the_actionable_horizon():
    snapshots = pd.to_datetime(
        ["2025-01-01", "2025-02-01", "2025-04-01", "2026-01-01"], utc=True
    )
    panel = pd.DataFrame({f: 0.0 for f in FEATURES}, index=range(len(snapshots)))
    panel["account_id"] = "acc_horizon"
    panel["snapshot_at"] = snapshots
    renewals = pd.DataFrame([{
        "renewal_id": "ren_horizon", "account_id": "acc_horizon",
        "renewal_date": pd.Timestamp("2025-06-01", tz="UTC"),
        "outcome": "churned", "arr_before_cents": 100_000, "arr_after_cents": 0, "settled": 1,
    }])
    out = training_rows(panel, renewals)
    assert list(out["days_to_renewal"]) == [61, 120, 151]
    assert all(HORIZON_MIN <= d <= HORIZON_MAX for d in out["days_to_renewal"])
    assert set(out["label"]) == {1}


def test_active_model_is_versioned_and_loadable(world):
    conn = world["conn"]
    model = one(conn, "SELECT * FROM model_versions WHERE tenant_id=? AND is_active=1", (TENANT,))
    assert model is not None
    active = scalar(conn, "SELECT COUNT(*) FROM model_versions WHERE tenant_id=? AND is_active=1", (TENANT,))
    assert active == 1
    assert Path(model["artifact_path"]).exists()

    loaded, art = load_active_model(conn, TENANT)
    assert loaded["id"] == model["id"] == art["version"]
    assert art["features"] == FEATURES
    assert art["dataset_hash"] == model["dataset_hash"]
    assert set(art["medians"]) == set(FEATURES)


def test_metrics_are_complete_and_honest(world):
    conn = world["conn"]
    model = one(conn, "SELECT * FROM model_versions WHERE tenant_id=? AND is_active=1", (TENANT,))
    metrics = unj(model["metrics_json"])
    for key in ("auc", "pr_auc", "brier", "ece"):
        assert key in metrics
        assert 0.0 <= float(metrics[key]) <= 1.0
    assert 0.0 <= metrics["at_90d"]["auc"] <= 1.0
    assert metrics["at_90d"]["n"] > 0
    split = metrics["split"]
    assert split["train_renewals"] > 0 and split["calib_renewals"] > 0 and split["test_renewals"] > 0
    assert split["train_through"] < split["test_from"]
    assert sum(split[k] for k in ("train_rows", "calib_rows", "test_rows")) > 500
    thresholds = metrics["thresholds"]
    assert 0 < thresholds["yellow"] < thresholds["red"] < 1
    assert metrics["top_features"]
    artifact_metrics = json.loads(Path(model["artifact_path"].replace(".joblib", ".metrics.json")).read_text())
    assert artifact_metrics["thresholds"] == thresholds


def test_calibrator_is_monotone_and_predictions_are_probabilities(world):
    _, art = load_active_model(world["conn"], TENANT)
    grid = np.linspace(0.0, 1.0, 101)
    calibrated = art["calibrator"].predict(grid)
    assert np.all(np.diff(calibrated) >= -1e-12)
    row = pd.DataFrame([{f: art["medians"][f] for f in FEATURES}])
    p = float(art["calibrator"].predict(art["model"].predict_proba(row)[:, 1])[0])
    assert 0.0 <= p <= 1.0


def test_every_stored_score_is_reproducible_from_its_features(world):
    conn = world["conn"]
    scores = rows(conn, "SELECT * FROM scores WHERE tenant_id=? AND run_id=?",
                  (TENANT, world["first_run"]["run_id"]))
    assert scores
    for s in scores[:25]:
        features = {r["feature"]: r["value"] for r in
                    rows(conn, "SELECT feature, value FROM score_features WHERE score_id=?", (s["id"],))}
        assert set(features) == set(FEATURES)
        payload = json.dumps({k: (None if features[k] is None else round(float(features[k]), 6))
                              for k in FEATURES}, sort_keys=True)
        assert hashlib.sha256(payload.encode()).hexdigest()[:16] == s["feature_hash"]

        acct = one(conn, "SELECT arr_cents FROM dim_accounts WHERE tenant_id=? AND account_id=?",
                   (TENANT, s["account_id"]))
        assert s["expected_arr_at_risk_cents"] == round(s["prob"] * acct["arr_cents"])
        thresholds = unj(s["reason_json"])["band_thresholds"]
        expected_band = ("red" if s["prob"] >= thresholds["red"]
                         else "yellow" if s["prob"] >= thresholds["yellow"] else "green")
        assert s["band"] == expected_band
        assert 0.0 <= s["prob"] <= 1.0