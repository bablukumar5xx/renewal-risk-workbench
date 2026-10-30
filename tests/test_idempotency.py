"""Idempotency: retries, late runs in the same week, and the canonical store."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from app.agent.runner import run_agent
from app.config import AS_OF
from app.db import rows, scalar, tx
from app.ingest.canonicalize import canonicalize
from tests.conftest import TENANT


def test_rerunning_the_same_window_is_a_noop(world):
    conn = world["conn"]
    first = world["first_run"]
    runs_before = scalar(conn, "SELECT COUNT(*) FROM agent_runs WHERE tenant_id=?", (TENANT,))
    scores_before = scalar(conn, "SELECT COUNT(*) FROM scores WHERE tenant_id=? AND run_id=?",
                           (TENANT, first["run_id"]))
    actions_before = scalar(conn, "SELECT COUNT(*) FROM actions WHERE tenant_id=?", (TENANT,))

    again = run_agent(conn, TENANT, trigger=first["trigger"], as_of=AS_OF)

    assert again["run_id"] == first["run_id"]
    assert again["deduplicated"] is True
    assert scalar(conn, "SELECT COUNT(*) FROM agent_runs WHERE tenant_id=?", (TENANT,)) == runs_before
    assert scalar(conn, "SELECT COUNT(*) FROM scores WHERE tenant_id=? AND run_id=?",
                  (TENANT, first["run_id"])) == scores_before
    assert scalar(conn, "SELECT COUNT(*) FROM actions WHERE tenant_id=?", (TENANT,)) == actions_before


def test_recanonicalising_the_same_window_adds_nothing(world):
    conn = world["conn"]
    before = {
        "raw": scalar(conn, "SELECT COUNT(*) FROM raw_events WHERE tenant_id=?", (TENANT,)),
        "facts": scalar(conn, "SELECT COUNT(*) FROM facts WHERE tenant_id=?", (TENANT,)),
        "processed": scalar(conn, "SELECT COUNT(*) FROM processed_events WHERE tenant_id=?", (TENANT,)),
    }
    result = canonicalize(conn, TENANT, AS_OF)
    assert result["read"] == 0
    assert result["kept"] == 0
    assert result["facts"] == 0
    assert scalar(conn, "SELECT COUNT(*) FROM raw_events WHERE tenant_id=?", (TENANT,)) == before["raw"]
    assert scalar(conn, "SELECT COUNT(*) FROM facts WHERE tenant_id=?", (TENANT,)) == before["facts"]
    assert scalar(conn, "SELECT COUNT(*) FROM processed_events WHERE tenant_id=?", (TENANT,)) == before["processed"]


def test_a_second_run_in_the_same_week_does_not_duplicate_actions(world):
    conn = world["conn"]
    before = scalar(conn, "SELECT COUNT(*) FROM actions WHERE tenant_id=?", (TENANT,))
    # Tuesday, same ISO week, no new facts have arrived yet.
    result = run_agent(conn, TENANT, trigger="test-second-run", as_of=AS_OF + timedelta(days=1))
    assert result["actions"]["duplicates"] > 0
    # A later run may surface an account that was outside Monday's capacity, but
    # it must never give an already-actioned account a second action.
    twice = scalar(conn, """SELECT COUNT(*) FROM (
                              SELECT account_id FROM actions WHERE tenant_id=?
                              GROUP BY account_id HAVING COUNT(*) > 1)""", (TENANT,))
    assert twice == 0
    after = scalar(conn, "SELECT COUNT(*) FROM actions WHERE tenant_id=?", (TENANT,))
    assert after == before + result["actions"]["created"]
    new_accounts = {r["account_id"] for r in rows(conn, "SELECT account_id FROM actions WHERE run_id=?",
                                                   (result["run_id"],))}
    assert len(new_accounts) == result["actions"]["created"]


def test_action_keys_are_unique_per_tenant(world):
    conn = world["conn"]
    for tenant in (TENANT, "tnt_other"):
        total = scalar(conn, "SELECT COUNT(*) FROM actions WHERE tenant_id=?", (tenant,))
        distinct = scalar(conn, "SELECT COUNT(DISTINCT idempotency_key) FROM actions WHERE tenant_id=?", (tenant,))
        assert total == distinct


def test_database_rejects_a_duplicate_action_key(world):
    conn = world["conn"]
    row = conn.execute("SELECT * FROM actions WHERE tenant_id=? LIMIT 1", (TENANT,)).fetchone()
    assert row is not None
    with pytest.raises(sqlite3.IntegrityError):
        with tx(conn):
            conn.execute("""INSERT INTO actions (id, tenant_id, account_id, play_key, play_name, channel,
                            status, priority, trigger_reason, idempotency_key, created_at, updated_at)
                            VALUES ('act_duplicate_probe', ?, ?, ?, ?, ?, 'proposed', 3, 'probe', ?, ?, ?)""",
                         (TENANT, row["account_id"], row["play_key"], row["play_name"], row["channel"],
                          row["idempotency_key"], row["created_at"], row["updated_at"]))