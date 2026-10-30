"""Background scheduler. This is what makes the system agentic rather than a UI.

A daemon thread checks, per tenant, whether the interval has elapsed and runs
the same `run_agent` used by the CLI. Failures are recorded in agent_runs and do
not kill the loop.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from .config import (SCHEDULER_ENABLED, SCHEDULER_INTERVAL_MINUTES, SCHEDULER_TICK_SECONDS,
                     TENANT_CONFIG)
from .db import connect, one, rows

_started = False
_last_error: str | None = None


def _due(conn, tenant_id: str) -> bool:
    last = one(conn, "SELECT started_at FROM agent_runs WHERE tenant_id=? AND status='success' ORDER BY started_at DESC LIMIT 1",
               (tenant_id,))
    if not last:
        return False
    try:
        dt = datetime.fromisoformat(last["started_at"].replace("Z", "+00:00"))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - dt).total_seconds() >= SCHEDULER_INTERVAL_MINUTES * 60


def _loop() -> None:
    global _last_error
    from .agent.runner import run_agent

    conn = connect()
    while True:
        try:
            tenants = rows(conn, "SELECT id FROM tenants")
            for t in tenants:
                if _due(conn, t["id"]):
                    try:
                        run_agent(conn, t["id"], trigger="schedule", as_of=datetime.now(timezone.utc))
                        _last_error = None
                    except Exception as exc:  # noqa: BLE001
                        _last_error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001
            _last_error = f"scheduler: {type(exc).__name__}: {exc}"
        time.sleep(SCHEDULER_TICK_SECONDS)


def start() -> None:
    global _started
    if not SCHEDULER_ENABLED or _started:
        return
    _started = True
    threading.Thread(target=_loop, name="chp-scheduler", daemon=True).start()


def status() -> dict:
    return {"enabled": SCHEDULER_ENABLED, "interval_minutes": SCHEDULER_INTERVAL_MINUTES,
            "last_error": _last_error, "started": _started}
