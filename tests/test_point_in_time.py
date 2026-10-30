"""Point-in-time and late-arrival invariants."""
from __future__ import annotations

from datetime import timedelta

from app.agent.runner import run_agent
from app.config import AS_OF, iso
from app.db import one, scalar
from tests.conftest import TENANT


def test_facts_never_use_data_that_had_not_arrived(world):
    conn = world["conn"]
    assert world["etl"]["held_back"] > 0
    future = scalar(conn, """SELECT COUNT(*) FROM facts WHERE tenant_id=? AND arrived_at > ?""",
                    (TENANT, iso(AS_OF)))
    assert future == 0


def test_scores_are_bounded_by_the_run_as_of(world):
    conn = world["conn"]
    run_id = world["first_run"]["run_id"]
    bad = scalar(conn, """SELECT COUNT(*) FROM scores WHERE tenant_id=? AND run_id=? AND as_of > ?""",
                 (TENANT, run_id, iso(AS_OF)))
    assert bad == 0


def test_a_later_run_ingests_the_held_back_records(world):
    conn = world["conn"]
    later = AS_OF + timedelta(days=3)
    before = scalar(conn, "SELECT COUNT(*) FROM facts WHERE tenant_id=?", (TENANT,))
    result = run_agent(conn, TENANT, trigger="test-late", as_of=later)
    after = scalar(conn, "SELECT COUNT(*) FROM facts WHERE tenant_id=?", (TENANT,))
    assert result["ingest"]["kept"] > 0
    assert after > before
    # The new facts respect the new cut-off, not the wall clock.
    too_new = scalar(conn, "SELECT COUNT(*) FROM facts WHERE tenant_id=? AND arrived_at > ?",
                     (TENANT, iso(later)))
    assert too_new == 0
