"""Tenant resolution and isolation."""
from __future__ import annotations

from tests.conftest import OTHER_KEY, TENANT, KEY


def test_missing_key_is_rejected(client):
    assert client.get("/api/me").status_code == 401


def test_key_resolves_to_exactly_one_tenant(client, auth_headers):
    a, _ = auth_headers
    body = client.get("/api/me", headers=a).json()
    assert body["tenant_id"] == TENANT


def test_account_lookup_cannot_cross_tenants(client, auth_headers, world):
    a, b = auth_headers
    aid = world["shared_account_id"]
    mine = client.get(f"/api/accounts/{aid}", headers=a).json()["account"]
    theirs = client.get(f"/api/accounts/{aid}", headers=b).json()["account"]
    assert mine["name"] != theirs["name"]
    assert theirs["name"] == "Other Tenant Co"


def test_tenant_without_a_run_has_no_queue(client, auth_headers):
    _, b = auth_headers
    r = client.get("/api/queue", headers=b)
    assert r.status_code == 404


def test_queue_only_returns_own_accounts(client, auth_headers, world):
    a, _ = auth_headers
    rows = client.get("/api/queue?limit=200", headers=a).json()["rows"]
    assert rows
    ids = {r["account_id"] for r in rows}
    assert world["top_account_id"] in ids
    names = {r["name"] for r in rows}
    assert "Other Tenant Co" not in names


def test_demo_key_listing_does_not_leak_hashes(client):
    tenants = client.get("/api/demo/tenants").json()["tenants"]
    assert tenants
    assert all("api_key_hash" not in t for t in tenants)
