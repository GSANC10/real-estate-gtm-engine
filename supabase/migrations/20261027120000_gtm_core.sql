-- Core pipeline schema: tenancy, configuration, contacts, jobs, results, and outreach identity.
--
-- Runs as the project's migration role (on Supabase: postgres, which is not a superuser).
-- Runtime roles are created NOLOGIN. Each environment enables login and sets passwords
-- outside migrations (docs/operations.md), so no credential ever lives in this file.

-- ---------------------------------------------------------------------------
-- Roles
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['gtm_worker', 'gtm_app', 'gtm_trigger', 'gtm_claim_owner', 'gtm_enqueue_owner'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS', r);
    END IF;
  END LOOP;
END $$;

-- A non-superuser migration role may assign function ownership only to roles it can SET.
DO $$
BEGIN
  IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
    EXECUTE format('GRANT gtm_claim_owner, gtm_enqueue_owner TO %I WITH INHERIT FALSE, SET TRUE', current_user);
  END IF;
END $$;

CREATE SCHEMA gtm;
REVOKE ALL ON SCHEMA gtm FROM PUBLIC;
GRANT USAGE ON SCHEMA gtm TO gtm_worker, gtm_app, gtm_trigger, gtm_claim_owner, gtm_enqueue_owner;

-- Tenant scope is set transaction-locally by trusted backend code. Missing or empty fails closed.
CREATE FUNCTION gtm.current_tenant() RETURNS uuid
LANGUAGE sql STABLE
AS $$ SELECT nullif(pg_catalog.current_setting('app.tenant_id', true), '')::uuid $$;

CREATE FUNCTION gtm.reject_update() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'gtm.%: rows are immutable', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END $$;

-- ---------------------------------------------------------------------------
-- Tenancy and configuration
-- ---------------------------------------------------------------------------
CREATE TABLE gtm.tenants (
  id uuid PRIMARY KEY,
  slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  name text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('fictional', 'real')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE gtm.senders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES gtm.tenants (id),
  slug text NOT NULL,
  display_name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, slug)
);

CREATE TABLE gtm.mailboxes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES gtm.tenants (id),
  address text NOT NULL,
  mode text NOT NULL DEFAULT 'draft_only' CHECK (mode = 'draft_only'),
  status text NOT NULL DEFAULT 'not_connected'
    CHECK (status IN ('not_connected', 'connected', 'needs_reauth')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, address)
);

CREATE TABLE gtm.campaigns (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES gtm.tenants (id),
  slug text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused')),
  sender_id uuid NOT NULL,
  mailbox_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, slug),
  FOREIGN KEY (tenant_id, sender_id) REFERENCES gtm.senders (tenant_id, id),
  FOREIGN KEY (tenant_id, mailbox_id) REFERENCES gtm.mailboxes (tenant_id, id)
);

CREATE TABLE gtm.config_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES gtm.tenants (id),
  campaign_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  content jsonb NOT NULL,
  content_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, campaign_id, version),
  UNIQUE (tenant_id, campaign_id, content_sha256),
  FOREIGN KEY (tenant_id, campaign_id) REFERENCES gtm.campaigns (tenant_id, id)
);

-- ---------------------------------------------------------------------------
-- Contacts
-- ---------------------------------------------------------------------------
CREATE TABLE gtm.leads (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES gtm.tenants (id),
  identity_key text NOT NULL,  -- M1: sha256 of the normalized supplied email
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, identity_key)
);

CREATE TABLE gtm.lead_records (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  lead_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  schema_version text NOT NULL,
  content jsonb NOT NULL,
  content_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, lead_id, id),
  UNIQUE (tenant_id, lead_id, version),
  UNIQUE (tenant_id, lead_id, content_sha256),
  FOREIGN KEY (tenant_id, lead_id) REFERENCES gtm.leads (tenant_id, id)
);

CREATE TABLE gtm.contact_imports (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  campaign_id uuid NOT NULL,
  source_label text NOT NULL,
  file_sha256 text NOT NULL,
  row_count integer NOT NULL CHECK (row_count >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, campaign_id) REFERENCES gtm.campaigns (tenant_id, id)
);

