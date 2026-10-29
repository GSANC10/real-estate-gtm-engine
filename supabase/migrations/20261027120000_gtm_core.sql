-- Pipeline schema for one brokerage: the master lead list, jobs, results, and outreach identity.
-- Each customer gets its own repository and database, so there is no tenant layer here.

CREATE SCHEMA gtm;

CREATE FUNCTION gtm.reject_update() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'gtm.%: rows are immutable', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END $$;

-- ---------------------------------------------------------------------------
-- Master lead list
-- ---------------------------------------------------------------------------
CREATE TABLE gtm.leads (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  identity_key text NOT NULL UNIQUE,  -- sha256 of the normalized email
  state text NOT NULL DEFAULT 'imported' CHECK (state IN (
    'imported', 'in_progress', 'insufficient_evidence', 'disqualified', 'suppressed',
    'needs_review', 'ready_for_draft', 'draft_created')),
  state_reason text CHECK (state_reason ~ '^[a-z0-9_]+$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE gtm.imports (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_label text NOT NULL,
  file_sha256 text NOT NULL,
  row_count integer NOT NULL CHECK (row_count >= 0),
  outcomes jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Each change to a supplied contact is a new version; old versions never change.
CREATE TABLE gtm.lead_records (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id uuid NOT NULL REFERENCES gtm.leads (id),
  version integer NOT NULL CHECK (version > 0),
  schema_version text NOT NULL,
  content jsonb NOT NULL,
  content_sha256 text NOT NULL,
  import_id uuid REFERENCES gtm.imports (id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (lead_id, version),
  UNIQUE (lead_id, content_sha256)
);

CREATE TABLE gtm.suppressions (
  value_hash text PRIMARY KEY,  -- sha256 of the normalized email
  reason text NOT NULL CHECK (reason ~ '^[a-z0-9_]+$'),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Runs, jobs, results
-- ---------------------------------------------------------------------------
CREATE TABLE gtm.runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  config jsonb NOT NULL,  -- resolved brokerage config, including prompt text; jobs read it from here
  config_sha256 text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused_budget')),
  budget_cap_usd numeric(14, 8) NOT NULL CHECK (budget_cap_usd >= 0),
  spent_usd numeric(14, 8) NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE gtm.jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,  -- first in, first out
  run_id uuid NOT NULL REFERENCES gtm.runs (id),
  lead_id uuid NOT NULL REFERENCES gtm.leads (id),
  lead_record_id uuid NOT NULL REFERENCES gtm.lead_records (id),
  stage text NOT NULL CHECK (stage IN ('research', 'check', 'write', 'draft')),
  job_key text NOT NULL UNIQUE,  -- names the work by its input, so finished work is reused
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'completed', 'blocked', 'failed', 'cancelled')),
  input_result_id uuid,
  outreach_item_id uuid,
  rerun integer NOT NULL DEFAULT 0 CHECK (rerun >= 0),
  attempts integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  available_at timestamptz NOT NULL DEFAULT now(),
  error_code text CHECK (error_code ~ '^[a-z0-9_]+$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  CHECK ((stage = 'research') = (input_result_id IS NULL)),
  CHECK ((stage = 'draft') = (outreach_item_id IS NOT NULL))
);
CREATE INDEX jobs_queued ON gtm.jobs (seq) WHERE status = 'queued';

CREATE TABLE gtm.stage_results (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id uuid NOT NULL REFERENCES gtm.leads (id),
  lead_record_id uuid NOT NULL REFERENCES gtm.lead_records (id),
  job_id uuid NOT NULL UNIQUE REFERENCES gtm.jobs (id),
  kind text NOT NULL CHECK (kind IN ('research', 'checked_research', 'writing')),
  schema_version text NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  parent_result_id uuid REFERENCES gtm.stage_results (id),
  outcome text CHECK (outcome ~ '^[a-z0-9_]+$'),
  reason text CHECK (reason ~ '^[a-z0-9_]+$'),
  content jsonb NOT NULL,
  output_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (lead_id, kind, version)
);
ALTER TABLE gtm.jobs ADD FOREIGN KEY (input_result_id) REFERENCES gtm.stage_results (id);

-- Every model call and tool call is saved before it is sent: exact input, then output and cost.
CREATE TABLE gtm.provider_calls (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES gtm.runs (id),
  job_id uuid NOT NULL REFERENCES gtm.jobs (id),
  stage text NOT NULL,
  job_attempt integer NOT NULL,
  round integer NOT NULL,
  seq integer NOT NULL,
  provider text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('model', 'tool')),
  operation text NOT NULL,
  model_id text,
  input jsonb NOT NULL,
  input_sha256 text NOT NULL,
  output jsonb,
  usage jsonb,
  state text NOT NULL CHECK (state IN ('started', 'succeeded', 'failed', 'uncertain')),
  idempotency_key text NOT NULL,
  request_id text,
  cost_estimate_usd numeric(14, 8) NOT NULL CHECK (cost_estimate_usd >= 0),
  cost_usd numeric(14, 8) CHECK (cost_usd >= 0),
  error_code text CHECK (error_code ~ '^[a-z0-9_]+$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  settled_at timestamptz,
  UNIQUE (provider, idempotency_key)
);

-- One first-touch email per lead: a later writing version can never schedule a second draft.
CREATE TABLE gtm.outreach_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id uuid NOT NULL REFERENCES gtm.leads (id),
  step text NOT NULL CHECK (step IN ('first_touch')),
  writing_result_id uuid NOT NULL REFERENCES gtm.stage_results (id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (lead_id, step)
);
ALTER TABLE gtm.jobs ADD FOREIGN KEY (outreach_item_id) REFERENCES gtm.outreach_items (id);

CREATE TRIGGER lead_records_immutable BEFORE UPDATE ON gtm.lead_records
  FOR EACH ROW EXECUTE FUNCTION gtm.reject_update();
CREATE TRIGGER stage_results_immutable BEFORE UPDATE ON gtm.stage_results
  FOR EACH ROW EXECUTE FUNCTION gtm.reject_update();
