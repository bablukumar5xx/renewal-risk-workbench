"""Synthetic multi-source customer data.

Realism is the point, so the generator models a latent health process and then
emits *observations* of it per source. That gives the model something real to
learn (and something real to miss). It also injects the failure modes a real
warehouse has: duplicate records, naive timestamps, missing fields, account name
variants, unmatched/orphan rows, and events that arrive after the snapshot that
should not see them.

Deterministic: same seed -> same world.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from ..config import AS_OF, TENANT_CONFIG, iso
from ..db import insert_many, j, tx

SOURCES = ["salesforce", "stripe", "zendesk", "gmail", "product"]

SEGMENTS = [("Enterprise", 0.08), ("Mid-Market", 0.30), ("SMB", 0.62)]
INDUSTRIES = ["SaaS", "Financial Services", "Healthcare", "Retail", "Manufacturing", "Media", "Logistics", "Education"]
REGIONS = ["NA", "EMEA", "APAC", "LATAM"]
FIRST = ["Ava", "Noah", "Mia", "Liam", "Zoe", "Ethan", "Isla", "Owen", "Maya", "Lucas", "Nina", "Arjun",
         "Sofia", "Mateo", "Chloe", "Ravi", "Elena", "Jonas", "Priya", "Sam", "Grace", "Diego", "Hana", "Tom"]
LAST = ["Chen", "Patel", "Garcia", "Kim", "Muller", "Rossi", "Okafor", "Novak", "Silva", "Haddad", "Ivanov",
        "Tanaka", "Dubois", "Anders", "Reyes", "Kaur", "Mensah", "Costa", "Larsen", "Nakamura"]
COMPANY_A = ["Acme", "Northstar", "Bluefin", "Verdant", "Quanta", "Helio", "Meridian", "Cobalt", "Lattice",
             "Orbit", "Pinecrest", "Riverstone", "Sable", "Tessera", "Umbra", "Vantage", "Wren", "Zephyr",
             "Atlas", "Beacon", "Cinder", "Drift", "Ember", "Fable", "Grove", "Harbor", "Indigo", "Juniper",
             "Kestrel", "Lumen", "Mosaic", "Nimbus", "Onyx", "Pivot", "Quarry", "Ridge", "Summit", "Tide",
             "Union", "Vertex"]
COMPANY_B = ["Labs", "Systems", "Health", "Analytics", "Works", "Cloud", "Group", "Technologies", "Partners",
             "Digital", "Software", "Data", "Networks", "Solutions", "Industries", "Media", "Retail", "Logistics"]
COMPANY_C = ["", "Group", "International", "Holdings", "Partners", "Collective", "Ventures", "Global",
             "North", "South", "Europe", "Americas", "Labs", "Technologies", "Services", "Capital",
             "Industries", "Networks", "Digital", "Solutions"]
FEATURES = ["dashboards", "alerts", "api", "exports", "automations", "sso", "audit_log", "forecasting", "integrations"]
CSM_FIRST = ["Jordan", "Priya", "Marcus", "Elena", "Sam", "Nadia", "Chris", "Aisha", "Diego", "Hannah",
             "Omar", "Lena", "Theo", "Maya", "Ben", "Sofia", "Kai", "Ruth", "Adam", "Ines", "Leo"]
CSM_LAST = ["Blake", "Raman", "Cole", "Petrova", "Iyer", "Haddad", "Wong", "Bello", "Ruiz", "Fischer",
            "Aziz", "Berg", "Nolan", "Shah", "Ortiz", "Klein", "Mori", "Diaz", "Fry", "Costa", "Vega"]


def _sigmoid(x: float) -> float:
    if x < -35:
        return 0.0
    if x > 35:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def _weighted(rng: np.random.Generator, values: list[tuple[str, float]]) -> str:
    r = rng.random()
    acc = 0.0
    for v, w in values:
        acc += w
        if r <= acc:
            return v
    return values[-1][0]


def _company_name(rng: np.random.Generator, used: set) -> tuple[str, str]:
    for _ in range(400):
        name = f"{rng.choice(COMPANY_A)} {rng.choice(COMPANY_B)}"
        suffix = str(rng.choice(COMPANY_C))
        if suffix:
            name = f"{name} {suffix}"
        if name not in used:
            used.add(name)
            slug = name.lower().replace(" ", "").replace("&", "and")
            return name, slug
    # deterministic fallback for very large books
    n = len(used)
    name = f"{rng.choice(COMPANY_A)} {rng.choice(COMPANY_B)} {n}"
    used.add(name)
    return name, name.lower().replace(" ", "")


def _domain_variant(domain: str, rng: np.random.Generator) -> str:
    r = rng.random()
    if r < 0.05:
        return "www." + domain
    if r < 0.08:
        return domain.upper()
    return domain


def _name_variant(name: str, rng: np.random.Generator) -> str:
    r = rng.random()
    if r < 0.10:
        return name.upper()
    if r < 0.20:
        return name.replace(" Inc.", ",") + " Inc"
    if r < 0.26:
        return name.split(" ")[0]
    return name


def _biz_time(rng: np.random.Generator, day: datetime) -> datetime:
    """Most human activity lands in business hours, with a tail."""
    hour = int(np.clip(rng.normal(14, 3.2), 6, 23))
    minute = int(rng.integers(0, 60))
    return day.replace(hour=hour, minute=minute, second=int(rng.integers(0, 60)), microsecond=0)


def _add_months(d: datetime, months: int) -> datetime:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30,
                      31, 31, 30, 31, 30, 31][m - 1])
    return d.replace(year=y, month=m, day=day)


class Emitter:
    def __init__(self, tenant_id: str, seed: int):
        self.tenant_id = tenant_id
        self.seed = seed
        self.events: list[tuple] = []
        self.renewals: list[tuple] = []
        self.accounts: list[tuple] = []
        self.contacts: list[tuple] = []
        self._seq = 0
        self._rng = np.random.default_rng(seed)
        # The world is frozen at the Monday cut-off. Using wall-clock "now"
        # here would create events that land in the future, which is not messy
        # data — it is impossible data.
        self.now = AS_OF
        self.late_pool: list[tuple] = []

    # -- identity -------------------------------------------------------
    def next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self.tenant_id.replace('tnt_', '')[:8]}_{self._seq:07d}"

    def add_event(self, source: str, etype: str, natural_key: str, account: dict | None,
                  occurred: datetime, payload: dict, *, delay_seconds: int | None = None,
                  force_account_external: str | None = None, tz_assumed: int = 0,
                  name_raw: str | None = None, domain_raw: str | None = None) -> None:
        if occurred > self.now:
            occurred = self.now - timedelta(minutes=int(self._rng.integers(1, 120)))
        if delay_seconds is None:
            r = self._rng.random()
            if r < 0.85:
                delay_seconds = int(self._rng.integers(60, 6 * 3600))
            elif r < 0.95:
                delay_seconds = int(self._rng.integers(6 * 3600, 3 * 86400))
            else:
                delay_seconds = int(self._rng.integers(3 * 86400, 14 * 86400))
        arrived = occurred + timedelta(seconds=delay_seconds)
        ext = force_account_external if force_account_external is not None else (account or {}).get("domain")
        nm = name_raw if name_raw is not None else (account or {}).get("name")
        dm = domain_raw if domain_raw is not None else (account or {}).get("domain")
        row = (
            self.next_id("raw"), self.tenant_id, source, etype, natural_key, ext, nm, dm,
            iso(occurred), iso(arrived), tz_assumed, j(payload), "batch_initial", None,
            iso(self.now),
        )
        # A held-back tail of recent events lands after the Monday snapshot: this
        # is the late-arrival set used to demonstrate point-in-time correctness.
        # Everything else is clamped to have landed by Monday; nothing arrives
        # in the future.
        if arrived > AS_OF:
            row = list(row)
            if occurred < AS_OF and self._rng.random() < 0.035:
                row[9] = iso(AS_OF + timedelta(days=2, hours=float(self._rng.uniform(0, 20))))
                row[12] = "batch_late_01"
                self.late_pool.append(tuple(row))
            else:
                row[9] = iso(max(occurred + timedelta(minutes=1),
                                 AS_OF - timedelta(seconds=int(self._rng.integers(60, 6 * 3600)))))
                self.events.append(tuple(row))
        else:
            self.events.append(row)

    # -- persistence ----------------------------------------------------
    def flush(self, conn) -> dict:
        # Deterministic late arrivals: recent events whose ingestion timestamp
        # lands after the Monday snapshot. A score computed at AS_OF must not
        # see them; a later run must.
        rng = self._rng
        recent = [i for i, e in enumerate(self.events)
                  if AS_OF - timedelta(days=120) <= datetime.fromisoformat(e[8].replace("Z", "+00:00")) < AS_OF]
        for j, idx in enumerate(recent):
            if j % 37 == 0:
                row = list(self.events[idx])
                row[9] = iso(AS_OF + timedelta(days=2, hours=float(rng.uniform(0, 20))))
                row[12] = "batch_late_01"
                self.late_pool.append(tuple(row))
                self.events[idx] = None  # type: ignore[assignment]
        self.events = [e for e in self.events if e is not None]

        # 5% duplicate rows: same natural key, new raw id, later arrival. The
        # canonicalizer must dedupe on natural key, not on id.
        dups = []
        for row in self.events:
            if rng.random() < 0.045:
                d = list(row)
                d[0] = self.next_id("raw")
                d[11] = row[11]
                d[12] = "batch_initial"
                d[13] = row[0]
                arrived = datetime.fromisoformat(row[9].replace("Z", "+00:00")) + timedelta(
                    seconds=float(rng.integers(60, 3 * 86400)))
                if arrived > AS_OF:
                    arrived = max(datetime.fromisoformat(row[8].replace("Z", "+00:00")) + timedelta(minutes=1),
                                  AS_OF - timedelta(seconds=float(rng.integers(60, 6 * 3600))))
                d[9] = iso(arrived)
                dups.append(tuple(d))
        all_events = self.events + dups + self.late_pool

        with tx(conn):
            insert_many(conn, "dim_accounts", [
                "tenant_id", "account_id", "name", "domain", "segment", "industry", "region", "arr_cents",
                "seats", "owner_email", "owner_name", "renewal_date", "contract_start", "term_months",
                "status", "churned_at", "created_at",
            ], self.accounts)
            insert_many(conn, "contacts", [
                "tenant_id", "contact_id", "account_id", "name", "email", "title", "role",
                "consent_email", "consent_source", "opted_out_at", "is_primary", "active",
            ], self.contacts)
            insert_many(conn, "raw_events", [
                "id", "tenant_id", "source", "event_type", "natural_key", "account_external_id",
                "account_name_raw", "domain_raw", "occurred_at", "arrived_at", "tz_assumed",
                "payload_json", "batch_id", "duplicate_of", "created_at",
            ], all_events)
        by_source: dict[str, int] = {}
        for e in all_events:
            by_source[e[2]] = by_source.get(e[2], 0) + 1
        return {
            "accounts": len(self.accounts),
            "contacts": len(self.contacts),
            "events": len(all_events),
            "duplicates": len(dups),
            "held_back_late": len(self.late_pool),
            "by_source": by_source,
            "renewals": len(self.renewals),
        }

def generate_tenant(conn, cfg: dict) -> dict:
    tenant_id = cfg["id"]
    n = cfg["accounts"]
    months = cfg["months"]
    seed = cfg["seed"]
    rng = np.random.default_rng(seed)
    em = Emitter(tenant_id, seed)

    start = _add_months(AS_OF.replace(hour=0, minute=0, second=0, microsecond=0), -months)
    start = start.replace(day=1)
    month_starts = [_add_months(start, i) for i in range(months + 2)]
    horizon = AS_OF + timedelta(days=430)

    # CSM roster sized to the book: ~120 accounts per CSM.
    n_csm = max(4, round(n / 120))
    csm_names = [f"{CSM_FIRST[i % len(CSM_FIRST)]} {CSM_LAST[(i * 7) % len(CSM_LAST)]}" for i in range(n_csm)]
    csm_emails = [f"{nm.lower().replace(' ', '.')}@vendor.example" for nm in csm_names]

    used_names: set = set()
    account_meta: list[dict] = []
    h_matrix = np.zeros((n, months + 2))
    segment_list: list[str] = []

    for i in range(n):
        name, slug = _company_name(rng, used_names)
        domain = f"{slug}.com"
        segment = _weighted(rng, SEGMENTS)
        segment_list.append(segment)
        if segment == "Enterprise":
            arr = int(np.clip(rng.lognormal(np.log(180_000), 0.55), 90_000, 1_400_000))
            seats = int(np.clip(arr / 900, 90, 2400))
        elif segment == "Mid-Market":
            arr = int(np.clip(rng.lognormal(np.log(52_000), 0.45), 18_000, 180_000))
            seats = int(np.clip(arr / 420, 25, 500))
        else:
            arr = int(np.clip(rng.lognormal(np.log(9_500), 0.55), 2_400, 40_000))
            seats = int(np.clip(arr / 190, 3, 160))
        arr_cents = arr * 100
        owner_idx = int(rng.integers(0, n_csm))
        tenure_months = int(rng.integers(1, months + 1))
        signup = month_starts[max(0, months - tenure_months)] + timedelta(days=int(rng.integers(0, 27)))
        if signup > AS_OF - timedelta(days=20):
            signup = AS_OF - timedelta(days=int(rng.integers(20, 120)))
        term = 3 if rng.random() < 0.16 else 12
        first_renewal = _add_months(signup.replace(day=min(signup.day, 28)), term)
        first_renewal = first_renewal.replace(day=int(rng.integers(1, 27)))
        silent = rng.random() < 0.10
        declining = rng.random() < 0.22
        expanding = rng.random() < 0.12

        h = np.zeros(months + 2)
        h[0] = rng.normal(0.55, 0.95)
        base_drift = -0.055 if declining else (0.045 if expanding else 0.0)
        for m in range(1, months + 2):
            drift = base_drift + rng.normal(0, 0.15)
            h[m] = 0.94 * h[m - 1] + drift
            if rng.random() < 0.020:
                h[m] -= rng.uniform(0.8, 2.0)          # incident / champion leaves / budget freeze
            elif rng.random() < 0.012:
                h[m] += rng.uniform(0.4, 1.0)          # win / expansion
            h[m] = float(np.clip(h[m], -3.0, 3.0))
        h_matrix[i, :] = h

        # champion departure shock, observable in email + contact state
        champion_left_month = None
        if rng.random() < 0.16:
            champion_left_month = int(rng.integers(2, months))
            h[champion_left_month:] -= 0.75
            h_matrix[i, :] = h

        account_meta.append({
            "idx": i, "account_id": f"acc_{i:05d}", "name": name, "domain": domain, "segment": segment,
            "industry": str(rng.choice(INDUSTRIES)), "region": str(rng.choice(REGIONS)),
            "arr": arr_cents, "seats": seats, "owner_idx": owner_idx,
            "owner_name": csm_names[owner_idx], "owner_email": csm_emails[owner_idx],
            "signup": signup, "term": term, "first_renewal": first_renewal, "silent": bool(silent),
            "champion_left_month": champion_left_month, "h": h,
        })

    # --- renewals & churn -------------------------------------------------
    for meta in account_meta:
        i = meta["idx"]
        h = meta["h"]
        arr = meta["arr"]
        term = meta["term"]
        r = meta["first_renewal"]
        churned_at = None
        next_renewal = None
        while r <= horizon:
            m_idx = max(0, min(months + 1, (r.year - start.year) * 12 + (r.month - start.month)))
            hv = float(h[m_idx]) + rng.normal(0, 0.28)
            seg_adj = 0.20 if meta["segment"] == "Enterprise" else (-0.15 if meta["segment"] == "SMB" else 0.0)
            p = _sigmoid(-2.12 - 1.12 * hv + seg_adj)
            # 5% of churn is genuinely unforecastable; 3% of health looks terminal but is renewed.
            if rng.random() < p:
                if rng.random() < 0.03:
                    outcome = "renewed"
                else:
                    outcome = "churned"
            else:
                outcome = "churned" if rng.random() < 0.02 else "renewed"

            if r < AS_OF - timedelta(days=30):
                settled = 1
            else:
                settled = 0
            arr_after = arr
            reason = None
            if outcome == "churned":
                reason = str(rng.choice(["budget_cut", "competitor", "low_adoption", "champion_left",
                                         "support_incident", "consolidation"], p=[.16, .19, .27, .19, .09, .10]))
                arr_after = 0
                churned_at = r
            elif rng.random() < 0.10:
                outcome = "downgraded"
                arr_after = int(arr * float(rng.uniform(0.55, 0.85)))
                reason = "seat_reduction"
            source_fact = f"opp_{tenant_id}_{meta['account_id']}_{r.date()}"
            em.renewals.append((
                tenant_id, f"ren_{tenant_id}_{meta['account_id']}_{r.date()}", meta["account_id"],
                iso(r), arr if outcome != "churned" else arr, arr_after, outcome, reason, settled,
                source_fact,
                iso(AS_OF),
            ))
            if churned_at:
                next_renewal = None
                break
            if arr_after != arr:
                arr = arr_after
            next_renewal = _add_months(r, term)
            r = next_renewal
            if r > horizon:
                break
        meta["churned_at"] = churned_at
        meta["next_renewal"] = next_renewal if not churned_at else None
        meta["arr_now"] = 0 if churned_at else arr

    # --- accounts + contacts ---------------------------------------------
    renewals_by_account: dict[str, list] = {}
    for ren in em.renewals:
        renewals_by_account.setdefault(ren[2], []).append(ren)
    for meta in account_meta:
        meta["renewals"] = renewals_by_account.get(meta["account_id"], [])
        # Point-in-time honesty: a churn simulated for next month is not known
        # on Monday and must not appear in account state.
        visible_churn = meta["churned_at"] is not None and meta["churned_at"] <= AS_OF
        status = "churned" if visible_churn else "active"
        next_renewal = None if visible_churn else (meta["next_renewal"] or meta["churned_at"])
        em.accounts.append((
            tenant_id, meta["account_id"], meta["name"], meta["domain"], meta["segment"], meta["industry"],
            meta["region"], 0 if visible_churn else meta["arr_now"], meta["seats"],
            meta["owner_email"], meta["owner_name"],
            iso(next_renewal) if next_renewal else None, iso(meta["signup"]),
            meta["term"], status, iso(meta["churned_at"]) if visible_churn else None, iso(meta["signup"]),
        ))
        em.contacts.extend(_contacts(em, meta, rng))

    # --- source observations ---------------------------------------------
    for meta in account_meta:
        _emit_account_observations(em, meta, rng, month_starts, start, months)

    stats = em.flush(conn)
    stats["tenant"] = cfg["name"]
    stats["churned"] = sum(1 for a in em.accounts if a[14] == "churned")
    stats["active"] = stats["accounts"] - stats["churned"]
    return stats


def _contacts(em: Emitter, meta: dict, rng: np.random.Generator) -> list[tuple]:
    out = []
    n_contacts = int(rng.integers(2, 6))
    roles = ["champion", "exec_sponsor", "admin", "billing", "user"]
    emails: list[str] = []
    for k in range(n_contacts):
        role = roles[k] if k < len(roles) else "user"
        first = str(rng.choice(FIRST))
        last = str(rng.choice(LAST))
        email = f"{first.lower()}.{last.lower()}@{meta['domain']}"
        emails.append(email)
        consent = 1 if rng.random() > 0.07 else 0
        opted_out = None
        if consent == 0 and rng.random() < 0.5:
            opted_out = iso(meta["signup"] + timedelta(days=int(rng.integers(30, 600))))
        active = 1
        if role == "champion" and meta["champion_left_month"] is not None:
            active = 0
        out.append((
            em.tenant_id, f"ct_{meta['account_id']}_{k}", meta["account_id"],
            f"{first} {last}", email,
            str(rng.choice(["VP Operations", "Head of Data", "COO", "Director of Analytics", "Procurement",
                            "IT Manager", "CFO", "RevOps Lead"])),
            role, consent, "salesforce_contact" if consent else "unknown", opted_out,
            1 if role in ("champion", "exec_sponsor") else 0, active,
        ))
    meta["contact_emails"] = emails or [f"admin@{meta['domain']}"]
    return out


def _emit_account_observations(em: Emitter, meta: dict, rng: np.random.Generator,
                               month_starts: list[datetime], start: datetime, months: int) -> None:
    tenant_id = em.tenant_id
    domain = meta["domain"]
    signup = meta["signup"]
    churn = meta["churned_at"]
    end = churn - timedelta(days=18) if churn else AS_OF
    if end <= signup:
        end = signup + timedelta(days=5)
    silent = meta["silent"]
    h = meta["h"]

    def health_at(dt: datetime) -> float:
        m = max(0, min(months + 1, (dt.year - start.year) * 12 + (dt.month - start.month)))
        return float(h[m])

    # Salesforce: account created + owner changes + renewal opportunities.
    created_payload = {
        "Id": "001" + hashlib.md5(meta["account_id"].encode()).hexdigest()[:12].upper(),
        "Name": meta["name"], "Website": f"https://{domain}", "Segment__c": meta["segment"],
        "Industry": meta["industry"], "Region__c": meta["region"], "ARR__c": meta["arr"] / 100,
        "Seats__c": meta["seats"], "Term__c": meta["term"],
        "Renewal_Date__c": iso(meta["first_renewal"])[:10],
        "Owner": {"Name": meta["owner_name"], "Email": meta["owner_email"]},
        "CreatedDate": iso(signup), "LastModifiedDate": iso(signup),
    }
    if rng.random() < 0.12:
        created_payload["Industry"] = None          # incomplete CRM hygiene
    em.add_event("salesforce", "account.created", f"sf_account:{domain}", meta, signup,
                 created_payload, delay_seconds=int(rng.integers(0, 3600)))

    n_owners = 1 if rng.random() > 0.22 else int(rng.integers(2, 4))
    for k in range(1, n_owners):
        at = signup + timedelta(days=int(rng.integers(90, max(120, (end - signup).days))))
        if at >= end:
            break
        em.add_event("salesforce", "account.owner_changed", f"sf_owner:{domain}:{k}", meta, at, {
            "Id": created_payload["Id"], "Account": {"Website": domain, "Name": meta["name"]},
            "OldOwner": {"Email": meta["owner_email"]},
            "NewOwner": {"Email": f"csm{int(rng.integers(1, 60))}@vendor.example"},
            "LastModifiedDate": iso(at),
        })

    # renewal opportunities (also the label source, materialised by the ETL)
    for ren in meta.get("renewals", []):
        rd = datetime.fromisoformat(ren[3].replace("Z", "+00:00"))
        if rd > em.now:
            continue
        stage = {"renewed": "Closed Won", "downgraded": "Closed Won", "churned": "Closed Lost", "open": "Negotiation"}[ren[6]]
        em.add_event("salesforce", "opportunity.renewal", ren[9], meta, rd + timedelta(hours=4), {
            "Id": "006" + hashlib.md5(ren[1].encode()).hexdigest()[:12].upper(),
            "AccountId": created_payload["Id"],
            "Account": {"Name": meta["name"], "Website": domain},
            "Name": f"{meta['name']} — Renewal {rd.year}",
            "StageName": stage,
            "Amount": ren[5] / 100 if ren[5] is not None else 0,
            "CloseDate": iso(rd)[:10], "Renewal_Date__c": iso(rd)[:10],
            "Lost_Reason__c": ren[7], "Owner": {"Email": meta["owner_email"]},
        }, delay_seconds=int(rng.integers(3600, 5 * 86400)))

    # Stripe: invoice at each renewal, payment or failure, occasional dunning.
    for ren in meta.get("renewals", []):
        rd = datetime.fromisoformat(ren[3].replace("Z", "+00:00"))
        if rd > em.now:
            continue
        amount = ren[4]
        inv_id = "in_" + hashlib.md5((ren[1] + "inv").encode()).hexdigest()[:14]
        failure = (not silent) and rng.random() < (0.10 + (0.10 if health_at(rd) < -0.4 else 0.0))
        base = {
            "id": inv_id, "customer": "cus_" + hashlib.md5(domain.encode()).hexdigest()[:14],
            "customer_email": f"billing@{domain}", "customer_name": _name_variant(meta["name"], rng),
            "amount_due": amount, "currency": "usd", "created": int(rd.timestamp()),
            "period_start": int((rd - timedelta(days=365 if meta["term"] == 12 else 90)).timestamp()),
            "period_end": int(rd.timestamp()),
            "description": None if rng.random() < 0.4 else f"{meta['term']}mo subscription — {meta['name']}",
        }
        em.add_event("stripe", "invoice.created", inv_id, meta, rd - timedelta(days=7),
                     dict(base, status="open"), delay_seconds=int(rng.integers(60, 7200)))
        if failure:
            em.add_event("stripe", "invoice.payment_failed", inv_id + ":fail", meta, rd - timedelta(days=2), dict(
                base, status="open", attempt_count=int(rng.integers(1, 4)),
                next_payment_attempt=int((rd + timedelta(days=3)).timestamp()),
                failure_message=str(rng.choice(["card_declined", "insufficient_funds", "expired_card"])),
            ), delay_seconds=int(rng.integers(60, 3600)))
            if rng.random() < 0.55:
                em.add_event("stripe", "invoice.payment_failed", inv_id + ":retry", meta, rd + timedelta(days=3),
                             dict(base, status="open", attempt_count=3,
                                  next_payment_attempt=int((rd + timedelta(days=6)).timestamp()),
                                  failure_message="card_declined"),
                             delay_seconds=int(rng.integers(60, 3600)))
        else:
            em.add_event("stripe", "invoice.paid", inv_id + ":paid", meta, rd, dict(
                base, status="paid", amount_paid=amount, attempt_count=1,
            ), delay_seconds=int(rng.integers(60, 7200)))
        if rng.random() < 0.05:
            em.add_event("stripe", "payment_method.expiring", f"pm:{domain}:{rd.date()}", meta,
                         rd - timedelta(days=20), {
                             "customer": base["customer"], "customer_email": f"billing@{domain}",
                             "card": {"last4": str(rng.integers(1000, 9999)), "exp_month": 11, "exp_year": 2026},
                         })

    # Product telemetry: weekly rollups; missing weeks are normal.
    week = signup
    while week < end:
        hv = health_at(week)
        p_emit = 0.34 + 0.05 * _sigmoid(hv)
        if rng.random() < p_emit:
            active_users = int(np.clip(
                meta["seats"] * _sigmoid(hv * 1.05 + 0.55) * rng.uniform(0.90, 1.05), 1, meta["seats"]))
            sessions = int(max(1, rng.poisson(active_users * rng.uniform(1.5, 3.2))))
            feats = list(rng.choice(FEATURES, size=int(rng.integers(1, 6)), replace=False))
            occurred = _biz_time(rng, week)
            em.add_event("product", "usage.weekly_rollup", f"usage:{domain}:{week.date()}", meta, occurred, {
                "account": _domain_variant(domain, rng),
                "week_start": int(week.timestamp()),
                "active_users": active_users, "licensed_seats": meta["seats"],
                "sessions": sessions,
                "api_calls": int(sessions * rng.uniform(2, 18)),
                "features": feats, "storage_gb": round(float(rng.uniform(1, 400)), 1),
                "trial": False,
            }, tz_assumed=0)
            if rng.random() < 0.12:
                em.add_event("product", "feature.adopted", f"feat:{domain}:{week.date()}", meta, occurred,
                             {"account": domain, "feature": str(rng.choice(FEATURES)),
                              "first_seen": int(week.timestamp()), "user_count": int(rng.integers(1, 40))})
            if rng.random() < 0.18:
                em.add_event("product", "admin.last_seen", f"admin:{domain}:{week.date()}", meta, occurred,
                             {"account": domain, "email": f"admin@{domain}",
                              "last_seen": int(occurred.timestamp())})
        week += timedelta(days=7)

    # Zendesk: tickets driven by health; silent accounts almost never raise them.
    t = signup
    while t < end:
        hv = health_at(t)
        lam = 0.035 if silent else max(0.02, 0.19 - 0.09 * hv)
        n_tickets = int(rng.poisson(lam))
        for _ in range(n_tickets):
            ticket_dt = _biz_time(rng, t + timedelta(days=int(rng.integers(0, 30))))
            if ticket_dt >= end or ticket_dt > em.now:
                continue
            sev = str(rng.choice(["low", "normal", "high", "urgent"], p=[.40, .38, .17, .05]))
            subject, body, tag = _ticket_text(rng, sev, hv)
            requester = f"user{int(rng.integers(1, 30))}@{domain}"
            ext_override = None
            name_override = None
            domain_override = None
            if rng.random() < 0.015:
                requester = f"someone{int(rng.integers(1, 99))}@{str(rng.choice(['gmail.com', 'outlook.com', 'example.net']))}"
                ext_override = ""
                name_override = "Unknown Organization"
                domain_override = "example.net"
            tz_shift = timedelta(hours=int(rng.integers(-8, 9)))
            tz_assumed = 0
            if rng.random() < 0.05:
                occurred_str = (ticket_dt + tz_shift).strftime("%Y-%m-%dT%H:%M:%S")
                tz_assumed = 1
            else:
                occurred_str = (ticket_dt + tz_shift).isoformat()
            tid = int(100000 + rng.integers(1, 899999))
            em.add_event("zendesk", "ticket.created", f"zd:{tid}", meta, ticket_dt, {
                "id": tid, "subject": subject, "description": body,
                "priority": None if rng.random() < 0.05 else sev,
                "status": str(rng.choice(["open", "pending", "solved"], p=[.35, .15, .5])),
                "created_at": occurred_str,
                "requester": {"email": requester, "name": f"User {tid % 97}"},
                "organization": {"name": name_override or _name_variant(meta["name"], rng)},
                "tags": [tag], "via": "email",
            }, tz_assumed=tz_assumed, domain_raw=domain_override,
               force_account_external=ext_override, name_raw=name_override)
            if rng.random() < 0.55:
                em.add_event("zendesk", "ticket.comment", f"zd:{tid}:c1", meta,
                             ticket_dt + timedelta(hours=float(rng.uniform(1, 40))), {
                                 "ticket_id": tid, "public": True,
                                 "author_email": meta["owner_email"],
                                 "body": "Thanks for flagging — I've pulled in engineering and will update you here.",
                                 "created_at": (ticket_dt + timedelta(hours=3)).isoformat(),
                             })
            if rng.random() < 0.62:
                solved = ticket_dt + timedelta(hours=float(rng.uniform(2, 200)))
                em.add_event("zendesk", "ticket.solved", f"zd:{tid}:solved", meta, solved, {
                    "ticket_id": tid, "satisfaction_score": None if rng.random() < 0.55 else str(
                        rng.choice(["good", "bad", "neutral"], p=[.55, .25, .20])),
                    "resolution_hours": round(float((solved - ticket_dt).total_seconds() / 3600), 1),
                    "solved_at": solved.isoformat(),
                })
        t += timedelta(days=30)

    # Gmail: relationship signal. Outbound cadence is CSM-driven; inbound is a
    # function of health. Champion departure leaves a visible trace.
    contact_emails = meta.get("contact_emails") or [f"admin@{domain}"]
    if rng.random() < 0.34:
        # champion out-of-office / handover
        at = signup + timedelta(days=int(rng.integers(120, max(150, (end - signup).days))))
        if at < end:
            em.add_event("gmail", "message.received", f"gm:ooo:{domain}:{at.date()}", meta, at, {
                "threadId": f"th_{int(rng.integers(1e6, 9e6))}", "id": f"msg_{int(rng.integers(1e9, 9e9))}",
                "internalDate": int(at.timestamp() * 1000),
                "from": contact_emails[0], "to": meta["owner_email"],
                "subject": f"Re: {meta['name']} — next steps",
                "snippet": "I'm no longer with the company. Please reach out to our ops team.",
                "labelIds": ["INBOX"], "direction": "inbound",
            })
            if rng.random() < 0.6:
                meta["champion_left_month"] = meta.get("champion_left_month") or 1

    n_msgs = int(rng.integers(3, 10))
    for k in range(n_msgs):
        span = max(30, int((end - signup).days))
        at = signup + timedelta(days=int(rng.integers(10, span)))
        hv = health_at(at)
        inbound = rng.random() < _sigmoid(hv * 1.05 + 0.05)
        sender = contact_emails[k % len(contact_emails)] if inbound else meta["owner_email"]
        recipient = meta["owner_email"] if inbound else contact_emails[k % len(contact_emails)]
        subject, snippet = _email_text(rng, inbound, hv)
        em.add_event("gmail", "message.received" if inbound else "message.sent",
                     f"gm:{domain}:{k}", meta, at, {
                         "threadId": f"th_{int(rng.integers(1e6, 9e6))}",
                         "id": f"msg_{int(rng.integers(1e9, 9e9))}",
                         "internalDate": int(at.timestamp() * 1000),
                         "from": sender, "to": recipient,
                         "subject": None if rng.random() < 0.03 else subject,
                         "snippet": snippet,
                         "labelIds": ["INBOX"] if inbound else ["SENT"],
                         "direction": "inbound" if inbound else "outbound",
                     }, force_account_external=domain if inbound else None,
                     domain_raw=None if inbound else domain)

    # A few expansion / downgrade subscription events, independent of renewals.
    if rng.random() < 0.16 and not churn:
        at = signup + timedelta(days=int(rng.integers(60, max(90, (end - signup).days))))
        if at < end:
            em.add_event("stripe", "customer.subscription.updated", f"sub:{domain}:{at.date()}", meta, at, {
                "customer": "cus_" + hashlib.md5(domain.encode()).hexdigest()[:14],
                "customer_email": f"billing@{domain}",
                "quantity_before": meta["seats"], "quantity_after": int(meta["seats"] * (
                    1.2 if rng.random() < 0.5 else 0.8)),
                "created": int(at.timestamp()),
            })


def _ticket_text(rng: np.random.Generator, sev: str, hv: float) -> tuple[str, str, str]:
    negatives = [
        ("Dashboard is unusable after the update", "Since Thursday every dashboard times out. This is blocking our Monday reporting.", "performance"),
        ("API returning 500s intermittently", "We're seeing 500s on the bulk endpoint roughly every 20 minutes. Our sync job is failing.", "api"),
        ("SSO login loop", "Half the team cannot sign in. We've tried clearing cookies and a different browser.", "sso"),
        ("Billing charge looks wrong", "We were charged for 40 seats we don't have. Please review invoice before we escalate.", "billing"),
        ("Missing data in exports", "Yesterday's exports are missing the last 6 hours. We have a board deck tomorrow.", "exports"),
    ]
    neutrals = [
        ("How do I set up a scheduled export?", "Trying to get a weekly CSV to our warehouse. Is there a guide?", "howto"),
        ("Question about alert thresholds", "Can thresholds be different per team? Happy to jump on a call.", "alerts"),
        ("Add a user to our workspace", "New analyst starting Monday — can you enable their seat?", "admin"),
        ("Integration with Snowflake", "We're evaluating the Snowflake connector. Documentation link?", "integrations"),
    ]
    positives = [
        ("Thanks for the quick fix", "Everything is working again — appreciate the fast turnaround.", "feedback"),
        ("Feature request: saved views", "Loving the new dashboards. A saved-view option would help the ops team.", "feature_request"),
    ]
    if sev in ("urgent", "high") or hv < -0.7:
        subject, body, tag = negatives[int(rng.integers(0, len(negatives)))]
    elif hv > 0.6:
        subject, body, tag = positives[int(rng.integers(0, len(positives)))]
    else:
        subject, body, tag = neutrals[int(rng.integers(0, len(neutrals)))]
    return subject, body, tag


def _email_text(rng: np.random.Generator, inbound: bool, hv: float) -> tuple[str, str]:
    if inbound:
        if hv < -0.4:
            opts = [
                ("Re: renewal", "We need to discuss the renewal. Usage is down and the team is frustrated with support times."),
                ("Budget review", "Finance has asked us to justify the spend this quarter. Can you send adoption numbers?"),
                ("Re: escalation", "Still waiting on the RCA. Our CTO is asking why we should renew."),
            ]
        else:
            opts = [
                ("Re: QBR", "Thanks for the session — sharing internally. One ask: can we get the usage report?"),
                ("Re: new workspace", "Rolling out to the second team next week. Could you join the kickoff?"),
                ("Quick question", "Can we add SSO for the new subsidiary?"),
            ]
    else:
        opts = [
            ("Checking in", "Wanted to see how the rollout is going and whether the new dashboards are landing."),
            ("Renewal planning", "Starting to look at your renewal. Would a 30-minute business review next week help?"),
            ("Support follow-up", "Following up on the open ticket — engineering has a fix in staging."),
            ("Value recap", "Put together a short summary of what your team shipped with us this quarter."),
        ]
    return opts[int(rng.integers(0, len(opts)))]


def generate_all(conn, only_tenant: str | None = None) -> list[dict]:
    out = []
    for cfg in TENANT_CONFIG:
        if only_tenant and cfg["id"] != only_tenant:
            continue
        out.append(generate_tenant(conn, cfg))
    return out