CREATE TABLE gtm.import_rows (
  tenant_id uuid NOT NULL,
  import_id uuid NOT NULL,
  row_number integer NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('imported', 'duplicate_in_file', 'suppressed', 'invalid')),
  lead_id uuid,
  lead_record_id uuid,
  error_code text CHECK (error_code ~ '^[a-z0-9_]+$'),
  PRIMARY KEY (tenant_id, import_id, row_number),
  FOREIGN KEY (tenant_id, import_id) REFERENCES gtm.contact_imports (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id, lead_record_id) REFERENCES gtm.lead_records (tenant_id, lead_id, id),
  CHECK ((outcome = 'imported') = (lead_record_id IS NOT NULL))
);

CREATE TABLE gtm.campaign_leads (
  tenant_id uuid NOT NULL,
  campaign_id uuid NOT NULL,
  lead_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'imported' CHECK (state IN (
    'imported', 'in_progress', 'insufficient_evidence', 'disqualified', 'suppressed',
    'needs_review', 'ready_for_draft', 'draft_created')),
  state_reason text CHECK (state_reason ~ '^[a-z0-9_]+$'),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, campaign_id, lead_id),
  FOREIGN KEY (tenant_id, campaign_id) REFERENCES gtm.campaigns (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id) REFERENCES gtm.leads (tenant_id, id)
);

CREATE TABLE gtm.suppressions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES gtm.tenants (id),
  kind text NOT NULL CHECK (kind IN ('email')),
  value_hash text NOT NULL,
  reason text NOT NULL CHECK (reason ~ '^[a-z0-9_]+$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, kind, value_hash)
);

-- ---------------------------------------------------------------------------
-- Execution
-- ---------------------------------------------------------------------------
CREATE TABLE gtm.runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  campaign_id uuid NOT NULL,
  import_id uuid NOT NULL,
  trigger_id text NOT NULL CHECK (length(trigger_id) BETWEEN 1 AND 200),
  config_version_id uuid NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused_budget')),
  budget_cap_usd numeric(14, 8) NOT NULL CHECK (budget_cap_usd >= 0),
  settled_usd numeric(14, 8) NOT NULL DEFAULT 0 CHECK (settled_usd >= 0),
  reserved_usd numeric(14, 8) NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
  uncertain_usd numeric(14, 8) NOT NULL DEFAULT 0 CHECK (uncertain_usd >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, trigger_id),
  FOREIGN KEY (tenant_id, campaign_id) REFERENCES gtm.campaigns (tenant_id, id),
  FOREIGN KEY (tenant_id, import_id) REFERENCES gtm.contact_imports (tenant_id, id),
  FOREIGN KEY (tenant_id, config_version_id) REFERENCES gtm.config_versions (tenant_id, id)
);

