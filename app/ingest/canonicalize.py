"""Raw -> canonical. The only place that knows source quirks.

Responsibilities, in order:
  1. consume raw events once (idempotent), in arrival order, bounded by `as_of`
  2. drop duplicate natural keys (keeping first arrival)
  3. normalise timestamps (epoch, offset, naive) to UTC
  4. resolve the account: domain -> contact email -> fuzzy name
  5. project into typed `facts`, always with raw_event_id
  6. derive the renewal ledger and update account state
  7. record every data-quality defect we had to work around
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from ..config import iso
from ..db import insert_many, j, one, rows, scalar, tx

SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|gmbh|plc|sa|ag|group)\b", re.I)


def _fact_id(raw_id: str, suffix: str = "") -> str:
    return "f_" + hashlib.md5((raw_id + suffix).encode()).hexdigest()[:18]


def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_domain(s: str | None) -> str | None:
    if not s or not isinstance(s, str):
        return None
    s = s.strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0]
    s = s.split("@")[-1] if "@" in s else s
    s = s[4:] if s.startswith("www.") else s
    return s or None


def parse_dt(value: Any, *, kind: str = "iso") -> tuple[datetime | None, bool]:
    """Return (utc datetime, tz_was_assumed).

    Sources disagree: Salesforce sends Z, Zendesk sends offsets (sometimes
    naive local time), Stripe sends epoch seconds, Gmail epoch milliseconds.
    """
    if value is None or value == "":
        return None, False
    try:
        if kind == "epoch_s":
            return datetime.fromtimestamp(float(value), tz=timezone.utc), False
        if kind == "epoch_ms":
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc), False
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc), True
        return dt.astimezone(timezone.utc), False
    except (ValueError, TypeError, OverflowError, OSError):
        return None, False


class Resolver:
    def __init__(self, conn, tenant_id: str):
        self.tenant_id = tenant_id
        self.domains: dict[str, str] = {}
        self.emails: dict[str, str] = {}
        self.names: dict[str, str] = {}
        for r in rows(conn, "SELECT account_id, name, domain FROM dim_accounts WHERE tenant_id = ?", (tenant_id,)):
            if r["domain"]:
                self.domains[norm_domain(r["domain"]) or ""] = r["account_id"]
            self.names.setdefault(norm_name(r["name"]), r["account_id"])
        for r in rows(conn, "SELECT account_id, email FROM contacts WHERE tenant_id = ? AND email IS NOT NULL",
                      (tenant_id,)):
            self.emails[r["email"].strip().lower()] = r["account_id"]

    def resolve(self, domain_hints: list[str | None], email_hints: list[str | None],
                name_hints: list[str | None]) -> tuple[str | None, str | None, str | None]:
        for d in domain_hints:
            nd = norm_domain(d)
            if nd and nd in self.domains:
                return self.domains[nd], "domain", nd
        for e in email_hints:
            if not e or "@" not in e:
                continue
            ee = e.strip().lower()
            if ee in self.emails:
                return self.emails[ee], "contact_email", ee
            nd = norm_domain(ee)
            if nd and nd in self.domains:
                return self.domains[nd], "domain_from_email", nd
        for n in name_hints:
            nn = norm_name(n)
            if not nn:
                continue
            if nn in self.names:
                return self.names[nn], "name_exact", nn
            best, best_score = None, 0.0
            for cand, acc in self.names.items():
                score = SequenceMatcher(None, nn, cand).ratio()
                if score > best_score:
                    best, best_score = acc, score
            if best and best_score >= 0.93:
                return best, "name_fuzzy", f"{nn}~{best_score:.2f}"
        return None, None, None


def _extract_hints(source: str, p: dict, raw_domain: str | None, raw_ext: str | None) -> tuple[list, list, list]:
    domains: list[str | None] = [raw_domain, raw_ext]
    emails: list[str | None] = []
    names: list[str | None] = []
    if source == "salesforce":
        domains.append(p.get("Website"))
        if isinstance(p.get("Account"), dict):
            domains.append(p["Account"].get("Website"))
            names.append(p["Account"].get("Name"))
        names.append(p.get("Name"))
        if isinstance(p.get("Owner"), dict):
            emails.append(p["Owner"].get("Email"))
    elif source == "stripe":
        emails.append(p.get("customer_email"))
        names.append(p.get("customer_name"))
    elif source == "zendesk":
        req = p.get("requester") or {}
        emails.append(req.get("email"))
        names.append((p.get("organization") or {}).get("name"))
        emails.append(p.get("author_email"))
    elif source == "gmail":
        emails.extend([p.get("from"), p.get("to")])
    elif source == "product":
        domains.append(p.get("account"))
    return domains, emails, names


def _build_facts(source: str, etype: str, p: dict, raw: Any, account_id: str, as_of: datetime,
                 resolver: Resolver) -> tuple[list[tuple], list[dict]]:
    """Return (fact rows, issue dicts)."""
    issues: list[dict] = []
    facts: list[tuple] = []
    now = iso(as_of)

    def add(fact_type: str, occurred: datetime, *, contact_id=None, amount=None, quantity=None,
            score=None, status=None, text=None, attrs=None, suffix="") -> None:
        facts.append((
            _fact_id(raw["id"], suffix), raw["tenant_id"], source, fact_type, account_id, contact_id,
            iso(occurred), raw["arrived_at"], amount, quantity, score, status, text,
            j(attrs or {}), raw["id"],
        ))

    occurred_col = datetime.fromisoformat(raw["occurred_at"].replace("Z", "+00:00"))

    if source == "salesforce":
        if etype == "account.created":
            dt, assumed = parse_dt(p.get("CreatedDate"))
            if assumed:
                issues.append({"type": "tz_assumed", "detail": "salesforce CreatedDate had no timezone"})
            add("account", dt or occurred_col, amount=int((p.get("ARR__c") or 0) * 100),
                quantity=p.get("Seats__c"), text=p.get("Name"),
                attrs={"segment": p.get("Segment__c"), "term": p.get("Term__c"),
                       "industry": p.get("Industry"), "renewal_date": p.get("Renewal_Date__c")})
        elif etype == "account.owner_changed":
            add("owner_change", occurred_col, attrs={"new_owner": (p.get("NewOwner") or {}).get("Email")})
        elif etype == "opportunity.renewal":
            dt, _ = parse_dt(p.get("CloseDate"))
            stage = p.get("StageName") or ""
            outcome = "churned" if stage == "Closed Lost" else ("open" if "Closed" not in stage else "renewed")
            amount = int(float(p.get("Amount") or 0) * 100)
            add("renewal", dt or occurred_col, amount=amount, status=outcome, text=p.get("Name"),
                attrs={"stage": stage, "renewal_date": p.get("Renewal_Date__c"),
                       "lost_reason": p.get("Lost_Reason__c"), "sf_id": p.get("Id")})
        return facts, issues

    if source == "stripe":
        dt, _ = parse_dt(p.get("created"), kind="epoch_s")
        when = dt or occurred_col
        if etype == "invoice.created":
            add("invoice", when, amount=int(p.get("amount_due") or 0), status=p.get("status"),
                attrs={"invoice_id": p.get("id"), "currency": p.get("currency")})
        elif etype == "invoice.payment_failed":
            add("payment_failed", when, amount=int(p.get("amount_due") or 0), status="failed",
                attrs={"invoice_id": p.get("id"), "attempts": p.get("attempt_count"),
                       "failure": p.get("failure_message")})
        elif etype == "invoice.paid":
            add("payment_paid", when, amount=int(p.get("amount_paid") or p.get("amount_due") or 0),
                status="paid", attrs={"invoice_id": p.get("id")})
        elif etype == "payment_method.expiring":
            add("payment_method_expiring", when, attrs=p.get("card") or {})
        elif etype == "customer.subscription.updated":
            before, after = p.get("quantity_before"), p.get("quantity_after")
            add("seat_change", when, quantity=after, attrs={"before": before, "after": after})
        return facts, issues

    if source == "zendesk":
        if etype == "ticket.created":
            dt, assumed = parse_dt(p.get("created_at"))
            if assumed:
                issues.append({"type": "tz_assumed", "detail": "zendesk created_at had no offset"})
            if not p.get("priority"):
                issues.append({"type": "missing_field", "detail": "zendesk ticket.priority is null"})
            add("ticket", dt or occurred_col, status=p.get("status"),
                text=f"{p.get('subject') or ''}\n{p.get('description') or ''}".strip(),
                attrs={"ticket_id": p.get("id"), "priority": p.get("priority"),
                       "tags": p.get("tags"), "requester": (p.get("requester") or {}).get("email"),
                       "subject": p.get("subject")})
        elif etype == "ticket.comment":
            dt, _ = parse_dt(p.get("created_at"))
            add("ticket_comment", dt or occurred_col, text=p.get("body"),
                attrs={"ticket_id": p.get("ticket_id"), "public": p.get("public"),
                       "author": p.get("author_email")})
        elif etype == "ticket.solved":
            dt, _ = parse_dt(p.get("solved_at"))
            add("ticket_solved", dt or occurred_col, score=p.get("resolution_hours"),
                status=p.get("satisfaction_score"), attrs={"ticket_id": p.get("ticket_id")})
        return facts, issues

    if source == "gmail":
        dt, _ = parse_dt(p.get("internalDate"), kind="epoch_ms")
        when = dt or occurred_col
        if not p.get("subject"):
            issues.append({"type": "missing_field", "detail": "gmail subject is null"})
        inbound = p.get("direction") == "inbound"
        add("email_in" if inbound else "email_out", when,
            text=f"{p.get('subject') or ''}\n{p.get('snippet') or ''}".strip(),
            attrs={"thread": p.get("threadId"), "from": p.get("from"), "to": p.get("to"),
                   "counterparty": p.get("from") if inbound else p.get("to"),
                   "subject": p.get("subject")})
        return facts, issues

    if source == "product":
        dt, assumed = parse_dt(p.get("week_start"), kind="epoch_s")
        if etype == "usage.weekly_rollup":
            add("usage", dt or occurred_col, quantity=p.get("active_users"),
                attrs={"sessions": p.get("sessions"), "api_calls": p.get("api_calls"),
                       "features": p.get("features") or [], "licensed_seats": p.get("licensed_seats"),
                       "storage_gb": p.get("storage_gb")})
        elif etype == "feature.adopted":
            add("feature_adopted", dt or occurred_col, text=p.get("feature"),
                attrs={"feature": p.get("feature"), "users": p.get("user_count")})
        elif etype == "admin.last_seen":
            dt2, _ = parse_dt(p.get("last_seen"), kind="epoch_s")
            add("admin_seen", dt2 or occurred_col, attrs={"email": p.get("email")})
        return facts, issues

    return facts, issues


def canonicalize(conn, tenant_id: str, as_of: datetime, ingest_run_id: str | None = None) -> dict:
    run_id = ingest_run_id or "ing_" + hashlib.md5(
        f"{tenant_id}{iso(as_of)}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:14]
    resolver = Resolver(conn, tenant_id)
    as_of_s = iso(as_of)

    raw_rows = rows(conn, """
        SELECT r.* FROM raw_events r
        LEFT JOIN processed_events p ON p.raw_event_id = r.id
        WHERE r.tenant_id = ? AND r.arrived_at <= ? AND p.raw_event_id IS NULL
        ORDER BY r.arrived_at ASC, r.id ASC
    """, (tenant_id, as_of_s))

    stats = {"read": len(raw_rows), "kept": 0, "duplicates": 0, "orphan": 0, "unmatched": 0,
             "facts": 0, "issues": 0, "sources": {}}
    # Records that have physically landed but are dated after the event-time
    # cut-off are held back, not silently included.
    stats["held_back"] = scalar(conn, """
        SELECT COUNT(*) FROM raw_events r
        LEFT JOIN processed_events p ON p.raw_event_id = r.id
        WHERE r.tenant_id = ? AND r.arrived_at > ? AND p.raw_event_id IS NULL""",
        (tenant_id, as_of_s)) or 0
    known_keys = {r["natural_key"]: r["raw_event_id"] for r in rows(
        conn, "SELECT natural_key, raw_event_id FROM canonical_keys WHERE tenant_id = ?", (tenant_id,))}

    fact_rows: list[tuple] = []
    processed: list[tuple] = []
    key_rows: list[tuple] = []
    issue_rows: list[tuple] = []
    dup_updates: list[tuple] = []
    renewal_rows: list[tuple] = []
    touched_accounts: set[str] = set()
    dim_updates: list[tuple] = []

    issue_seq = 0

    def issue(typ: str, raw_id: str, detail: str) -> None:
        nonlocal issue_seq
        issue_seq += 1
        issue_rows.append((f"iss_{tenant_id}_{issue_seq:07d}", tenant_id, run_id, raw_id, typ, detail,
                           iso(as_of)))

    for raw in raw_rows:
        src = raw["source"]
        stats["sources"][src] = stats["sources"].get(src, 0) + 1
        nk = raw["natural_key"]

        if nk in known_keys:
            stats["duplicates"] += 1
            dup_updates.append((known_keys[nk], raw["id"]))
            processed.append((raw["id"], tenant_id, nk, 0, iso(as_of)))
            issue("duplicate", raw["id"], f"natural_key {nk} already canonicalised")
            continue

        try:
            p = json.loads(raw["payload_json"])
        except json.JSONDecodeError:
            issue("missing_field", raw["id"], "payload is not valid JSON")
            processed.append((raw["id"], tenant_id, nk, 0, iso(as_of)))
            continue

        domains, emails, names = _extract_hints(src, p, raw["domain_raw"], raw["account_external_id"])
        account_id, method, detail = resolver.resolve(domains, emails, names)
        if not account_id:
            is_orphan = not any(domains) and not any(emails) and not any(names)
            stats["orphan" if is_orphan else "unmatched"] += 1
            issue("orphan" if is_orphan else "unmatched", raw["id"],
                  f"no account match (method={method}, detail={detail})")
            processed.append((raw["id"], tenant_id, nk, 0, iso(as_of)))
            known_keys[nk] = raw["id"]
            key_rows.append((tenant_id, nk, raw["id"], raw["arrived_at"]))
            continue

        facts, issues = _build_facts(src, raw["event_type"], p, raw, account_id, as_of, resolver)
        for it in issues:
            issue(it["type"], raw["id"], it["detail"])
        for f in facts:
            fact_rows.append(f)
            if f[3] == "renewal":
                renewal_rows.append(f)
                touched_accounts.add(f[4])

        stats["kept"] += 1
        stats["facts"] += len(facts)
        processed.append((raw["id"], tenant_id, nk, len(facts), iso(as_of)))
        known_keys[nk] = raw["id"]
        key_rows.append((tenant_id, nk, raw["id"], raw["arrived_at"]))

    # --- persist ---------------------------------------------------------
    with tx(conn):
        insert_many(conn, "facts", [
            "id", "tenant_id", "source", "fact_type", "account_id", "contact_id", "occurred_at",
            "arrived_at", "amount_cents", "quantity", "score", "status", "text", "attrs_json", "raw_event_id",
        ], fact_rows)
        insert_many(conn, "processed_events", [
            "raw_event_id", "tenant_id", "natural_key", "fact_count", "processed_at"], processed)
        insert_many(conn, "canonical_keys", [
            "tenant_id", "natural_key", "raw_event_id", "arrived_at"], key_rows)
        insert_many(conn, "ingest_issues", [
            "id", "tenant_id", "ingest_run_id", "raw_event_id", "issue_type", "detail", "created_at"],
            issue_rows)
        for canonical_id, dup_id in dup_updates:
            conn.execute("UPDATE raw_events SET duplicate_of = ? WHERE id = ? AND tenant_id = ?",
                         (canonical_id, dup_id, tenant_id))

        # Renewal ledger + account state are derived chronologically from facts,
        # never trusted from the source system's "current" record. Rebuilding is
        # what makes out-of-order arrival safe: a 2024 renewal that lands after a
        # 2025 churn cannot resurrect the account.
        for account_id in touched_accounts:
            _rebuild_account_state(conn, tenant_id, account_id, as_of)

        stats["issues"] = len(issue_rows)
        conn.execute("""INSERT INTO ingest_runs (id, tenant_id, batch_id, started_at, finished_at, rows_read,
                        rows_kept, rows_duplicate, rows_orphan, rows_unmatched, watermark, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'success')""",
                     (run_id, tenant_id, "incremental", iso(as_of), iso(datetime.now(timezone.utc)),
                      stats["read"], stats["kept"], stats["duplicates"], stats["orphan"],
                      stats["unmatched"], as_of_s))
    return {**stats, "ingest_run_id": run_id}


def _rebuild_account_state(conn, tenant_id: str, account_id: str, as_of: datetime) -> None:
    """Recompute the renewal ledger and account state in event-time order."""
    acct = one(conn, "SELECT * FROM dim_accounts WHERE tenant_id = ? AND account_id = ?", (tenant_id, account_id))
    if not acct:
        return
    base = one(conn, """SELECT amount_cents FROM facts WHERE tenant_id=? AND account_id=? AND fact_type='account'
                        ORDER BY occurred_at ASC LIMIT 1""", (tenant_id, account_id))
    arr = int(base["amount_cents"]) if base and base["amount_cents"] else int(acct["arr_cents"])
    term = int(acct["term_months"] or 12)
    facts = rows(conn, """SELECT * FROM facts WHERE tenant_id=? AND account_id=? AND fact_type='renewal'
                          ORDER BY occurred_at ASC, id ASC""", (tenant_id, account_id))
    churned_at = None
    next_renewal = None
    for f in facts:
        occurred_at = f["occurred_at"]
        outcome = f["status"] or "open"
        if outcome == "open":
            continue
        if churned_at:
            break
        amount = int(f["amount_cents"] or 0)
        arr_before = arr
        if outcome == "renewed" and amount and arr_before and amount < int(arr_before * 0.9):
            outcome = "downgraded"
        arr_after = 0 if outcome == "churned" else (amount or arr_before)
        settled = 1 if (datetime.fromisoformat(occurred_at.replace("Z", "+00:00")) + timedelta(days=30)) <= as_of else 0
        attrs = json.loads(f["attrs_json"] or "{}")
        conn.execute("""INSERT OR REPLACE INTO renewals (tenant_id, renewal_id, account_id, renewal_date,
                        arr_before_cents, arr_after_cents, outcome, churn_reason, settled, source_fact_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (tenant_id, f"ren_{account_id}_{occurred_at[:10]}", account_id, occurred_at,
                      arr_before, arr_after, outcome, attrs.get("lost_reason"), settled, f["id"], iso(as_of)))
        arr = arr_after
        if outcome == "churned":
            churned_at = occurred_at
            next_renewal = None
            break
        rd = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        next_renewal = iso(rd + timedelta(days=30 * term))
    if churned_at:
        conn.execute("""UPDATE dim_accounts SET status='churned', churned_at=?, arr_cents=0, renewal_date=NULL
                        WHERE tenant_id=? AND account_id=?""", (churned_at, tenant_id, account_id))
    else:
        conn.execute("""UPDATE dim_accounts SET status='active', churned_at=NULL, arr_cents=?, renewal_date=?
                        WHERE tenant_id=? AND account_id=?""",
                     (arr, next_renewal, tenant_id, account_id))
