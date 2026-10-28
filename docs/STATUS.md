# Status

**Updated:** 2026-10-27
**Commit:** none yet. All work below is uncommitted in the working tree, awaiting review.
**Plan:** implementation plan revision 3 (Supabase backend, VM worker). Revision 2 is superseded.

## What works (verified locally 2026-10-27, fake providers only)
One fictional workflow runs end to end on PostgreSQL 17: import → research → check → writer
context built from the saved checked result → writing output with email checks → queued draft job.

| Behavior | Verified by |
|---|---|
| Schema, roles, forced RLS, and hardened queue functions apply as a **non-superuser** migration role | `tests/conftest.py` (every test run), `gtm demo` |
| Two fictional brokerages sharing a contact email stay isolated under the worker and operator roles; no scope → no rows; cross-tenant inserts and FKs rejected | `tests/test_isolation.py` |
| Privileged functions owned by no-login, non-bypass roles, fixed `search_path`, no `PUBLIC` execute; each role has only its operations | `tests/test_isolation.py` |
| Versioned inputs: immutable lead records, results, configs, and snapshots; jobs pinned to input, config, prompt hash, and model | `tests/test_pipeline.py`, `tests/test_identities.py` |
| Atomic completion with lease and fencing token; stale lease cannot commit; conflicting replay rejected; lost commit response resolved without new inference | `tests/test_restart.py` |
| Restart after each stage commit reuses completed results: no new `provider_calls` rows for completed jobs | `tests/test_restart.py`, `gtm demo` |
| Crash during a provider call: job recovered after lease expiry; the interrupted call is marked uncertain and counted as spent | `tests/test_restart.py` |
| Re-trigger with unchanged inputs reuses jobs; explicit rerun and revised records create new versions; still one first-touch outreach item | `tests/test_identities.py` |
| Per-job limits (model calls, tokens, JSON repairs) and run budget with reserved/settled/uncertain spend | `tests/test_limits_budget.py` |
| Live checks before paid stages: new suppression cancels, paused campaign blocks then resumes | `tests/test_live_checks.py` |
| Citation checks (identity, recency, meaning) and code-made qualification; service fit uses requested property location (Seattle resident buying in Torrance qualifies) | `tests/test_pipeline.py` |
| Email code checks (invented figures, unknown topics, prior relationship, protected attributes, inquiry permission, links/phones/emails, refs, length, language) and model sentence check; failures stop before any draft | `tests/test_email_checks.py` |
| No send path in code; contract schemas in sync; public-safety scan | `tests/test_repo_rules.py` |

**Test results (2026-10-27, macOS, PostgreSQL 17.11, Python 3.13.6):**
`uv run pytest` → 52 passed. `uv run mypy src` (strict) → no issues. `uv run ruff check .` and
`ruff format --check .` → clean. `uv run python scripts/check_public_safety.py` → passed (gitleaks
not installed locally, so its part was skipped). `uv run gtm demo --fake-providers` → exit 0.

## Decisions made while building (review these)
1. **Two function-owner roles**, `gtm_claim_owner` (queue columns only, across tenants) and
   `gtm_enqueue_owner` (tenant-scoped enqueue/status), instead of one shared owner.
2. **Completion is one psycopg transaction** in `gtm/queue.py`, not a database function.
3. **Prompt text is pinned inside each config version** (with its hash), so queued jobs stay
   reproducible even if a prompt file changes.
4. **Per-stage fingerprints** (research, check, write): a writing-only change, such as tone, does
   not re-run research or checks.
5. **Email-check verdicts are stored inside the writing result**, not as a separate stage result.
6. **Per-job call limits count per attempt.** Earlier attempts stay recorded, and attempts are
   capped at 3.
7. **Contacts suppressed at import are not stored at all.** Only the import row outcome is kept.
8. **Web findings with ambiguous identity are dropped from personalization.** Review is reserved
   for doubtful qualification evidence.
9. **Draft jobs are created and left queued.** The worker claims only research, check, and write
   until the draft operation exists.
10. **The migration requires PostgreSQL 16+** (`GRANT … WITH INHERIT FALSE, SET TRUE` for the
    non-superuser migration role).
11. **The demo's evaluation date is fixed at 2026-10-27**, so fictional evidence ages stay stable.

## Blockers and unverified items
- **Supabase:** no project or credentials yet. Migrations are tested only against local
  PostgreSQL 17 with a Supabase-like non-superuser migration role. Hosted grants, function
  ownership, and TLS are unverified.
- **CI:** `.github/workflows/ci.yml` has never run (nothing is committed or pushed). gitleaks has
  not run yet.
- **Providers:** the hosted model, Deepline, and Gmail adapters do not exist. Their capability,
  pricing, and idempotency facts (plan §5.4) are unverified.
- **Account-dependent unknowns** (plan §5.6): sender facts and tone reference, the five contacts,
  Workspace OAuth client, VM, Supabase plan, model privacy tier, Deepline plan, compliance footer.

## Not built yet (from the M1 plan)
Draft operation and Gmail client, `gmail_operations`, credentials encryption, `gtm review`,
provider adapters and `gtm providers check`, model evaluation, `deploy.yml`/`run.yml` and the VM
service, backups and restore drill, discovery-round job keys (`find/…`), live SIGKILL drill.

## Next task
**Draft operation with a fake Gmail client** (plan §2.3, §2.6):
- `gmail_operations` with the partial unique index (one active operation per outreach item).
- Draft-only client (path allowlist) and fake Gmail with success, timeout, and lost-response modes.
- Live rechecks before drafting: suppression, campaign pause, mailbox `needs_reauth`, evidence age.
- Uncertain-outcome reconciliation by Message-ID, where an empty search ends in `needs_review`.
- Add `draft` to the worker's stages, plus `gtm review`.