CREATE TABLE gtm.jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  campaign_id uuid NOT NULL,
  run_id uuid NOT NULL,
  lead_id uuid NOT NULL,
  lead_record_id uuid NOT NULL,
  stage text NOT NULL CHECK (stage IN ('research', 'check', 'write', 'draft')),
  job_key text NOT NULL,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'completed', 'blocked', 'failed', 'cancelled')),
  input_result_id uuid,
  input_result_version integer,
  outreach_item_id uuid,
  config_version_id uuid NOT NULL,
  prompt_version text,
  prompt_sha256 text,
  model_id text,
  rerun integer NOT NULL DEFAULT 0 CHECK (rerun >= 0),
  attempts integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  available_at timestamptz NOT NULL DEFAULT now(),
  leased_by uuid,
  lease_expires_at timestamptz,
  fencing_token bigint NOT NULL DEFAULT 0,
  error_code text CHECK (error_code ~ '^[a-z0-9_]+$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, job_key),
  CHECK ((stage = 'research') = (input_result_id IS NULL)),
  CHECK ((input_result_id IS NULL) = (input_result_version IS NULL)),
  CHECK ((stage = 'draft') = (outreach_item_id IS NOT NULL)),
  FOREIGN KEY (tenant_id, campaign_id) REFERENCES gtm.campaigns (tenant_id, id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES gtm.runs (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id, lead_record_id) REFERENCES gtm.lead_records (tenant_id, lead_id, id),
  FOREIGN KEY (tenant_id, config_version_id) REFERENCES gtm.config_versions (tenant_id, id)
);
CREATE INDEX jobs_claimable ON gtm.jobs (available_at, created_at) WHERE status IN ('queued', 'running');

CREATE FUNCTION gtm.reject_pinned_job_change() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF (NEW.tenant_id, NEW.campaign_id, NEW.run_id, NEW.lead_id, NEW.lead_record_id, NEW.stage,
      NEW.job_key, NEW.input_result_id, NEW.input_result_version, NEW.outreach_item_id,
      NEW.config_version_id, NEW.prompt_version, NEW.prompt_sha256, NEW.model_id, NEW.rerun)
     IS DISTINCT FROM
     (OLD.tenant_id, OLD.campaign_id, OLD.run_id, OLD.lead_id, OLD.lead_record_id, OLD.stage,
      OLD.job_key, OLD.input_result_id, OLD.input_result_version, OLD.outreach_item_id,
      OLD.config_version_id, OLD.prompt_version, OLD.prompt_sha256, OLD.model_id, OLD.rerun) THEN
    RAISE EXCEPTION 'gtm.jobs: pinned inputs cannot change' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER jobs_pinned_inputs BEFORE UPDATE ON gtm.jobs
  FOR EACH ROW EXECUTE FUNCTION gtm.reject_pinned_job_change();

CREATE TABLE gtm.run_leads (
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  lead_id uuid NOT NULL,
  lead_record_id uuid NOT NULL,
  research_job_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, run_id, lead_id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES gtm.runs (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id, lead_record_id) REFERENCES gtm.lead_records (tenant_id, lead_id, id),
  FOREIGN KEY (tenant_id, research_job_id) REFERENCES gtm.jobs (tenant_id, id)
);

CREATE TABLE gtm.context_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  job_id uuid NOT NULL,
  snapshot_version integer NOT NULL DEFAULT 1 CHECK (snapshot_version > 0),
  schema_version text NOT NULL,
  role text NOT NULL,
  payload jsonb NOT NULL,
  payload_sha256 text NOT NULL,
  prompt_version text,
  prompt_sha256 text,
  model_id text,
  tool_schema_version text,
  config_version_id uuid NOT NULL,
  input_refs jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, job_id, snapshot_version),
  FOREIGN KEY (tenant_id, job_id) REFERENCES gtm.jobs (tenant_id, id),
  FOREIGN KEY (tenant_id, config_version_id) REFERENCES gtm.config_versions (tenant_id, id)
);

CREATE TABLE gtm.provider_calls (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  run_id uuid NOT NULL,
  job_id uuid NOT NULL,
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
  state text NOT NULL CHECK (state IN ('reserved', 'succeeded', 'failed', 'uncertain')),
  idempotency_key text NOT NULL,
  request_id text,
  cost_reserved_usd numeric(14, 8) NOT NULL CHECK (cost_reserved_usd >= 0),
  cost_settled_usd numeric(14, 8) CHECK (cost_settled_usd >= 0),
  error_code text CHECK (error_code ~ '^[a-z0-9_]+$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  settled_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, provider, idempotency_key),
  FOREIGN KEY (tenant_id, run_id) REFERENCES gtm.runs (tenant_id, id),
  FOREIGN KEY (tenant_id, job_id) REFERENCES gtm.jobs (tenant_id, id)
);

-- ---------------------------------------------------------------------------
-- Results (immutable)
-- ---------------------------------------------------------------------------
CREATE TABLE gtm.stage_results (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  campaign_id uuid NOT NULL,
  lead_id uuid NOT NULL,
  lead_record_id uuid NOT NULL,
  job_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('research', 'checked_research', 'writing')),
  schema_version text NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  parent_result_id uuid,
  config_version_id uuid NOT NULL,
  context_snapshot_id uuid NOT NULL,
  outcome text CHECK (outcome ~ '^[a-z0-9_]+$'),
  content jsonb NOT NULL,
  output_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, lead_id, id),
  UNIQUE (tenant_id, lead_id, kind, version),
  UNIQUE (tenant_id, job_id),
  FOREIGN KEY (tenant_id, campaign_id) REFERENCES gtm.campaigns (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id, lead_record_id) REFERENCES gtm.lead_records (tenant_id, lead_id, id),
  FOREIGN KEY (tenant_id, job_id) REFERENCES gtm.jobs (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id, parent_result_id) REFERENCES gtm.stage_results (tenant_id, lead_id, id),
  FOREIGN KEY (tenant_id, config_version_id) REFERENCES gtm.config_versions (tenant_id, id),
  FOREIGN KEY (tenant_id, context_snapshot_id) REFERENCES gtm.context_snapshots (tenant_id, id)
);

