"""Outbound email transport.

The demo must not send mail by accident, so the default transport is a
side-effect-free simulation that returns a deterministic provider ID. Set
``CHP_EMAIL_TRANSPORT=smtp`` (plus the ``CHP_SMTP_*`` variables) to deliver
through a real SMTP relay. The transport is resolved per call so tests and
demo runs can force simulation without restarting the process.
"""
from __future__ import annotations

import hashlib
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


@dataclass(frozen=True)
class Delivery:
    provider: str
    message_id: str


class DeliveryError(RuntimeError):
    """The configured transport could not accept the message."""


def transport_name() -> str:
    return os.getenv("CHP_EMAIL_TRANSPORT", "simulated").strip().lower() or "simulated"


def send_email(to: str, subject: str, body: str, *, action_id: str, now_s: str) -> Delivery:
    """Deliver one message and return the provider + provider message ID.

    Raises DeliveryError on transport failure so the caller can leave the action
    unsent and surface the error; the outbox row is written only after a
    successful send.
    """
    if transport_name() != "smtp":
        return Delivery("simulated", "sim_" + hashlib.md5((action_id + now_s).encode()).hexdigest()[:16])

    host = os.getenv("CHP_SMTP_HOST", "").strip()
    if not host:
        raise DeliveryError("CHP_EMAIL_TRANSPORT=smtp requires CHP_SMTP_HOST")
    port = int(os.getenv("CHP_SMTP_PORT", "587"))
    user = os.getenv("CHP_SMTP_USER", "")
    password = os.getenv("CHP_SMTP_PASSWORD", "")
    sender = os.getenv("CHP_SMTP_FROM", "") or user or "customer-success@example.com"
    mode = os.getenv("CHP_SMTP_TLS", "starttls").strip().lower()

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="renewal-risk.local")
    msg.set_content(body)

    context = ssl.create_default_context()
    timeout = float(os.getenv("CHP_SMTP_TIMEOUT", "15"))
    try:
        if mode == "ssl":
            client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
        try:
            if mode == "starttls":
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            if user:
                client.login(user, password)
            client.send_message(msg)
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001 - the message is already accepted or the call is failing
                client.close()
    except Exception as exc:  # noqa: BLE001 - normalize transport failures for the API
        raise DeliveryError(f"SMTP delivery failed: {exc}") from exc
    return Delivery("smtp", msg["Message-ID"])
