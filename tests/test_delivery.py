"""The outbound transport: safe by default, real when configured."""
from __future__ import annotations

import socket
import threading
import time

from app.delivery import send_email


class FakeSMTP:
    """A tiny in-process SMTP server that captures one message."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self.messages: list[bytes] = []
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        stream = conn.makefile("rwb")

        def reply(line: str):
            stream.write((line + "\r\n").encode())
            stream.flush()

        reply("220 test.local ESMTP")
        data, buf = False, []
        while True:
            raw = stream.readline()
            if not raw:
                break
            if data:
                if raw == b".\r\n":
                    self.messages.append(b"".join(buf))
                    buf, data = [], False
                    reply("250 queued")
                else:
                    buf.append(raw)
                continue
            verb = raw.decode("utf-8", "replace").strip().split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                reply("250-test.local")
                reply("250 SIZE 1048576")
            elif verb in ("MAIL", "RCPT", "RSET", "NOOP"):
                reply("250 ok")
            elif verb == "DATA":
                data = True
                reply("354 end with .")
            elif verb == "QUIT":
                reply("221 bye")
                break
            else:
                reply("250 ok")
        conn.close()

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1)

    def wait(self, timeout=3.0) -> bytes:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.messages:
                return self.messages[0]
            time.sleep(0.02)
        raise AssertionError("SMTP message was not received")


def test_default_transport_is_simulated(monkeypatch):
    monkeypatch.delenv("CHP_EMAIL_TRANSPORT", raising=False)
    out = send_email("ops@example.com", "Subject", "Body", action_id="act_test", now_s="2026-10-30T00:00:00Z")
    assert out.provider == "simulated"
    assert out.message_id.startswith("sim_")


def test_smtp_transport_delivers_the_message(monkeypatch):
    server = FakeSMTP()
    try:
        monkeypatch.setenv("CHP_EMAIL_TRANSPORT", "smtp")
        monkeypatch.setenv("CHP_SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("CHP_SMTP_PORT", str(server.port))
        monkeypatch.setenv("CHP_SMTP_TLS", "none")
        monkeypatch.setenv("CHP_SMTP_FROM", "cs@example.com")
        monkeypatch.delenv("CHP_SMTP_USER", raising=False)

        out = send_email("customer@example.com", "Renewal check-in", "Hello there.",
                         action_id="act_smtp", now_s="2026-10-30T00:00:00Z")
        assert out.provider == "smtp"
        assert "@renewal-risk.local" in out.message_id

        message = server.wait()
        assert b"Subject: Renewal check-in" in message
        assert b"To: customer@example.com" in message
        assert b"From: cs@example.com" in message
        assert b"Hello there." in message
    finally:
        server.close()


def test_smtp_without_host_fails_loudly(monkeypatch):
    monkeypatch.setenv("CHP_EMAIL_TRANSPORT", "smtp")
    monkeypatch.delenv("CHP_SMTP_HOST", raising=False)
    try:
        send_email("ops@example.com", "S", "B", action_id="act_x", now_s="2026-10-30T00:00:00Z")
    except RuntimeError as exc:
        assert "CHP_SMTP_HOST" in str(exc)
    else:
        raise AssertionError("smtp transport must fail when no host is configured")
