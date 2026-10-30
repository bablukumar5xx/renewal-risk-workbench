"""SQLite access helpers. One connection per thread; WAL so the scheduler and API coexist."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import DB_PATH, ensure_dirs

_local = threading.local()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(str(path or DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect()
        _local.conn = conn
    return conn


def close_thread_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def init_db(path: Path | str | None = None) -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text()
    conn = connect(path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection | None = None):
    c = conn or get_conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


def one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def scalar(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    r = conn.execute(sql, params).fetchone()
    return r[0] if r else None


def insert_many(conn: sqlite3.Connection, table: str, columns: Sequence[str], data: Iterable[Sequence[Any]]) -> int:
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
    n = 0
    cur = conn.cursor()
    for row in data:
        cur.execute(sql, row)
        n += 1
    return n


def j(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def unj(s: str | None, default: Any = None) -> Any:
    if not s:
        return default if default is not None else {}
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}
