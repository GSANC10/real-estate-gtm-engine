# Development instructions

Shared instructions for anyone, human or coding agent, working in this repository.

## Start of every session
1. Read `docs/STATUS.md`: what works, how it was verified, blockers, and the next task.
2. Inspect the actual code and `git status` / `git diff` before trusting any document.
3. Work one phase at a time and stop for review. The repo owner makes every commit and push.
   Never add Claude or AI attribution to commits or PRs.

## This is a public repository
- Never commit real contacts, real brokerage configuration, sender emails, credentials, or tokens.
- Refer to the first customer only as "the pilot brokerage" and to its sender as "the pilot sender".
- Fictional data lives only in `config/examples/` and uses `.example` domains. Private configuration
  and contact CSVs stay on the VM (`/etc/gtm`) or in `config/private/` (git-ignored).
- GitHub Actions logs are public. Log only IDs, stages, error codes, counts, and costs.
- `scripts/check_public_safety.py` must pass. Set `GTM_PRIVATE_TERMS_FILE` locally to also check a
  private list of real names that must never appear.

## Invariants (do not break)
- **One customer per repo and database; one program at a time.** Startup recovery re-queues
  jobs left running, which is only safe with a single program.
- **Pinned inputs:** a job points at the exact result it consumes and reads config from its run.
  Never look up "the newest result". Lead records and step results are immutable.
- **Atomic completion:** `complete_stage` saves the result and inserts the next job in one
  transaction. No model, Deepline, or Gmail call happens inside a database transaction.
- **Recorded calls:** every model call and tool round is checked against the run's spending cap
  and saved to `provider_calls` before it is sent.
- **Draft-only:** no code path may send email. `tests/test_repo_rules.py` fails on send paths.
- **Code decides:** qualification and transitions are code. Models propose; they never choose the
  parent IDs or the next stage.

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
The CLI reads the database URL from `GTM_DATABASE_URL`.

## Changing things
- **Migrations** live in `supabase/migrations/`. Until the first real database exists, the initial
  migration may still be edited; after that, only add new files.
- **Behavior changes to checks** apply to new research. Use `gtm run --rerun N` to redo leads that
  were already processed.
- **Prompts** are versioned files in `prompts/`. Change behavior with a new version file.
- With each change, update `docs/STATUS.md` and the README's "What works today" (with the date
  it was verified). Development notes go in repo files; runtime lead history stays in the database.
- Ask before committing or pushing.
