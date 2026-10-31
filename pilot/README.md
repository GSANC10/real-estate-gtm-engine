# Opportunity-report pilot

A short report of verified real estate opportunities in the Los Angeles area for one licensed
agent (the pilot agent): five to start, 10–15 once the quality is good. Each opportunity comes
with context, the intent signal, evidence links and dates, a 0–100 score and why, confidence,
what's missing, and a next step. **The measure of success is how many he says are worth
pursuing.** Nothing is sent to anyone; the report is reviewed and emailed by hand.

```
criteria.yaml ─► discover (Venice, paid) ─► verify (free) ─► /score-leads (Claude Code) ─► report
                 discovered.json             verified.json    scored.json                  report.html / .csv
```

| Stage | What does it | Where |
|---|---|---|
| Ideal customer | You and the agent, in [`criteria.yaml`](criteria.yaml) | areas, signal types, searches |
| Discovery | Venice-hosted DeepSeek with Venice's built-in web search, prompt [`prompts/discovery-v1.md`](prompts/discovery-v1.md) | `src/gtm/pilot/discover.py` |
| Verification | Python: refetches every source, checks quotes, dates, area, agent status, duplicates | `src/gtm/pilot/verify.py` |
| Scoring | The `/score-leads` Claude Code skill, then a Python check of its output | `.claude/skills/score-leads/`, `src/gtm/pilot/report.py` |
| Enrichment | Deepline, by hand, only for opportunities he wants to pursue, with an agreed spending cap | not automated in the pilot |
| Report | Python: HTML and CSV with blank columns for his verdict | `src/gtm/pilot/report.py` |

Schemas for every file are in [`schemas/`](schemas/) (regenerate with `uv run gtm contracts export`).

## Who counts (working definition, to confirm with the agent)
An opportunity is a specific property, or a business that owns property, where a public, dated
source shows the owner intends to sell and no agent is involved yet. On by default:
for-sale-by-owner listings, public statements of intent to sell, and businesses moving out of a
building they own. Off by default: price cuts and expired listings (already with an agent, or
MLS-only) and buyers asking for an agent or a mortgage (needs his answer below, and mortgage help
needs the right license). Never: anyone chosen by who they are rather than by a documented
property need (see [`AGENTS.md`](../AGENTS.md)).

**Ask the agent before tuning the prompt:**
1. What are three examples of opportunities you'd genuinely want us to bring you, and where do
   you find those today?
2. Sellers who need listing representation, buyers (including ones asking about financing), or both?
3. Which areas and property types, and is the South Bay (Torrance, Redondo, Manhattan, Hermosa,
   El Segundo) the right place to start?
4. If buyers asking about mortgages count: is he licensed to originate loans, or would he refer them?

His answers go into `criteria.yaml`: turn signals on or off, change areas and searches.

## Run it (on the shared VM)
One-time setup:
```bash
cd ~/projects/real-estate-gtm-engine && git pull && uv sync
gtm-save-venice-key         # paste the Venice key; it is never shown and is saved in Pi's credential file
```

Every run (`<run>` is the folder the first command prints, `~/gtm-data/pilot/<date>/<run id>/`):
```bash
uv run gtm pilot discover                         # the plan and worst-case cost; sends nothing
uv run gtm pilot discover --label first-five --only fsbo_south_bay --execute
                                                  # one search: proves web search returns real sources
uv run gtm pilot discover --resume <run> --execute    # the remaining searches; the first is reused, not repaid
uv run gtm pilot verify --run-dir <run>           # free: fetches and checks every source
claude                                            # then type: /score-leads <run>
uv run gtm pilot report --run-dir <run>           # report.html and report.csv in <run>
uv run gtm pilot status --run-dir <run>           # where a run stands and the next command, any time
```
Copy the report to your Mac with `scp exedev@<vm>:<run>/report.html .` and open it.

**Stopping and resuming.** Every request is saved under `raw/` before it's sent and every
response after. `--resume` reuses saved successful responses, retries failed ones, and counts a
request that never got an answer at its worst-case cost. Once `verify` has run, a run is closed
to discovery; start a new run instead. Files are never overwritten.

**Costs.** `--max-usd` (default $0.50) caps the whole run, earlier spending included. Each
request is logged in `costs.jsonl` from the tokens Venice reports, at the list prices in
`criteria.yaml` plus $0.01 per web search. A Pi or Venice screen showing $0 doesn't mean free:
compare `costs.jsonl` with the Venice dashboard after the first run, and recheck prices before
relying on them. Verification and the report cost nothing. Scoring uses the Claude Code
subscription on the VM.

## Reading the results
| Verification status | Meaning |
|---|---|
| `verified` | The quote is on the live page, the page is dated within `lookback_days` (or the date is flagged unconfirmed), and the property is in the areas. Scored. |
| `unverified` | The site blocked the check or didn't answer. Listed in the report under "Couldn't check automatically", unscored. |
| `rejected` | Evidence not from the search results, quote not on the page, page gone, too old, outside the areas, already has an agent, signal type off, or protected-trait language. |
| `duplicate` | Same address or source as another candidate in this run, or as one verified in an earlier run. |

Warnings such as `source_mentions_sold_or_pending` or `source_mentions_listing_agent` mean the
page may contradict the signal; the scorer must read the page and say so before including it.

## After the agent reviews it
Record his verdict (`pursue`, `maybe`, `no`) and notes in `report.csv`'s last two columns. Count
the pursue verdicts: that's the pilot's result. Then change one thing (a search, a signal, the
areas, or the rubric weights), run again, and compare. Earlier runs' opportunities are skipped
automatically as duplicates.
