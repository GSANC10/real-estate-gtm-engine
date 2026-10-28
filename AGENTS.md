# Development instructions

Shared instructions for anyone, human or coding agent, working in this repository.

## Start of every session
1. Read `docs/STATUS.md`: what works, how it was verified, blockers, and the next task.
2. Inspect the actual code and `git status` / `git diff` before trusting any document.
3. The design reference is the implementation plan (revision 3). This repo's docs summarize the parts already built.

## This is a public repository
- Never commit real contacts, real brokerage configuration, sender emails, credentials, or tokens.
- Refer to the first customer only as "the pilot brokerage" and to its sender as "the pilot sender".
- Fictional data lives only in `config/examples/` and uses `.example` domains. Private configuration
  and contact CSVs stay on the VM (`/etc/gtm`) or in `config/private/` (git-ignored).
- GitHub Actions logs are public. Log only IDs, stages, error codes, counts, and costs.
- `scripts/check_public_safety.py` must pass. Set `GTM_PRIVATE_TERMS_FILE` locally to also check a
  private list of real names that must never appear.

## Invariants (do not break)
- **Tenant scope:** every tenant-protected query runs inside `gtm.db.tenant_tx`. Never set
  `app.tenant_id` at session level. Missing scope must return no rows.
- **Runtime roles:** the worker connects as `gtm_worker`, the operator CLI as `gtm_app`, and the
  trigger command as `gtm_trigger`. None may bypass RLS. Isolation tests run as these roles.
- **Pinned inputs:** a job references exact input result, config, prompt, and model versions. Never
  look up "the newest result". Stored results, records, configs, and snapshots are immutable.
- **Atomic completion:** `complete_stage` saves the result and inserts the next job in one
  transaction, guarded by the lease and fencing token. No model, Deepline, or Gmail call happens
  inside a database transaction.
- **Recorded calls:** every model call and tool round gets a `provider_calls` row and a budget
  reservation before dispatch.
- **Draft-only:** no code path may send email. `tests/test_repo_rules.py` fails on send paths.
- **Code decides:** qualification and transitions are code. Models propose; they never choose the
  tenant, parent IDs, or the next stage.

## Commands
```bash
uv sync                           # install
uv run ruff check . && uv run ruff format --check .
uv run mypy src                   # strict
uv run pytest                     # starts a throwaway PostgreSQL 17 cluster
uv run gtm demo --fake-providers  # fictional end-to-end run
uv run gtm contracts export       # after changing src/gtm/contracts.py
```
Tests need PostgreSQL 17 binaries (`brew install postgresql@17`, or set `GTM_PG_BIN`). They never use a
running local server. CI sets `GTM_TEST_ADMIN_DSN` to its service container instead.

## Changing things
- **Migrations** live in `supabase/migrations/` as one history. Until the first hosted deploy, the
  initial migration may still be edited; after that, only add new files. Migrations must run as a
  non-superuser role (the tests apply them as one, like Supabase's `postgres`).
- **Behavior changes to checks** bump the matching policy version in `gtm/config.py`, so new runs
  re-check instead of reusing results made under old rules.
- **Prompts** are versioned files in `prompts/`. Change behavior with a new version file.
- With each change, update `docs/STATUS.md` and the README's "What works today" (with the date
  it was verified). Development notes go in repo files; runtime lead history stays in the database.
- Ask before committing or pushing.
