"""Run the unattended agent for one or all tenants. Used by cron and by hand."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.runner import run_agent
from app.config import AS_OF, TENANT_CONFIG
from app.db import connect, one


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=None, help="tenant slug; default all")
    ap.add_argument("--as-of", default="now", help="'now' or an ISO timestamp")
    ap.add_argument("--trigger", default="schedule")
    args = ap.parse_args()
    as_of = datetime.now(timezone.utc) if args.as_of == "now" else datetime.fromisoformat(args.as_of)
    conn = connect()
    for cfg in TENANT_CONFIG:
        if args.tenant and cfg["slug"] != args.tenant:
            continue
        if not one(conn, "SELECT 1 FROM tenants WHERE id=?", (cfg["id"],)):
            continue
        r = run_agent(conn, cfg["id"], trigger=args.trigger, as_of=as_of)
        print(f"[{cfg['slug']}] {r['run_id']} as_of={r['as_of']} ingest={r['ingest']['kept']} "
              f"actions={r['actions']}")


if __name__ == "__main__":
    main()