ALTER TABLE gtm.jobs
  ADD FOREIGN KEY (tenant_id, lead_id, input_result_id) REFERENCES gtm.stage_results (tenant_id, lead_id, id);

CREATE TABLE gtm.citations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  research_result_id uuid NOT NULL,
  key text NOT NULL CHECK (key ~ '^C[0-9]{1,3}$'),
  source text NOT NULL CHECK (source IN ('import_record', 'web')),
  url text,
  quote text NOT NULL,
  source_date date,
  date_basis text NOT NULL CHECK (date_basis IN ('recorded_at', 'published_at', 'undated')),
  identifiers_seen jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, research_result_id, key),
  CHECK ((source = 'web') = (url IS NOT NULL)),
  FOREIGN KEY (tenant_id, research_result_id) REFERENCES gtm.stage_results (tenant_id, id)
);

CREATE TABLE gtm.citation_checks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  checked_result_id uuid NOT NULL,
  citation_id uuid NOT NULL,
  finding_key text NOT NULL,
  identity text NOT NULL CHECK (identity IN ('pass', 'fail', 'ambiguous')),
  recency text NOT NULL CHECK (recency IN ('pass', 'stale', 'undated')),
  meaning text NOT NULL CHECK (meaning IN ('pass', 'fail', 'unclear', 'not_checked')),
  method jsonb NOT NULL,
  passed boolean NOT NULL,
  reasons text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, checked_result_id, citation_id, finding_key),
  FOREIGN KEY (tenant_id, checked_result_id) REFERENCES gtm.stage_results (tenant_id, id),
  FOREIGN KEY (tenant_id, citation_id) REFERENCES gtm.citations (tenant_id, id)
);

CREATE TABLE gtm.qualification_decisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  campaign_id uuid NOT NULL,
  lead_id uuid NOT NULL,
  checked_result_id uuid NOT NULL,
  decision text NOT NULL
    CHECK (decision IN ('qualified', 'insufficient_evidence', 'disqualified', 'needs_review')),
  service_area text NOT NULL CHECK (service_area IN ('met', 'not_met', 'unknown')),
  property_need text NOT NULL CHECK (property_need IN ('met', 'not_met', 'unknown')),
  reasons text[] NOT NULL DEFAULT '{}',
  proposed_fit jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, checked_result_id),
  FOREIGN KEY (tenant_id, campaign_id) REFERENCES gtm.campaigns (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id, checked_result_id) REFERENCES gtm.stage_results (tenant_id, lead_id, id)
);

-- ---------------------------------------------------------------------------
-- Outreach identity
-- ---------------------------------------------------------------------------
CREATE TABLE gtm.outreach_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  campaign_id uuid NOT NULL,
  lead_id uuid NOT NULL,
  step text NOT NULL CHECK (step IN ('first_touch')),
  writing_result_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, campaign_id, lead_id, step),
  FOREIGN KEY (tenant_id, campaign_id) REFERENCES gtm.campaigns (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id, writing_result_id) REFERENCES gtm.stage_results (tenant_id, lead_id, id)
);

ALTER TABLE gtm.jobs
  ADD FOREIGN KEY (tenant_id, outreach_item_id) REFERENCES gtm.outreach_items (tenant_id, id);

