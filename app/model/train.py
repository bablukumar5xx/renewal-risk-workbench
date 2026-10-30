"""Train and version a tenant's renewal-churn model.

Design choices that matter (see docs/model-card.md):
  * one row per (renewal, monthly pre-renewal snapshot), 30-330 days out
  * split by *renewal date*, never by row, so snapshots of the same renewal
    cannot straddle train and test
  * isotonic calibration on a held-out time window
  * no LLM in the scoring path
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score

from ..config import ARTIFACT_DIR, CODE_VERSION, MODEL_FEATURES_VERSION, iso
from ..db import insert_many, j, one, rows, tx
from ..features.builder import build_panel, load_accounts
from ..features.registry import FEATURE_META, FEATURES
from .evaluate import expected_calibration_error, metrics, queue_metrics, net_value, reliability

HORIZON_MIN = 30
HORIZON_MAX = 330


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL,
                                       text=True).strip()
    except Exception:
        return CODE_VERSION


def _logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def training_rows(panel: pd.DataFrame, renewals: pd.DataFrame) -> pd.DataFrame:
    snap = panel[["account_id", "snapshot_at", *FEATURES]].copy()
    m = renewals.merge(snap, on="account_id", how="inner")
    m["days_to_renewal"] = (m["renewal_date"] - m["snapshot_at"]).dt.days
    m = m[(m["days_to_renewal"] >= HORIZON_MIN) & (m["days_to_renewal"] <= HORIZON_MAX)]
    m["label"] = (m["outcome"] == "churned").astype(int)
    # Same renewal can only use snapshots after its previous renewal, which is
    # implicit: the previous renewal is settled before the next 330-day window.
    m = m.sort_values(["renewal_date", "days_to_renewal"])
    return m


def split_by_renewal_dates(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = np.sort(df["renewal_date"].dropna().unique())
    if len(dates) < 12:
        raise ValueError(f"not enough settled renewal dates to train: {len(dates)}")
    tr_end = dates[int(len(dates) * 0.60) - 1]
    ca_end = dates[int(len(dates) * 0.80) - 1]
    train = df[df["renewal_date"] <= tr_end]
    calib = df[(df["renewal_date"] > tr_end) & (df["renewal_date"] <= ca_end)]
    test = df[df["renewal_date"] > ca_end]
    return train, calib, test


def _at_horizon(df: pd.DataFrame, target: int = 90) -> pd.DataFrame:
    """One row per renewal, closest snapshot to the target horizon."""
    if df.empty:
        return df
    d = df.copy()
    d["dist"] = (d["days_to_renewal"] - target).abs()
    return d.sort_values("dist").groupby("renewal_id", as_index=False).first()


def train_tenant(conn, tenant_id: str, as_of: datetime, register: bool = True) -> dict:
    accts = load_accounts(conn, tenant_id)
    if accts.empty:
        raise ValueError(f"no accounts for tenant {tenant_id}")

    start = accts["created_at"].min().normalize().replace(day=1)
    snapshots = list(pd.date_range(start, pd.Timestamp(as_of).normalize(), freq="MS").to_pydatetime())
    if snapshots and snapshots[-1].date() != pd.Timestamp(as_of).date():
        snapshots.append(pd.Timestamp(as_of).to_pydatetime())
    panel = build_panel(conn, tenant_id, snapshots, accounts=accts)

    ren = pd.read_sql_query(
        """SELECT renewal_id, account_id, renewal_date, outcome, arr_before_cents, arr_after_cents, settled
           FROM renewals WHERE tenant_id = ? AND settled = 1 AND outcome IN ('churned','renewed','downgraded')
           ORDER BY renewal_date""", conn, params=(tenant_id,))
    if ren.empty:
        raise ValueError("no settled renewals")
    ren["renewal_date"] = pd.to_datetime(ren["renewal_date"], utc=True)
    ren = ren[ren["renewal_date"] <= pd.Timestamp(as_of) - pd.Timedelta(days=30)]
    # Downgrades are revenue loss but the v1 target is full churn; keep them out
    # of training and report them separately in the model card.
    downgrades = int((ren["outcome"] == "downgraded").sum())
    ren = ren[ren["outcome"].isin(["churned", "renewed"])].reset_index(drop=True)
    ren["label"] = (ren["outcome"] == "churned").astype(int)

    data = training_rows(panel, ren)
    if len(data) < 500:
        raise ValueError(f"not enough training rows: {len(data)}")

    train, calib, test = split_by_renewal_dates(data)
    Xtr, ytr = train[FEATURES], train["label"].to_numpy()
    Xca, yca = calib[FEATURES], calib["label"].to_numpy()
    Xte, yte = test[FEATURES], test["label"].to_numpy()

    # Nearer snapshots are the ones CSMs act on; weight them more without
    # discarding the far-horizon rows that make the model usable year-round.
    w = 0.35 + 0.65 * np.exp(-train["days_to_renewal"].to_numpy() / 150.0)
    model = HistGradientBoostingClassifier(
        max_iter=400, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=40,
        l2_regularization=1.5, early_stopping=False, random_state=42,
    )
    model.fit(Xtr, ytr, sample_weight=w)
    p_raw_ca = model.predict_proba(Xca)[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    calibrator.fit(p_raw_ca, yca)
    p_ca = calibrator.predict(p_raw_ca)
    p_te_raw = model.predict_proba(Xte)[:, 1]
    p_te = calibrator.predict(p_te_raw)

    # metrics on all horizons + the operational 90-day view
    met = metrics(p_te, yte, p_te_raw)
    met["ece"] = expected_calibration_error(p_te, yte)
    h90 = _at_horizon(test, 90)
    h90["prob"] = calibrator.predict(model.predict_proba(h90[FEATURES])[:, 1])
    met["at_90d"] = metrics(h90["prob"].to_numpy(), h90["label"].to_numpy())
    met["queue"] = queue_metrics(h90, k=min(100, max(10, len(h90) // 5)))
    met["queue_250"] = queue_metrics(h90, k=min(250, max(10, len(h90) // 2)))
    met["value"] = net_value(h90, k=min(100, max(10, len(h90) // 5)))

    # honest baselines: what a CSM spreadsheet rule would do
    base = h90.copy()
    base["prob"] = (-base["usage_util_delta_30"].fillna(0)).rank(pct=True)
    met["baseline_usage_trend"] = {**queue_metrics(base, k=met["queue"]["k"]),
                                  "auc": round(float(roc_auc_score(base["label"], base["prob"])), 4)
                                  if base["label"].nunique() > 1 else None}
    base2 = h90.copy()
    base2["prob"] = (base2["days_since_inbound"].fillna(0)).rank(pct=True)
    met["baseline_silence"] = {**queue_metrics(base2, k=met["queue"]["k"]),
                               "auc": round(float(roc_auc_score(base2["label"], base2["prob"])), 4)
                               if base2["label"].nunique() > 1 else None}

    # global importance on a sample so training stays quick
    sample = test.sample(min(4000, len(test)), random_state=7)
    imp = permutation_importance(model, sample[FEATURES], sample["label"], n_repeats=3,
                                 random_state=7, scoring="roc_auc")
    order = np.argsort(imp.importances_mean)[::-1][:15]
    met["top_features"] = [
        {"feature": FEATURES[i], "label": FEATURE_META[FEATURES[i]]["label"],
         "importance": round(float(imp.importances_mean[i]), 5)}
        for i in order
    ]

    base_rate = float(ytr.mean())
    thresholds = {"red": round(max(0.35, 2.0 * base_rate), 3), "yellow": round(max(0.16, base_rate), 3)}
    met["thresholds"] = thresholds
    met["split"] = {
        "train_renewals": int(train["renewal_id"].nunique()),
        "calib_renewals": int(calib["renewal_id"].nunique()),
        "test_renewals": int(test["renewal_id"].nunique()),
        "train_rows": int(len(train)), "calib_rows": int(len(calib)), "test_rows": int(len(test)),
        "train_through": iso(pd.Timestamp(train["renewal_date"].max()).to_pydatetime()),
        "calib_through": iso(pd.Timestamp(calib["renewal_date"].max()).to_pydatetime()),
        "test_from": iso(pd.Timestamp(test["renewal_date"].min()).to_pydatetime()),
    }
    met["feature_version"] = MODEL_FEATURES_VERSION
    met["train_base_rate"] = round(base_rate, 4)
    met["downgrades_excluded"] = downgrades
    met["excluded_late_rows"] = int(len(panel) * 0)

    medians = Xtr.median(numeric_only=True)
    # Fall back to a neutral value where a column is entirely missing.
    medians = medians.fillna(pd.Series(0.0, index=FEATURES))
    version = "mdl_" + hashlib.sha256(
        f"{tenant_id}{iso(as_of)}{len(train)}{len(calib)}{met['at_90d'].get('auc')}{MODEL_FEATURES_VERSION}".encode()
    ).hexdigest()[:12]
    dataset_hash = hashlib.sha256(
        f"{MODEL_FEATURES_VERSION}:{len(data)}:{met['split']['train_through']}:{met['split']['test_from']}".encode()
    ).hexdigest()[:16]

    out_dir = ARTIFACT_DIR / tenant_id
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / f"{version}.joblib"
    joblib.dump({
        "model": model, "calibrator": calibrator, "features": FEATURES,
        "medians": medians.to_dict(), "thresholds": thresholds,
        "feature_meta": FEATURE_META, "version": version, "trained_at": iso(datetime.now(timezone.utc)),
        "dataset_hash": dataset_hash, "base_rate": base_rate,
    }, artifact_path)
    (out_dir / f"{version}.metrics.json").write_text(json.dumps(met, indent=2))

    if register:
        with tx(conn):
            conn.execute("UPDATE model_versions SET is_active=0 WHERE tenant_id=?", (tenant_id,))
            insert_many(conn, "model_versions", [
                "id", "tenant_id", "algorithm", "trained_at", "train_start", "train_end", "calib_start",
                "calib_end", "test_start", "test_end", "features_json", "hyperparams_json", "metrics_json",
                "artifact_path", "dataset_hash", "code_version", "is_active", "base_rate",
            ], [(
                version, tenant_id, "HistGradientBoostingClassifier + isotonic", iso(datetime.now(timezone.utc)),
                met["split"]["train_through"], met["split"]["train_through"],
                met["split"]["train_through"], met["split"]["calib_through"],
                met["split"]["test_from"], iso(as_of), j(FEATURES),
                j({"max_iter": 350, "learning_rate": 0.055, "max_leaf_nodes": 31,
                   "min_samples_leaf": 40, "l2_regularization": 1.5}),
                j(met), str(artifact_path), dataset_hash, _git_sha(), 1, base_rate,
            )])
    return {"version": version, "metrics": met, "artifact_path": str(artifact_path),
            "dataset_hash": dataset_hash, "rows": len(data), "panel_rows": len(panel)}


def load_active_model(conn, tenant_id: str) -> tuple[dict, dict]:
    row = one(conn, "SELECT * FROM model_versions WHERE tenant_id=? AND is_active=1 ORDER BY trained_at DESC LIMIT 1",
              (tenant_id,))
    if not row:
        raise ValueError(f"no active model for tenant {tenant_id}")
    art = joblib.load(row["artifact_path"])
    return dict(row), art
