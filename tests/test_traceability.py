"""Traceability: every CSM-visible number resolves to a row, a model, and a source."""
from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd

from app.db import one, rows, scalar, unj
from app.features.builder import load_accounts, load_facts, snapshot_features
from app.features.registry import FEATURES
from tests.conftest import OTHER_KEY, TENANT


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_digest_totals_reconcile_with_the_scored_run(client, auth_headers, world):
    headers, _ = auth_headers
    body = client.get("/api/digest", headers=headers).json()
    conn = world["conn"]
    run_id = body["run_id"]

    scores = rows(conn, """SELECT s.*, a.arr_cents FROM scores s
                           JOIN dim_accounts a ON a.tenant_id=s.tenant_id AND a.account_id=s.account_id
                           WHERE s.tenant_id=? AND s.run_id=?""", (TENANT, run_id))
    in_horizon = [s for s in scores if unj(s["reason_json"])["in_horizon"]]
    assert body["totals"]["red"] == sum(1 for s in in_horizon if s["band"] == "red")
    assert body["totals"]["yellow"] == sum(1 for s in in_horizon if s["band"] == "yellow")
    assert body["totals"]["green"] == sum(1 for s in in_horizon if s["band"] == "green")
    assert body["arr_at_risk_cents"] == sum(
        s["expected_arr_at_risk_cents"] for s in in_horizon if s["band"] in ("red", "yellow"))
    assert body["horizon_accounts"] == len(in_horizon)

    book = one(conn, """SELECT COUNT(*) n, COALESCE(SUM(arr_cents),0) arr FROM dim_accounts
                        WHERE tenant_id=? AND status='active'""", (TENANT,))
    assert body["active_accounts"] == book["n"]
    assert body["book_arr_cents"] == book["arr"]

    min_cov = world["cfg"]["config"]["min_coverage_for_outreach"]
    horizon_arr = sum(s["arr_cents"] for s in in_horizon)
    covered_arr = sum(s["arr_cents"] for s in in_horizon if s["coverage"] >= min_cov)
    assert body["coverage_pct"] == round(covered_arr / horizon_arr, 4)

    top = [a["expected_arr_at_risk_cents"] for a in body["accounts"]]
    assert top == sorted(top, reverse=True)
    assert body["run"]["stats"]["ingest"]["kept"] >= 0


def test_queue_rows_are_the_stored_scores(client, auth_headers, world):
    headers, _ = auth_headers
    body = client.get("/api/queue?limit=500&include_outside_horizon=true", headers=headers).json()
    conn = world["conn"]
    assert body["rows"]
    for row in body["rows"]:
        s = one(conn, "SELECT * FROM scores WHERE tenant_id=? AND run_id=? AND account_id=?",
                (TENANT, body["run_id"], row["account_id"]))
        assert s is not None
        assert row["prob"] == round(s["prob"], 4)
        assert row["band"] == s["band"]
        assert row["expected_arr_at_risk_cents"] == s["expected_arr_at_risk_cents"]
        assert row["days_to_renewal"] == s["days_to_renewal"]
        assert row["coverage"] == round(s["coverage"], 2)
        thresholds = unj(s["reason_json"])["band_thresholds"]
        assert row["band"] == ("red" if s["prob"] >= thresholds["red"]
                               else "yellow" if s["prob"] >= thresholds["yellow"] else "green")


def test_queue_shows_the_latest_action_even_when_it_came_from_an_earlier_run(client, auth_headers, world):
    headers, _ = auth_headers
    conn = world["conn"]
    action = one(conn, """SELECT * FROM actions WHERE tenant_id=? AND status='proposed'
                          ORDER BY created_at DESC LIMIT 1""", (TENANT,))
    assert action is not None
    body = client.get(f"/api/queue?search={action['account_id']}&include_outside_horizon=true",
                      headers=headers).json()
    row = next(r for r in body["rows"] if r["account_id"] == action["account_id"])
    assert row["action_status"] == "proposed"
    assert row["action_play"]


def test_each_score_has_a_model_and_a_complete_feature_row(client, auth_headers, world):
    headers, _ = auth_headers
    body = client.get("/api/model/health", headers=headers).json()
    model = body["model"]
    conn = world["conn"]
    run_id = body["latest_run"]["id"]
    scores = rows(conn, "SELECT * FROM scores WHERE tenant_id=? AND run_id=?", (TENANT, run_id))
    assert scores
    for s in scores:
        assert s["model_version"] == model["id"]
        assert scalar(conn, "SELECT COUNT(*) FROM score_features WHERE score_id=?", (s["id"],)) == len(FEATURES)
        assert s["as_of"] == body["latest_run"]["as_of"]
        assert s["expected_arr_at_risk_cents"] >= 0

    version = one(conn, "SELECT * FROM model_versions WHERE tenant_id=? AND id=?", (TENANT, model["id"]))
    assert version["is_active"] == 1
    assert version["dataset_hash"] and version["artifact_path"]