-- ---------------------------------------------------------------------------
-- Operations (no tenant data)
-- ---------------------------------------------------------------------------
CREATE TABLE gtm.workers (
  id uuid PRIMARY KEY,
  hostname text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Immutable records
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['config_versions', 'lead_records', 'context_snapshots', 'stage_results',
                           'citations', 'citation_checks', 'qualification_decisions'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON gtm.%I FOR EACH ROW EXECUTE FUNCTION gtm.reject_update()',
                   t || '_immutable', t);
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- Row level security: forced on every tenant table, scoped by gtm.current_tenant()
-- ---------------------------------------------------------------------------
ALTER TABLE gtm.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE gtm.tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON gtm.tenants TO gtm_worker, gtm_app, gtm_enqueue_owner
  USING (id = gtm.current_tenant()) WITH CHECK (id = gtm.current_tenant());

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['senders', 'mailboxes', 'campaigns', 'config_versions', 'leads', 'lead_records',
                           'contact_imports', 'import_rows', 'campaign_leads', 'suppressions', 'runs',
                           'jobs', 'run_leads', 'context_snapshots', 'provider_calls', 'stage_results',
                           'citations', 'citation_checks', 'qualification_decisions', 'outreach_items'] LOOP
    EXECUTE format('ALTER TABLE gtm.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE gtm.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_scope ON gtm.%I TO gtm_worker, gtm_app, gtm_enqueue_owner '
                   'USING (tenant_id = gtm.current_tenant()) WITH CHECK (tenant_id = gtm.current_tenant())', t);
  END LOOP;
END $$;

-- The claim function scans queue state across tenants. Its owner sees only the job columns
-- granted below (IDs, stage, status, lease), never contact content or model input.
CREATE POLICY claim_scan ON gtm.jobs FOR SELECT TO gtm_claim_owner USING (true);
CREATE POLICY claim_lease ON gtm.jobs FOR UPDATE TO gtm_claim_owner USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- Functions
-- ---------------------------------------------------------------------------

-- Job keys name work by its inputs: stage, input reference, and a fingerprint of the
-- stage-relevant configuration (prompts, models, limits, policies) plus the rerun number.
CREATE FUNCTION gtm.job_key(p_config_version_id uuid, p_stage text, p_input_ref uuid, p_rerun integer)
RETURNS text
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
  v_stage_fp text;
BEGIN
  IF p_input_ref IS NULL OR p_rerun IS NULL OR p_rerun < 0 THEN
    RAISE EXCEPTION 'job_key: invalid input reference or rerun' USING ERRCODE = '22023';
  END IF;
  IF p_stage = 'draft' THEN
    RETURN 'draft/' || p_input_ref::text;
  END IF;
  SELECT cv.content #>> ARRAY['fingerprints', p_stage] INTO v_stage_fp
    FROM gtm.config_versions cv WHERE cv.id = p_config_version_id;
  IF v_stage_fp IS NULL THEN
    RAISE EXCEPTION 'job_key: no % fingerprint for configuration version', p_stage USING ERRCODE = '22023';
  END IF;
  RETURN p_stage || '/' || p_input_ref::text || '/'
    || left(encode(sha256(convert_to(v_stage_fp || '|rerun=' || p_rerun::text, 'UTF8')), 'hex'), 24);
END $$;

CREATE FUNCTION gtm.claim_job(p_worker_id uuid, p_stages text[], p_lease_seconds integer)
RETURNS TABLE (job_id uuid, tenant_id uuid, stage text, attempt integer, fencing_token bigint,
               lease_expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
#variable_conflict use_column
DECLARE
  v_id uuid;
BEGIN
  IF p_worker_id IS NULL OR p_stages IS NULL OR p_lease_seconds IS NULL
     OR p_lease_seconds < 5 OR p_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'claim_job: invalid arguments' USING ERRCODE = '22023';
  END IF;

  SELECT j.id INTO v_id
    FROM gtm.jobs j
   WHERE j.stage = ANY (p_stages)
     AND ((j.status = 'queued' AND j.available_at <= now())
          OR (j.status = 'running' AND j.lease_expires_at < now()))
   ORDER BY j.available_at, j.created_at, j.id
   LIMIT 1
   FOR UPDATE SKIP LOCKED;

  IF v_id IS NULL THEN
    RETURN;
  END IF;

  RETURN QUERY
  UPDATE gtm.jobs j
     SET status = 'running',
         attempts = j.attempts + 1,
         leased_by = p_worker_id,
         lease_expires_at = now() + make_interval(secs => p_lease_seconds),
         fencing_token = j.fencing_token + 1,
         updated_at = now()
   WHERE j.id = v_id
  RETURNING j.id, j.tenant_id, j.stage, j.attempts, j.fencing_token, j.lease_expires_at;
END $$;

-- Starts a run for one import. Calling again with the same trigger_id returns the same run.
-- Research jobs are keyed by their inputs, so unchanged leads reuse completed work.
CREATE FUNCTION gtm.enqueue_run(p_tenant_id uuid, p_campaign_id uuid, p_import_id uuid,
                                p_trigger_id text, p_rerun integer DEFAULT 0)
RETURNS TABLE (run_id uuid, created boolean, leads_enqueued integer, jobs_created integer,
               jobs_reused integer, leads_skipped integer)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
#variable_conflict use_column
DECLARE
  v_prior text := current_setting('app.tenant_id', true);
  v_run gtm.runs%ROWTYPE;
  v_config gtm.config_versions%ROWTYPE;
  v_row record;
  v_key text;
  v_job_id uuid;
  v_enqueued integer := 0;
  v_created integer := 0;
  v_reused integer := 0;
  v_skipped integer := 0;
BEGIN
  IF p_tenant_id IS NULL OR p_campaign_id IS NULL OR p_import_id IS NULL
     OR coalesce(p_trigger_id, '') = '' OR p_rerun IS NULL OR p_rerun < 0 THEN
    RAISE EXCEPTION 'enqueue_run: missing or invalid identifier' USING ERRCODE = '22023';
  END IF;
  PERFORM set_config('app.tenant_id', p_tenant_id::text, true);

  -- Parent relationships are validated inside the tenant scope.
  PERFORM 1 FROM gtm.campaigns c WHERE c.id = p_campaign_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'enqueue_run: campaign not found for tenant' USING ERRCODE = '42704';
  END IF;
  PERFORM 1 FROM gtm.contact_imports i WHERE i.id = p_import_id AND i.campaign_id = p_campaign_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'enqueue_run: import not found for campaign' USING ERRCODE = '42704';
  END IF;

  SELECT * INTO v_run FROM gtm.runs r WHERE r.trigger_id = p_trigger_id;
  IF FOUND THEN
    IF v_run.campaign_id <> p_campaign_id OR v_run.import_id <> p_import_id THEN
      RAISE EXCEPTION 'enqueue_run: trigger_id already used for other inputs' USING ERRCODE = '23505';
    END IF;
    PERFORM set_config('app.tenant_id', coalesce(v_prior, ''), true);
    RETURN QUERY SELECT v_run.id, false,
      (SELECT count(*)::integer FROM gtm.run_leads rl WHERE rl.tenant_id = p_tenant_id AND rl.run_id = v_run.id),
      0, 0, 0;
    RETURN;
  END IF;

  SELECT * INTO v_config FROM gtm.config_versions cv
   WHERE cv.campaign_id = p_campaign_id ORDER BY cv.version DESC LIMIT 1;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'enqueue_run: campaign has no configuration version' USING ERRCODE = '42704';
  END IF;

  INSERT INTO gtm.runs (tenant_id, campaign_id, import_id, trigger_id, config_version_id, budget_cap_usd)
  VALUES (p_tenant_id, p_campaign_id, p_import_id, p_trigger_id, v_config.id,
          (v_config.content #>> '{campaign,budget_cap_usd}')::numeric)
  RETURNING * INTO v_run;

  FOR v_row IN
    SELECT ir.lead_id, ir.lead_record_id, l.identity_key
      FROM gtm.import_rows ir
      JOIN gtm.leads l ON l.tenant_id = ir.tenant_id AND l.id = ir.lead_id
     WHERE ir.import_id = p_import_id AND ir.outcome = 'imported'
     ORDER BY ir.row_number
  LOOP
    INSERT INTO gtm.campaign_leads (tenant_id, campaign_id, lead_id)
    VALUES (p_tenant_id, p_campaign_id, v_row.lead_id)
    ON CONFLICT DO NOTHING;

    IF EXISTS (SELECT 1 FROM gtm.suppressions s WHERE s.kind = 'email' AND s.value_hash = v_row.identity_key) THEN
      UPDATE gtm.campaign_leads cl SET state = 'suppressed', state_reason = 'suppressed', updated_at = now()
       WHERE cl.campaign_id = p_campaign_id AND cl.lead_id = v_row.lead_id;
      v_skipped := v_skipped + 1;
      CONTINUE;
    END IF;

    v_key := gtm.job_key(v_config.id, 'research', v_row.lead_record_id, p_rerun);
    v_job_id := NULL;
    INSERT INTO gtm.jobs (tenant_id, campaign_id, run_id, lead_id, lead_record_id, stage, job_key,
                          config_version_id, prompt_version, prompt_sha256, model_id, rerun)
    VALUES (p_tenant_id, p_campaign_id, v_run.id, v_row.lead_id, v_row.lead_record_id, 'research', v_key,
            v_config.id,
            v_config.content #>> '{roles,researcher,prompt_version}',
            v_config.content #>> '{prompts,researcher,sha256}',
            v_config.content #>> '{roles,researcher,model_id}',
            p_rerun)
    ON CONFLICT (tenant_id, job_key) DO NOTHING
    RETURNING id INTO v_job_id;

    IF v_job_id IS NULL THEN
      SELECT j.id INTO v_job_id FROM gtm.jobs j WHERE j.job_key = v_key;
      v_reused := v_reused + 1;
    ELSE
      v_created := v_created + 1;
      UPDATE gtm.campaign_leads cl SET state = 'in_progress', state_reason = NULL, updated_at = now()
       WHERE cl.campaign_id = p_campaign_id AND cl.lead_id = v_row.lead_id;
    END IF;

    INSERT INTO gtm.run_leads (tenant_id, run_id, lead_id, lead_record_id, research_job_id)
    VALUES (p_tenant_id, v_run.id, v_row.lead_id, v_row.lead_record_id, v_job_id);
    v_enqueued := v_enqueued + 1;
  END LOOP;

  PERFORM set_config('app.tenant_id', coalesce(v_prior, ''), true);
  RETURN QUERY SELECT v_run.id, true, v_enqueued, v_created, v_reused, v_skipped;
END $$;

-- Run progress for the trigger command and operator: IDs and counts only.
CREATE FUNCTION gtm.run_status(p_tenant_id uuid, p_run_id uuid)
RETURNS TABLE (run_id uuid, status text, leads integer, jobs jsonb, lead_states jsonb,
               settled_usd numeric, reserved_usd numeric, uncertain_usd numeric)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
#variable_conflict use_column
DECLARE
  v_prior text := current_setting('app.tenant_id', true);
  v_run gtm.runs%ROWTYPE;
BEGIN
  IF p_tenant_id IS NULL OR p_run_id IS NULL THEN
    RAISE EXCEPTION 'run_status: missing identifier' USING ERRCODE = '22023';
  END IF;
  PERFORM set_config('app.tenant_id', p_tenant_id::text, true);
  SELECT * INTO v_run FROM gtm.runs r WHERE r.id = p_run_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'run_status: run not found for tenant' USING ERRCODE = '42704';
  END IF;

  RETURN QUERY
  WITH rl AS (
    SELECT lead_id, lead_record_id FROM gtm.run_leads WHERE run_id = p_run_id
  ), js AS (
    SELECT j.status, count(*)::integer AS n
      FROM gtm.jobs j JOIN rl ON rl.lead_record_id = j.lead_record_id
     WHERE j.campaign_id = v_run.campaign_id
     GROUP BY j.status
  ), ls AS (
    SELECT cl.state, count(*)::integer AS n
      FROM gtm.campaign_leads cl JOIN rl ON rl.lead_id = cl.lead_id
     WHERE cl.campaign_id = v_run.campaign_id
     GROUP BY cl.state
  )
  SELECT v_run.id, v_run.status,
         (SELECT count(*)::integer FROM rl),
         coalesce((SELECT jsonb_object_agg(js.status, js.n) FROM js), '{}'::jsonb),
         coalesce((SELECT jsonb_object_agg(ls.state, ls.n) FROM ls), '{}'::jsonb),
         v_run.settled_usd, v_run.reserved_usd, v_run.uncertain_usd;

  PERFORM set_config('app.tenant_id', coalesce(v_prior, ''), true);
END $$;

-- ---------------------------------------------------------------------------
-- Grants. Nothing is granted to PUBLIC; each role gets only its operations.
-- Function privileges are set before ownership moves (end of file): a non-superuser
-- migration role cannot change the ACL of a function it no longer owns.
-- ---------------------------------------------------------------------------
REVOKE ALL ON ALL TABLES IN SCHEMA gtm FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA gtm FROM PUBLIC;

GRANT EXECUTE ON FUNCTION gtm.current_tenant() TO gtm_worker, gtm_app, gtm_claim_owner, gtm_enqueue_owner;
GRANT EXECUTE ON FUNCTION gtm.job_key(uuid, text, uuid, integer) TO gtm_worker, gtm_enqueue_owner;
GRANT EXECUTE ON FUNCTION gtm.claim_job(uuid, text[], integer) TO gtm_worker;
GRANT EXECUTE ON FUNCTION gtm.enqueue_run(uuid, uuid, uuid, text, integer) TO gtm_trigger, gtm_app;
GRANT EXECUTE ON FUNCTION gtm.run_status(uuid, uuid) TO gtm_trigger, gtm_app;

-- Claim function owner: queue columns only.
GRANT SELECT (id, tenant_id, stage, status, available_at, created_at, lease_expires_at, attempts, fencing_token),
      UPDATE (status, attempts, leased_by, lease_expires_at, fencing_token, updated_at)
  ON gtm.jobs TO gtm_claim_owner;

-- Enqueue/status function owner.
GRANT SELECT ON gtm.campaigns, gtm.contact_imports, gtm.import_rows, gtm.config_versions,
                gtm.suppressions, gtm.leads TO gtm_enqueue_owner;
GRANT SELECT, INSERT ON gtm.runs, gtm.run_leads, gtm.jobs TO gtm_enqueue_owner;
GRANT SELECT, INSERT, UPDATE (state, state_reason, updated_at) ON gtm.campaign_leads TO gtm_enqueue_owner;

-- Worker: tenant-scoped pipeline reads, results, and completion.
GRANT SELECT ON gtm.senders, gtm.mailboxes, gtm.campaigns, gtm.config_versions, gtm.leads,
                gtm.lead_records, gtm.suppressions, gtm.run_leads TO gtm_worker;
GRANT SELECT, UPDATE (state, state_reason, updated_at) ON gtm.campaign_leads TO gtm_worker;
GRANT SELECT, UPDATE (status, settled_usd, reserved_usd, uncertain_usd, updated_at) ON gtm.runs TO gtm_worker;
GRANT SELECT, INSERT,
      UPDATE (status, error_code, available_at, leased_by, lease_expires_at, updated_at, completed_at)
  ON gtm.jobs TO gtm_worker;
GRANT SELECT, INSERT ON gtm.context_snapshots, gtm.stage_results, gtm.citations, gtm.citation_checks,
                       gtm.qualification_decisions, gtm.outreach_items TO gtm_worker;
GRANT SELECT, INSERT,
      UPDATE (output, usage, state, request_id, cost_settled_usd, error_code, settled_at)
  ON gtm.provider_calls TO gtm_worker;
GRANT SELECT, INSERT, UPDATE (last_seen_at) ON gtm.workers TO gtm_worker;

-- Operator CLI: configuration, import, suppression, campaign status, and read-only trace.
GRANT SELECT, INSERT, UPDATE (name) ON gtm.tenants TO gtm_app;
GRANT SELECT, INSERT, UPDATE (display_name) ON gtm.senders TO gtm_app;
GRANT SELECT, INSERT ON gtm.mailboxes TO gtm_app;
GRANT SELECT, INSERT, UPDATE (status, sender_id, mailbox_id, updated_at) ON gtm.campaigns TO gtm_app;
GRANT SELECT, INSERT ON gtm.config_versions, gtm.leads, gtm.lead_records, gtm.contact_imports,
                       gtm.import_rows, gtm.suppressions TO gtm_app;
GRANT SELECT, INSERT, UPDATE (state, state_reason, updated_at) ON gtm.campaign_leads TO gtm_app;
GRANT SELECT ON gtm.runs, gtm.run_leads, gtm.context_snapshots, gtm.provider_calls, gtm.stage_results,
                gtm.citations, gtm.citation_checks, gtm.qualification_decisions, gtm.outreach_items TO gtm_app;
GRANT SELECT, UPDATE (status, error_code, available_at, updated_at) ON gtm.jobs TO gtm_app;

-- Supabase API roles get nothing: the gtm schema stays out of the Data API.
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE ALL ON SCHEMA gtm FROM %I', r);
      EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA gtm FROM %I', r);
      EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA gtm FROM %I', r);
    END IF;
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- Privileged functions belong to no-login, non-bypass owner roles that hold only the grants
-- above. PostgreSQL requires the new owner to hold CREATE on the schema while ownership changes.
-- ---------------------------------------------------------------------------
GRANT CREATE ON SCHEMA gtm TO gtm_claim_owner, gtm_enqueue_owner;
ALTER FUNCTION gtm.claim_job(uuid, text[], integer) OWNER TO gtm_claim_owner;
ALTER FUNCTION gtm.enqueue_run(uuid, uuid, uuid, text, integer) OWNER TO gtm_enqueue_owner;
ALTER FUNCTION gtm.run_status(uuid, uuid) OWNER TO gtm_enqueue_owner;
REVOKE CREATE ON SCHEMA gtm FROM gtm_claim_owner, gtm_enqueue_owner;
