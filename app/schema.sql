-- Renewal Risk Workbench — SQLite schema.
-- Every tenant-owned table carries tenant_id and every read path filters on it.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tenants (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  slug          TEXT UNIQUE NOT NULL,
  api_key_hash  TEXT NOT NULL,
  api_key_hint  TEXT NOT NULL,
  config_json   TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_accounts (
  tenant_id     TEXT NOT NULL,
  account_id    TEXT NOT NULL,
  name          TEXT NOT NULL,
  domain        TEXT,
  segment       TEXT NOT NULL,
  industry      TEXT,
  region        TEXT,
  arr_cents     INTEGER NOT NULL,
  seats         INTEGER,
  owner_email   TEXT,
  owner_name    TEXT,
  renewal_date  TEXT,
  contract_start TEXT,
  term_months   INTEGER NOT NULL DEFAULT 12,
  status        TEXT NOT NULL DEFAULT 'active',   -- active | churned
  churned_at    TEXT,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, account_id)
);
CREATE INDEX IF NOT EXISTS ix_accounts_owner ON dim_accounts(tenant_id, owner_email);
CREATE INDEX IF NOT EXISTS ix_accounts_status ON dim_accounts(tenant_id, status);

CREATE TABLE IF NOT EXISTS contacts (
  tenant_id     TEXT NOT NULL,
  contact_id    TEXT NOT NULL,
  account_id    TEXT NOT NULL,
  name          TEXT NOT NULL,
  email         TEXT,
  title         TEXT,
  role          TEXT NOT NULL,                    -- champion | exec_sponsor | admin | user | billing
  consent_email INTEGER NOT NULL DEFAULT 1,
  consent_source TEXT,
  opted_out_at  TEXT,
  is_primary    INTEGER NOT NULL DEFAULT 0,
  active        INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, contact_id)
);
CREATE INDEX IF NOT EXISTS ix_contacts_account ON contacts(tenant_id, account_id);

