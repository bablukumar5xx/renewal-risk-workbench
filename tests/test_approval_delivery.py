"""A failed transport must not leave an approval behind."""
from __future__ import annotations

from app.db import one, scalar
from app.delivery import DeliveryError
from tests.conftest import TENANT


def test_failed_transport_leaves_action_proposed(client, auth_headers, world, monkeypatch):
    headers, _ = auth_headers
    conn = world["conn"]
    action = one(conn, """SELECT * FROM actions WHERE tenant_id=? AND channel='email' AND status='proposed'
                          ORDER BY created_at DESC LIMIT 1""", (TENANT,))
    assert action is not None

    def refuse(*args, **kwargs):
        raise DeliveryError("relay refused the message")

    monkeypatch.setattr("app.agent.runner.send_email", refuse)
    response = client.post(f"/api/actions/{action['id']}/approve", json={}, headers=headers)
    assert response.status_code == 502
    assert "relay refused" in response.json()["detail"]

    assert scalar(conn, "SELECT status FROM actions WHERE id=?", (action["id"],)) == "proposed"
    assert scalar(conn, "SELECT COUNT(*) FROM outbox WHERE tenant_id=? AND action_id=?",
                  (TENANT, action["id"])) == 0
    assert scalar(conn, """SELECT COUNT(*) FROM action_events
                           WHERE tenant_id=? AND action_id=? AND event='approved'""",
                  (TENANT, action["id"])) == 0
    assert scalar(conn, """SELECT COUNT(*) FROM audit_log
                           WHERE tenant_id=? AND object_id=? AND action='email_approved'""",
                  (TENANT, action["id"])) == 0

    # The connection must be usable after the rollback.
    assert conn.execute("SELECT 1").fetchone()[0] == 1
