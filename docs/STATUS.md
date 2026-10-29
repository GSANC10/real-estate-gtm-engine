# Status

**Updated:** 2026-10-28
**Last commit:** `5d8b297` (the first build). The Phase 1 slim-down below is uncommitted and
waiting for review.
**Plan:** slim the repo, then build discovery with Venice + Deepline (five phases). Revision 3's
multi-brokerage, VM, and GitHub Actions design is retired: each customer gets its own repo, and one
program run by hand does the work.

## Phase 1 (slim-down): done, uncommitted
| Removed | Kept |
|---|---|
| Multi-brokerage isolation (tenant IDs, per-brokerage database rules, second fictional brokerage) | Research, check, and write stages, unchanged in behavior |
| Five database logins and the trigger functions (one login now, `GTM_DATABASE_URL`) | Atomic completion: result + next job + lead state in one transaction |
| Leases, fencing tokens, and the fault-injection framework | Jobs consume the exact result their parent produced |
| Three-part budget ledger (now one spending total vs. a cap per run) | Per-job call limits and the call log with exact inputs and costs |
| Saved-context table (the call log already stores every exact input) | Master list: one record per person, do-not-contact list, versioned records |
| Config versions and fingerprints (the config is saved on each run) | One first email per lead; failure reasons on every rejection |
| Supabase/VM/Actions docs and `docs/operations.md` | Fake providers, demo, `ci.yml` (tests only), public-safety scan |

**Size:** code 5,165 → 3,707 lines; tests 1,229 → 781. That's short of the ~2,700 target. The
remainder is mostly the stages, which the plan kept unchanged on purpose: checks 338, fake
providers 332, call log and limits 314, contracts 299, research 288. Further cuts would mean
changing how the stages behave.

## Verified (2026-10-28, macOS, PostgreSQL 17.11, Python 3.13.6)
- `uv run pytest`: 40 passed.
- `uv run mypy src` (strict), `ruff check`, and `ruff format --check`: clean.
- `uv run python scripts/check_public_safety.py`: passed (gitleaks not installed locally; CI runs it).
- `uv run gtm demo --fake-providers`: exit 0, with the same five outcomes as before the slim-down,
  no repeated calls after a restart, and 0 new calls on an unchanged rerun.

| Behavior | Test |
|---|---|
| Handoff chain: each job consumes its parent's exact result; the writer input excludes email, page text, and private sender facts | `test_pipeline.py` |
| Honest outcomes for all five fictional contacts; Seattle resident buying in Torrance qualifies | `test_pipeline.py` |
| A new suppression stops spending; oversized context blocks with a reason | `test_pipeline.py` |
| Restart after each finished stage reuses it; a job interrupted mid-call is re-queued and its call counted as spent | `test_resume.py` |
| A failure inside completion saves nothing; a lost commit response is replayed without new calls; conflicting replays are rejected | `test_resume.py` |
| Unchanged reruns reuse work; an explicit rerun redoes it; revised records add versions but no second first email; queued jobs keep their run's config | `test_identities.py` |
| Tool-loop, token, JSON-repair, and spending limits; timeouts count as spent | `test_limits.py` |
| Email rule checks and the model sentence check stop bad emails before any draft | `test_email_checks.py` |
| No send path, contract schemas in sync, public-safety scan | `test_repo_rules.py` |

## Decisions in this phase
1. **The rerun key is the research job's input.** Research is keyed by lead record plus rerun
   number. A config change applies to new leads, or to everyone with `gtm run --rerun N`.
2. **Lead state lives on `leads`.** There's one campaign per repo, so no per-campaign table.
3. **On startup the program re-queues jobs left `running`**, and calls left `started` are counted
   as spent. This is safe only because exactly one program runs at a time.

## Not built yet
Venice and Deepline adapters, discovery, qualification rules for discovered people, and Gmail drafts.

## Next task: Phase 2, Venice and Deepline connections
1. Read both providers' current API docs and record dated facts in `docs/providers.md`. Venice:
   the Qwen model ID, tool-calling support, turning off its own system prompt and web search,
   pricing. Deepline: search, page contents, email finding, pricing, retries.
2. `providers/venice.py` and `providers/deepline.py`, tested with recorded responses only.
3. `gtm providers check` for one live test. **Needs your Venice and Deepline API keys.**
