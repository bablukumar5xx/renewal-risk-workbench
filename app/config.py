"""Runtime configuration. Everything is file-backed so the demo is one process."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("CHP_DATA_DIR", ROOT / "data"))
ARTIFACT_DIR = Path(os.getenv("CHP_ARTIFACT_DIR", ROOT / "artifacts"))
WEB_DIR = ROOT / "app" / "web"
DB_PATH = Path(os.getenv("CHP_DB_PATH", DATA_DIR / "chp.db"))

# The demo world is frozen at a Monday morning so runs are reproducible.
AS_OF = datetime.fromisoformat(os.getenv("CHP_AS_OF", "2026-10-26T06:00:00+00:00"))

TENANT_CONFIG = [
    {
        "id": "tnt_northwind",
        "name": "Northwind Cloud",
        "slug": "northwind",
        "api_key": "chp_demo_northwind_2f8a",
        "accounts": 5000,
        "months": 36,
        "seed": 20261026,
        "config": {
            "auto_send_enabled": False,
            "auto_send_max_arr_cents": 5_000_000,
            "daily_send_cap": 25,
            "per_account_cooldown_days": 7,
            "per_account_monthly_cap": 3,
            "min_coverage_for_outreach": 0.45,
            "kill_switch": False,
            "risk_jump_threshold": 0.15,
            "queue_capacity": 120,
        },
    },
    {
        "id": "tnt_lumen",
        "name": "Lumen Analytics",
        "slug": "lumen",
        "api_key": "chp_demo_lumen_9c14",
        "accounts": 600,
        "months": 24,
        "seed": 99001,
        "config": {
            "auto_send_enabled": True,
            "auto_send_max_arr_cents": 6_000_000,
            "daily_send_cap": 10,
            "per_account_cooldown_days": 14,
            "per_account_monthly_cap": 2,
            "min_coverage_for_outreach": 0.45,
            "kill_switch": False,
            "risk_jump_threshold": 0.15,
            "queue_capacity": 60,
        },
    },
]

# Runs on a background thread inside the API process; set CHP_SCHEDULER=0 to disable.
SCHEDULER_ENABLED = os.getenv("CHP_SCHEDULER", "1") == "1"
SCHEDULER_TICK_SECONDS = int(os.getenv("CHP_SCHEDULER_TICK", "60"))
# In demo mode a scheduled run is due every N minutes of wall clock.
SCHEDULER_INTERVAL_MINUTES = int(os.getenv("CHP_SCHEDULER_INTERVAL_MIN", "30"))

MODEL_FEATURES_VERSION = "features/v1"
CODE_VERSION = os.getenv("CHP_CODE_VERSION", "dev")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
