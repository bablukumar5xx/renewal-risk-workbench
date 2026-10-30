"""Build the demo world end to end: tenants -> data -> ETL -> model -> first agent run."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import AS_OF, DB_PATH, TENANT_CONFIG, ensure_dirs, iso
from app.db import connect, init_db, j, tx
from app.ingest.canonicalize import canonicalize
from app.ingest.generator import generate_tenant
from app.model.train import train_tenant
from app.agent.runner import run_agent


def seed_tenants(conn) -> None:
    from app.auth import hash_key

    with tx(conn):
        for cfg in TENANT_CONFIG:
            conn.execute("""INSERT OR REPLACE INTO tenants (id, name, slug, api_key_hash, api_key_hint,
                            config_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                         (cfg["id"], cfg["name"], cfg["slug"], hash_key(cfg["api_key"]),
                          cfg["api_key"][-4:], j(cfg["config"]), iso(AS_OF)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="delete the existing database first")
    ap.add_argument("--scale", type=float, default=1.0, help="scale account counts (testing)")
    ap.add_argument("--only", default=None, help="tenant slug")
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--skip-agent", action="store_true")
    args = ap.parse_args()

    ensure_dirs()
    if args.fresh and DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()
    init_db()
    conn = connect()
    seed_tenants(conn)

    for cfg in TENANT_CONFIG:
        if args.only and cfg["slug"] != args.only:
            continue
        if conn.execute("SELECT COUNT(*) FROM raw_events WHERE tenant_id=?", (cfg["id"],)).fetchone()[0] > 0:
            print(f"[{cfg['slug']}] raw data already present, skipping generation (use --fresh)")
            continue
        small = dict(cfg)
        small["accounts"] = max(120, int(cfg["accounts"] * args.scale))
        print(f"[{cfg['slug']}] generating {small['accounts']} accounts / {small['months']} months ...")
        t = time.time()
        g = generate_tenant(conn, small)
        print(f"  generated in {time.time()-t:.1f}s: {g['events']:,} events, "
              f"{g['active']:,} active, {g['churned']:,} churned, {g['duplicates']:,} dups, "
              f"{g['held_back_late']:,} held back")

        t = time.time()
        e = canonicalize(conn, cfg["id"], AS_OF)
        print(f"  ETL in {time.time()-t:.1f}s: {e['kept']:,} kept, {e['facts']:,} facts, "
              f"{e['duplicates']:,} dup, {e['unmatched']:,} unmatched")

        if not args.skip_train:
            t = time.time()
            tr = train_tenant(conn, cfg["id"], AS_OF)
            m = tr["metrics"]
            print(f"  model {tr['version']} in {time.time()-t:.1f}s: rows={tr['rows']:,} "
                  f"AUC={m.get('auc')} PR-AUC={m.get('pr_auc')} Brier={m.get('brier')} "
                  f"@90d AUC={m['at_90d'].get('auc')} ECE={m.get('ece')}")
            print(f"    split: {m['split']}")
            print(f"    top-100 precision={m['queue'].get('precision_at_k')} "
                  f"churned-ARR recall={m['queue'].get('churned_arr_recall_at_k')}")

        if not args.skip_agent:
            t = time.time()
            r = run_agent(conn, cfg["id"], trigger="schedule", as_of=AS_OF)
            print(f"  agent run {r['run_id']} in {time.time()-t:.1f}s: {r['actions']}")
    print("done")


if __name__ == "__main__":
    main()
