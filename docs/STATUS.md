# Status

**Updated:** 2026-10-30
**Last commit:** `61de00b` (the slim-down). The pilot code and the tool docs below are
uncommitted and waiting for review.
**Plan:** build a pilot report of verified selling opportunities for the pilot agent (five to
start), measured by how many he says are worth pursuing. Runbook: [`pilot/README.md`](../pilot/README.md).

## 2026-10-30: the opportunity-report pilot (built, not run live yet)
`gtm pilot discover | verify | check-scored | report | status`, plus the `/score-leads` skill.
- **Discovery:** Venice-hosted `deepseek-v4-1-flash-uncensored` with Venice's built-in web search
  and citations, prompt `pilot/prompts/discovery-v1.md`, criteria `pilot/criteria.yaml`. A dry
  run unless `--execute`. Stops if a response has no web results, and before any request that
  could pass the run's cap. `--only` runs one search, and `--resume` finishes a run while reusing
  saved responses. Every request is saved before sending, and its cost logged from reported usage.
- **Verification (free):** only URLs from the search results are fetched. Each source is checked
  for the quote, a date within the lookback, the area, and agent status. Pages that are gone are
  rejected, and blocked pages are marked `unverified` for checking by hand. Duplicates are caught
  within a run and against earlier runs. Pages that mention sold/pending or a listing agent get
  warnings the scorer must address.
- **Scoring:** the `/score-leads` skill (rubric: intent 40, evidence 25, relevance 20,
  recency 15, a draft to validate with the agent). `check-scored` rejects scores that don't add
  up, facts citing unverified pages, phone numbers or emails, and protected-trait language.
- **Report:** HTML and CSV, best first. Phones and emails are removed, unverified candidates are
  listed separately, and the agent's verdict columns are blank.
- **Data:** `~/gtm-data/pilot/<date>/<run id>/`, outside git; files are never overwritten.

Verified on macOS (Python 3.13.6, PostgreSQL 17.11): `uv run pytest` 60 passed (20 pilot tests
on synthetic data: invalid and invented URLs, missing quotes, blocked and missing pages, stale
sources, out-of-area, duplicates in and across runs, contradicted claims, agent-represented,
protected language, budget, resume, an interrupted request counted as spent, scored-file rules,
report scrubbing, no overwrite). mypy strict, ruff, and the public-safety scan are clean.
`uv run gtm pilot discover` (dry run) plans 4 requests, worst case $0.10.

Not verified yet: live Venice web search. The first live step is one search (`--only
fsbo_south_bay`), which costs at most about $0.03 and proves real sources come back.

## 2026-10-30: tools and the shared VM
Checked on the shared VM with a login shell. **Correction:** an earlier check from a non-login
shell said the Deepline command-line tool and Node weren't installed. Both are; `~/.local/bin` is
on the `PATH` only in login shells. That check also missed Claude Code.
- Pi 1.4.0 (exe.dev's build), with Venice set up as a provider for one model,
  `deepseek-v4-1-flash-uncensored`. The `gpi` shortcut starts Pi on that model; plain `pi`
  keeps its usual default because the account is shared. The `gtm-save-venice-key` helper saves
  the Venice key in Pi's credential file. The key isn't saved yet.
- The Deepline command-line tool (v0.3.450) is installed, signed in, and has auto-update turned off. Its key
  file is now readable only by the account. The balance was checked. No monthly cap is set,
  because the workspace is shared and the cap is still being agreed.
- Claude Code is installed. This repo is cloned at `~/projects/real-estate-gtm-engine`.
- Not installed: the `psql` client. `gh` isn't logged in. No trace of Parallel.
- Exploratory prospect lists on the VM aren't used: they select people by traits this repo forbids.

Decisions:
1. **Pi** is the development agent for trying tools by hand. **`gtm pilot`** and **`gtm run`**
   are the repeatable commands. See [tools.md](tools.md).
2. **The pilot uses Venice's web search, not Parallel,** and no database (files only).
   Deepline enrichment is by hand, only for opportunities the agent wants, after the cap is agreed.
3. **Jev** is left out until labeled results show it would pick better leads.
4. **`AGENTS.md` carries the spending and targeting rules,** because Pi loads it in sessions
   that call paid tools directly.

## Phase 1 (slim-down): done, committed in `61de00b`
| Removed | Kept |
|---|---|
| Multi-brokerage isolation (tenant IDs, per-brokerage database rules, second fictional brokerage) | Research, check, and write stages, unchanged in behavior |
| Five database logins and the trigger functions (one login now, `GTM_DATABASE_URL`) | Atomic completion: result + next job + lead state in one transaction |
| Leases, fencing tokens, and the fault-injection framework | Jobs consume the exact result their parent produced |
| Three-part budget ledger (now one spending total vs. a cap per run) | Per-job call limits and the call log with exact inputs and costs |
| Saved-context table (the call log already stores every exact input) | Master list: one record per person, do-not-contact list, versioned records |
| Config versions and fingerprints (the config is saved on each run) | One first email per lead; failure reasons on every rejection |
| Supabase/VM/Actions docs and `docs/operations.md` | Fake providers, demo, `ci.yml` (tests only), public-safety scan |

**Size:** code 5,165 → 3,710 lines; tests 1,229 → 781. That's short of the ~2,700 target. The
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

Slim-down decisions:
1. **The rerun key is the research job's input.** Research is keyed by lead record plus rerun
   number. A config change applies to new leads, or to everyone with `gtm run --rerun N`.
2. **Lead state lives on `leads`.** There's one campaign per repo, so no per-campaign table.
3. **On startup the program re-queues jobs left `running`**, and calls left `started` are counted
   as spent. This is safe only because exactly one program runs at a time.

## Not built yet
A live pilot run; Venice, Deepline, and Parallel adapters for `gtm run`; Deepline enrichment;
Gmail drafts.

## Next tasks
1. **Commit and push** (repo owner), then on the VM: `git pull && uv sync`, and run
   `gtm-save-venice-key`.
2. **First live search (needs approval):** `uv run gtm pilot discover --label first-five --only
   fsbo_south_bay --execute`, at most about $0.03. Compare `costs.jsonl` with the Venice dashboard.
3. **If real sources came back:** `--resume` the run for the other three searches (at most about
   $0.08), then verify, `/score-leads`, and report. Review the report, then send it to the agent by hand.
4. **Ask the agent** the four questions in `pilot/README.md` and update `criteria.yaml`.
5. **Agree on a Deepline cap** for the shared workspace before any enrichment.