-- Immutable landing zone. Payloads keep the shape of the source system.
CREATE TABLE IF NOT EXISTS raw_events (
  id                 TEXT PRIMARY KEY,
  tenant_id          TEXT NOT NULL,
  source             TEXT NOT NULL,               -- salesforce|stripe|zendesk|gmail|product
  event_type         TEXT NOT NULL,
  natural_key        TEXT NOT NULL,
  account_external_id TEXT,
  account_name_raw   TEXT,
  domain_raw         TEXT,
  occurred_at        TEXT NOT NULL,
  arrived_at         TEXT NOT NULL,
  tz_assumed         INTEGER NOT NULL DEFAULT 0,
  payload_json       TEXT NOT NULL,
  batch_id           TEXT,
  duplicate_of       TEXT,
  created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_raw_tenant_arrived ON raw_events(tenant_id, arrived_at);
CREATE INDEX IF NOT EXISTS ix_raw_natural ON raw_events(tenant_id, natural_key);

-- Thin canonical store. One row per normalised source fact, always linked to raw.
CREATE TABLE IF NOT EXISTS facts (
  id           TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  source       TEXT NOT NULL,
  fact_type    TEXT NOT NULL,
  account_id   TEXT,
  contact_id   TEXT,
  occurred_at  TEXT NOT NULL,
  arrived_at   TEXT NOT NULL,
  amount_cents INTEGER,
  quantity     REAL,
  score        REAL,
  status       TEXT,
  text         TEXT,
  attrs_json   TEXT NOT NULL DEFAULT '{}',
  raw_event_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_facts_account_type ON facts(tenant_id, account_id, fact_type, occurred_at);
CREATE INDEX IF NOT EXISTS ix_facts_arrived ON facts(tenant_id, arrived_at);

-- Materialised renewal ledger derived from Salesforce opportunities.
CREATE TABLE IF NOT EXISTS renewals (
  tenant_id     TEXT NOT NULL,
  renewal_id    TEXT NOT NULL,
  account_id    TEXT NOT NULL,
  renewal_date  TEXT NOT NULL,
  arr_before_cents INTEGER NOT NULL,
  arr_after_cents  INTEGER,
  outcome       TEXT NOT NULL,                    -- renewed | churned | downgraded | open
  churn_reason  TEXT,
  settled       INTEGER NOT NULL DEFAULT 0,
  source_fact_id TEXT,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, renewal_id)
);
CREATE INDEX IF NOT EXISTS ix_renewals_account ON renewals(tenant_id, account_id, renewal_date);

CREATE TABLE IF NOT EXISTS ingest_runs (
  id           TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  batch_id     TEXT NOT NULL,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  rows_read    INTEGER DEFAULT 0,
  rows_kept    INTEGER DEFAULT 0,
  rows_duplicate INTEGER DEFAULT 0,
  rows_orphan  INTEGER DEFAULT 0,
  rows_unmatched INTEGER DEFAULT 0,
  watermark    TEXT,
  status       TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS ingest_issues (
  id           TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  ingest_run_id TEXT,
  raw_event_id TEXT,
  issue_type   TEXT NOT NULL,                     -- orphan | unmatched | missing_field | tz_assumed | duplicate
  detail       TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_issues_type ON ingest_issues(tenant_id, issue_type);

-- Idempotent consumption: a raw event is canonicalised at most once, and one
-- natural key produces at most one canonical fact (duplicates are dropped).
CREATE TABLE IF NOT EXISTS processed_events (
  raw_event_id TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  natural_key  TEXT NOT NULL,
  fact_count   INTEGER NOT NULL,
  processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_keys (
  tenant_id     TEXT NOT NULL,
  natural_key   TEXT NOT NULL,
  raw_event_id  TEXT NOT NULL,
  arrived_at    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, natural_key)
);

CREATE TABLE IF NOT EXISTS model_versions (
  id             TEXT PRIMARY KEY,
  tenant_id      TEXT NOT NULL,
  algorithm      TEXT NOT NULL,
  trained_at     TEXT NOT NULL,
  train_start    TEXT NOT NULL,
  train_end      TEXT NOT NULL,
  calib_start    TEXT,
  calib_end      TEXT,
  test_start     TEXT,
  test_end       TEXT,
  features_json  TEXT NOT NULL,
  hyperparams_json TEXT NOT NULL,
  metrics_json   TEXT NOT NULL,
  artifact_path  TEXT NOT NULL,
  dataset_hash   TEXT NOT NULL,
  code_version   TEXT NOT NULL,
  is_active      INTEGER NOT NULL DEFAULT 0,
  base_rate      REAL
);
CREATE INDEX IF NOT EXISTS ix_models_tenant ON model_versions(tenant_id, trained_at);

CREATE TABLE IF NOT EXISTS agent_runs (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  trigger       TEXT NOT NULL,                    -- schedule | manual | cli
  as_of         TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL,                    -- running | success | failed
  model_version TEXT,
  stats_json    TEXT NOT NULL DEFAULT '{}',
  error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_tenant ON agent_runs(tenant_id, started_at);

CREATE TABLE IF NOT EXISTS agent_steps (
  id         TEXT PRIMARY KEY,
  run_id     TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  name       TEXT NOT NULL,
  status     TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  stats_json TEXT NOT NULL DEFAULT '{}'
);

-- One row per account per scored run. This is the audit record.
CREATE TABLE IF NOT EXISTS scores (
  id             TEXT PRIMARY KEY,
  tenant_id      TEXT NOT NULL,
  run_id         TEXT NOT NULL,
  account_id     TEXT NOT NULL,
  as_of          TEXT NOT NULL,
  model_version  TEXT NOT NULL,
  prob           REAL NOT NULL,
  band           TEXT NOT NULL,                   -- red | yellow | green
  expected_arr_at_risk_cents INTEGER NOT NULL,
  coverage       REAL NOT NULL,
  confidence     TEXT NOT NULL,                   -- high | medium | low
  days_to_renewal INTEGER,
  reason_json    TEXT NOT NULL,
  feature_hash   TEXT NOT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_scores_run ON scores(tenant_id, run_id, prob);
CREATE INDEX IF NOT EXISTS ix_scores_account ON scores(tenant_id, account_id, as_of);

CREATE TABLE IF NOT EXISTS score_features (
  score_id     TEXT NOT NULL,
  feature      TEXT NOT NULL,
  value        REAL,
  imputed      INTEGER NOT NULL DEFAULT 0,
  contribution REAL NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (score_id, feature)
);

CREATE TABLE IF NOT EXISTS actions (
  id             TEXT PRIMARY KEY,
  tenant_id      TEXT NOT NULL,
  account_id     TEXT NOT NULL,
  score_id       TEXT,
  run_id         TEXT,
  play_key       TEXT NOT NULL,
  play_name      TEXT NOT NULL,
  channel        TEXT NOT NULL,                   -- task | email | call
  status         TEXT NOT NULL,                   -- proposed|approved|rejected|sent|blocked|completed
  priority       INTEGER NOT NULL DEFAULT 3,
  due_at         TEXT,
  assigned_to    TEXT,
  trigger_reason TEXT NOT NULL,
  draft_subject  TEXT,
  draft_body     TEXT,
  policy_json    TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT NOT NULL,
  blocked_reason TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  decided_by     TEXT,
  decided_at     TEXT,
  sent_at        TEXT,
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_actions_tenant ON actions(tenant_id, status, priority);
CREATE INDEX IF NOT EXISTS ix_actions_account ON actions(tenant_id, account_id, created_at);

CREATE TABLE IF NOT EXISTS action_events (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  action_id   TEXT NOT NULL,
  at          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  event       TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS outbox (
  id           TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  action_id    TEXT,
  account_id   TEXT NOT NULL,
  to_email     TEXT NOT NULL,
  subject      TEXT NOT NULL,
  body         TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'queued',      -- queued | sent | suppressed | failed
  provider     TEXT NOT NULL DEFAULT 'simulated',
  provider_message_id TEXT,
  sent_at      TEXT,
  meta_json    TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_tenant ON outbox(tenant_id, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  at          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id   TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_audit_tenant ON audit_log(tenant_id, at);
