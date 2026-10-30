"""Offline evaluation helpers: discrimination, calibration, and queue value."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    log_loss,
    roc_auc_score,
)


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def reliability(p: np.ndarray, y: np.ndarray, bins: int = 10) -> list[dict]:
    df = pd.DataFrame({"p": p, "y": y})
    df["bin"] = pd.qcut(df["p"].rank(method="first"), q=min(bins, max(2, len(df) // 20)), labels=False)
    out = []
    for b, g in df.groupby("bin"):
        out.append({"bin": int(b), "n": int(len(g)), "predicted": round(float(g["p"].mean()), 4),
                    "observed": round(float(g["y"].mean()), 4)})
    return out


def discrimination(p: np.ndarray, y: np.ndarray) -> dict:
    if len(np.unique(y)) < 2:
        return {"auc": None, "pr_auc": None}
    return {
        "auc": round(float(roc_auc_score(y, p)), 4),
        "pr_auc": round(float(average_precision_score(y, p)), 4),
    }


def queue_metrics(df: pd.DataFrame, k: int = 100, arr_col: str = "arr_before_cents") -> dict:
    """Precision/recall at the top of the queue, weighted by revenue.

    `df` must have one row per renewal: prob, label, arr.
    """
    if df.empty:
        return {}
    df = df.sort_values("prob", ascending=False)
    top = df.head(k)
    churned = df[df["label"] == 1]
    return {
        "k": k,
        "precision_at_k": round(float(top["label"].mean()), 4) if len(top) else None,
        "recall_renewals_at_k": round(float(top["label"].sum() / max(1, df["label"].sum())), 4),
        "arr_at_risk_captured_at_k": round(float(top[arr_col].sum() / max(1, df[arr_col].sum())), 4),
        "churned_arr_recall_at_k": round(
            float(top.loc[top["label"] == 1, arr_col].sum() / max(1, churned[arr_col].sum())), 4),
        "churned_arr_total_cents": int(churned[arr_col].sum()),
    }


def net_value(df: pd.DataFrame, k: int = 100, intervention_cost_cents: int = 40_000,
              relative_save_lift: float = 0.12) -> dict:
    """A deliberately simple value model, stated so it can be argued with.

    Intervening on a true churn saves `relative_save_lift * ARR`; intervening on
    a false positive costs `intervention_cost_cents`. We do not claim this is
    causal — it is the assumption the business needs to confirm.
    """
    top = df.sort_values("prob", ascending=False).head(k)
    saved = float((top["label"] * top["arr_before_cents"] * relative_save_lift).sum())
    cost = float(len(top) * intervention_cost_cents)
    return {
        "k": k,
        "assumed_relative_save_lift": relative_save_lift,
        "intervention_cost_cents": intervention_cost_cents,
        "expected_value_saved_cents": int(saved),
        "expected_cost_cents": int(cost),
        "net_value_cents": int(saved - cost),
        "true_churns_in_queue": int(top["label"].sum()),
    }


def metrics(p: np.ndarray, y: np.ndarray, p_uncal: np.ndarray | None = None) -> dict:
    out = discrimination(p, y)
    out["brier"] = round(float(brier_score_loss(y, p)), 4)
    if p_uncal is not None:
        out["brier_uncalibrated"] = round(float(brier_score_loss(y, p_uncal)), 4)
    out["log_loss"] = round(float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6))), 4)
    out["n"] = int(len(y))
    out["positives"] = int(np.sum(y))
    out["base_rate"] = round(float(np.mean(y)), 4)
    out["calibration"] = reliability(p, y)
    return out


def expected_calibration_error(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    rel = reliability(p, y, bins)
    n = max(1, len(y))
    return round(float(sum(abs(r["predicted"] - r["observed"]) * r["n"] / n for r in rel)), 4)
