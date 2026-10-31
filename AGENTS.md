# Development instructions

Shared instructions for anyone working in this repository: people, Claude Code (through
`CLAUDE.md`), and Pi (which loads `AGENTS.md` files by default). How the tools fit together is in
[`docs/tools.md`](docs/tools.md).

## Start of every session
1. Read `docs/STATUS.md`: what works, how it was verified, blockers, and the next task.
2. Inspect the actual code and `git status` / `git diff` before trusting any document.
3. Work one phase at a time and stop for review. The repo owner makes every commit and push.
   Never add Claude or AI attribution to commits or PRs.

## This is a public repository
- Never commit real contacts, real brokerage configuration, sender emails, credentials, or tokens.
- Refer to the first customer only as "the pilot brokerage" and to its sender as "the pilot sender".
- Fictional data lives only in `config/examples/` and uses `.example` domains. Private configuration,
  contact lists, and run outputs stay outside the repo (see below) or in `config/private/`, which
  git ignores.
- GitHub Actions logs are public. Log only IDs, stages, error codes, counts, and costs.
- `scripts/check_public_safety.py` must pass. Set `GTM_PRIVATE_TERMS_FILE` locally to also check a
  private list of real names that must never appear.

## Paid tools and real people
These apply to every agent, including Pi sessions that call Deepline or Parallel directly. Inside
a Pi session, this repo's spending caps and call log don't apply, so these rules and the
person's approval are the only guardrails.
- **Ask before spending.** Before any paid call, check the balance and state the expected cost.
  Test on one record first. Anything larger, any purchase, and any credit top-up needs explicit
  approval and a stated maximum.
- **Keep keys secret.** Read API keys from environment variables or the tool's own credential
  file (the pilot reads Venice's from Pi's). Never print, log, paste, or commit them.
- **Deepline on the shared VM:** run it with `--maintenance off`, and agree on the workspace's
  monthly cap (`deepline billing set-limit`) before the first paid call. The workspace is shared.
- **Keep real people's data out of this repo.** Contact lists, enrichment results, and run outputs
  go in a private folder outside the repo: `~/gtm-data/` on the shared VM (the pilot uses `~/gtm-data/pilot/`).
- **Choose people only by service area and expressed property need, backed by sources.** Never
  select, score, filter, or describe people by race, color, ethnicity, national origin, ancestry,
  religion, sex, gender identity, sexual orientation, age, familial status, disability, or
  immigration or citizenship status. Never infer these from names. When choosing who to contact,
  never use languages, schools, or employers as stand-ins for them.
- **Send nothing.** Outreach is a draft that a person reviews.

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
uv run gtm contracts export       # after changing src/gtm/contracts.py or src/gtm/pilot/models.py
uv run gtm pilot discover         # pilot plan and worst-case cost; nothing is sent without --execute
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