def test_features_recompute_from_raw_facts(client, auth_headers, world):
    headers, _ = auth_headers
    body = client.get("/api/queue?limit=1&include_outside_horizon=true", headers=headers).json()
    account_id = body["rows"][0]["account_id"]
    run = one(world["conn"], "SELECT * FROM agent_runs WHERE tenant_id=? AND id=?",
              (TENANT, body["run_id"]))
    as_of = _iso(run["as_of"])

    accounts = load_accounts(world["conn"], TENANT)
    facts = load_facts(world["conn"], TENANT, as_of)
    expected = snapshot_features(facts, accounts, as_of).loc[account_id]
    stored = {r["feature"]: r["value"] for r in rows(world["conn"], """
        SELECT sf.feature, sf.value FROM score_features sf JOIN scores s ON s.id=sf.score_id
        WHERE s.tenant_id=? AND s.run_id=? AND s.account_id=?""", (TENANT, body["run_id"], account_id))}

    for feature in FEATURES:
        if feature == "days_to_renewal":
            continue
        want, got = expected[feature], stored[feature]
        if want is None or (isinstance(want, float) and np.isnan(want)):
            assert got is None
        else:
            assert got is not None
            assert abs(float(got) - float(want)) < 1e-6, feature

    account = accounts[accounts["account_id"] == account_id].iloc[0]
    days = (account["renewal_date"] - pd.Timestamp(as_of)).total_seconds() / 86400
    assert stored["days_to_renewal"] == int(np.clip(round(days), -30, 330))


def test_fact_lineage_reaches_the_immutable_raw_event(client, auth_headers, world):
    headers, other = auth_headers
    account_id = world["top_account_id"]
    timeline = client.get(f"/api/accounts/{account_id}/timeline?limit=5", headers=headers).json()
    assert timeline["events"]
    fact_id = timeline["events"][0]["id"]

    lineage = client.get(f"/api/lineage/fact/{fact_id}", headers=headers)
    assert lineage.status_code == 200
    body = lineage.json()
    assert body["fact"]["id"] == fact_id
    assert body["raw"]["id"] == body["fact"]["raw_event_id"]
    assert body["raw"]["tenant_id"] == TENANT
    assert body["raw"]["payload"]

    reverse = client.get(f"/api/lineage/raw/{body['raw']['id']}", headers=headers).json()
    assert fact_id in {f["id"] for f in reverse["facts"]}

    # The same identifiers must be invisible to another tenant.
    assert client.get(f"/api/lineage/fact/{fact_id}", headers=other).status_code == 404
    assert client.get(f"/api/lineage/raw/{body['raw']['id']}", headers=other).status_code == 404


def test_no_fact_is_orphaned_from_its_raw_event(world):
    conn = world["conn"]
    orphans = scalar(conn, """
        SELECT COUNT(*) FROM facts f
        LEFT JOIN raw_events r ON r.tenant_id=f.tenant_id AND r.id=f.raw_event_id
        WHERE f.tenant_id=? AND r.id IS NULL""", (TENANT,))
    assert orphans == 0


def test_approval_writes_outbox_events_and_audit(client, auth_headers, world):
    headers, _ = auth_headers
    conn = world["conn"]
    action = one(conn, """SELECT * FROM actions WHERE tenant_id=? AND channel='email' AND status='proposed'
                          ORDER BY created_at DESC LIMIT 1""", (TENANT,))
    assert action is not None

    response = client.post(f"/api/actions/{action['id']}/approve", json={}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "sent"

    outbox = one(conn, "SELECT * FROM outbox WHERE tenant_id=? AND action_id=?", (TENANT, action["id"]))
    assert outbox is not None
    assert outbox["status"] == "sent"
    assert outbox["to_email"]
    meta = unj(outbox["meta_json"])
    assert meta["policy_trace"]
    assert "consent_source" in meta

    audit = one(conn, """SELECT * FROM audit_log WHERE tenant_id=? AND object_id=?
                         AND action='email_approved'""", (TENANT, action["id"]))
    assert audit is not None

    events = {r["event"] for r in rows(conn, "SELECT event FROM action_events WHERE action_id=?", (action["id"],))}
    assert {"created", "approved", "sent"}.issubset(events)

    detail = client.get(f"/api/actions/{action['id']}", headers=headers).json()
    assert detail["action"]["status"] == "sent"
    assert detail["outbox"]
    assert detail["action"]["reasons"]