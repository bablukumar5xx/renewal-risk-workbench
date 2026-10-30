"""Shared test world.

A small tenant is generated, canonicalised, trained and run once per session.
Tests use the same code paths as production; only the size changes.
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

# Must be set before app modules import config.DB_PATH.
TEST_DIR = Path(tempfile.mkdtemp(prefix="chp-tests-"))
os.environ["CHP_DB_PATH"] = str(TEST_DIR / "test.db")
os.environ["CHP_SCHEDULER"] = "0"

from app.agent.runner import run_agent  # noqa: E402
from app.auth import hash_key  # noqa: E402
from app.config import AS_OF, iso  # noqa: E402
from app.db import connect, init_db, j, tx  # noqa: E402
from app.ingest.canonicalize import canonicalize  # noqa: E402
from app.ingest.generator import generate_tenant  # noqa: E402
from app.model.train import train_tenant  # noqa: E402

TENANT = "tnt_test"
OTHER = "tnt_other"
KEY = "test_key_alpha"
OTHER_KEY = "test_key_beta"

CFG = {
    "id": TENANT,
    "name": "Test Cloud",
    "slug": "test",
    "api_key": KEY,
    "accounts": 320,
    "months": 24,
    "seed": 4242,
    "config": {
        "auto_send_enabled": False,
        "auto_send_max_arr_cents": 5_000_000,
        "daily_send_cap": 25,
        "per_account_cooldown_days": 7,
        "per_account_monthly_cap": 3,
        "min_coverage_for_outreach": 0.45,
        "kill_switch": False,
        "risk_jump_threshold": 0.15,
        "queue_capacity": 40,
    },
}


def _seed_tenant(conn, tenant_id: str, name: str, key: str, cfg: dict) -> None:
    conn.execute("""INSERT OR REPLACE INTO tenants (id, name, slug, api_key_hash, api_key_hint,
                    config_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                 (tenant_id, name, tenant_id.replace("tnt_", ""), hash_key(key), key[-4:],
                  j(cfg), iso(AS_OF)))
    conn.commit()


@pytest.fixture(scope="session")
def world():
    init_db()
    conn = connect()
    _seed_tenant(conn, TENANT, "Test Cloud", KEY, CFG["config"])
    _seed_tenant(conn, OTHER, "Other Cloud", OTHER_KEY, {**CFG["config"], "auto_send_enabled": True})

    gen = generate_tenant(conn, CFG)
    etl = canonicalize(conn, TENANT, AS_OF)
    trained = train_tenant(conn, TENANT, AS_OF)
    first = run_agent(conn, TENANT, trigger="test", as_of=AS_OF)

    # A second tenant with one account that deliberately reuses an account_id
    # from the first tenant. Queries must still never cross the boundary.
    aid = conn.execute("SELECT account_id FROM dim_accounts WHERE tenant_id=? LIMIT 1", (TENANT,)).fetchone()[0]
    top_aid = conn.execute("""SELECT account_id FROM scores WHERE tenant_id=? AND run_id=?
                              AND json_extract(reason_json, '$.in_horizon') = 1
                              ORDER BY expected_arr_at_risk_cents DESC LIMIT 1""",
                           (TENANT, first["run_id"])).fetchone()[0]
    conn.execute("""INSERT OR REPLACE INTO dim_accounts
                    (tenant_id, account_id, name, domain, segment, industry, region, arr_cents, seats,
                     owner_email, owner_name, renewal_date, contract_start, term_months, status, churned_at, created_at)
                    VALUES (?, ?, 'Other Tenant Co', 'other.example', 'SMB', 'Software', 'EMEA', 100000, 5,
                            'other@vendor.example', 'Other CSM', ?, ?, 12, 'active', NULL, ?)""",
                 (OTHER, aid, iso(AS_OF + timedelta(days=100)), iso(AS_OF - timedelta(days=200)), iso(AS_OF)))
    conn.commit()

    yield {"conn": conn, "cfg": CFG, "gen": gen, "etl": etl, "train": trained,
           "first_run": first, "shared_account_id": aid, "top_account_id": top_aid}

    conn.close()


@pytest.fixture(scope="session")
def client(world):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def auth_headers():
    return {"Authorization": f"Bearer {KEY}"}, {"Authorization": f"Bearer {OTHER_KEY}"}
