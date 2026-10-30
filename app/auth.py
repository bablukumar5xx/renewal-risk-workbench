"""Auth: an API key maps to exactly one tenant. No key, no data.

Deliberately simple for the exercise: a bearer key, hashed at rest, resolved to a
tenant on every request. The important property is that tenant_id never comes
from the request body or URL.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
from typing import Optional

from fastapi import Header, HTTPException

from .db import one, unj


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Principal:
    def __init__(self, tenant_id: str, tenant_name: str, config: dict, key_hint: str, is_admin: bool = True):
        self.tenant_id = tenant_id
        self.tenant_name = tenant_name
        self.config = config
        self.key_hint = key_hint
        self.is_admin = is_admin


def resolve(conn: sqlite3.Connection, api_key: str) -> Optional[Principal]:
    row = one(conn, "SELECT * FROM tenants WHERE api_key_hash = ?", (hash_key(api_key),))
    if not row:
        return None
    return Principal(row["id"], row["name"], unj(row["config_json"]), row["api_key_hint"])


def current_principal(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> Principal:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_api_key:
        token = x_api_key.strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    from .db import get_conn

    p = resolve(get_conn(), token)
    if not p:
        raise HTTPException(status_code=401, detail="invalid API key")
    return p


def require(condition: bool, message: str, status: int = 403) -> None:
    if not condition:
        raise HTTPException(status_code=status, detail=message)
