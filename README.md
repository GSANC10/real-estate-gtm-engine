# real-estate-gtm-engine

A lead pipeline for a real estate brokerage. It researches each contact with cited evidence and
qualifies them on two criteria only: service area and an expressed property need. For qualified
contacts it writes a first email for a person to review. It never sends email.

> **Status: early development.** Everything under "What works today" is verified locally with
> **fake** AI, search, and email providers. Venice (the AI) and Deepline (search and contact data)
> are not connected yet. See [docs/STATUS.md](docs/STATUS.md).

## How it works

```mermaid
flowchart TD
    D["Discovery (planned): Qwen on Venice + Deepline find people"] --> L[("Master list (PostgreSQL)")]
    I["Import a CSV of contacts"] --> L
    L --> R["Research: model + read-only search tools"]
    R --> C{"Evidence and qualification pass?"}
    C -->|"No or unclear"| N["Save the reason"]
    C -->|"Yes"| T["Write: fresh model context"]
    T --> V{"Email checks pass?"}
    V -->|"No"| N
    V -->|"Yes"| G["Gmail draft (planned)"]
    G --> H["A person reviews and sends"]
```

- **One program, one lead and one stage at a time.** Research, checking, and writing are steps in
  one Python program. Each step starts a fresh model conversation built from saved records.
- **Code decides.** Code checks every citation (right person, recent, says what's claimed), makes
  the qualification decision, and chooses the next step.
- **The database is the master list.** One record per person, a do-not-contact list, the reason
  behind every rejection, and every step's saved result. A stopped run picks up where it left off
  without repeating paid calls.

## What works today

Verified 2026-10-28 with fake providers on PostgreSQL 17 (`uv run pytest`: 40 passed).

| Feature | Status |
|---|---|
| Brokerage configuration in one YAML file, saved with each run | Works |
| CSV import with dedupe, do-not-contact list, and versioned records | Works |
| Research with a bounded, read-only search loop | Works (fake model and search) |
| Citation checks and code-made qualification | Works (fake claim checker) |
| Writer context built from the saved checked result | Works |
| Email rule checks plus a model sentence check, before any draft | Works (fake checker) |
| Resume after a stop, with no repeated calls | Works |
| Per-job call limits and a spending cap per run | Works |
| Discovery, Venice and Deepline connections, Gmail drafts | Not built yet ([roadmap](#roadmap)) |

## Quick start

Requires Python 3.13, [uv](https://docs.astral.sh/uv/), and PostgreSQL 17 binaries
(`brew install postgresql@17`). The tests and demo start their own throwaway database.

```bash
uv sync
uv run gtm demo --fake-providers   # fictional end-to-end run, a few seconds
uv run pytest
```

The demo imports five fictional contacts for the fictional brokerage "Example Homes". It stops
the program after the first finished job, restarts it, and prints the outcomes:

```text
=== 2. Stop after the first finished job, then restart ===
first program finished 1 research job; the restarted program processed 8 more
provider calls for the finished job: 3 before restart, 3 after

=== 3. Outcomes ===
  Avery Park     disqualified           property_location_outside_service_area
  Jordan Lee     ready_for_draft
  Morgan Diaz    insufficient_evidence  evidence_stale
  Riley Chen     insufficient_evidence  property_location_missing

=== 4. Run again with nothing changed ===
0 jobs created, 4 reused, 0 processed, 0 new provider calls
```

The finished job's calls weren't repeated after the restart. The fifth contact was on the
do-not-contact list, so their details were never stored. Jordan
lives in Seattle and asked for help buying in Torrance, so Jordan qualifies: service fit uses
where the person wants to buy, not where they live. A fictional web page about a *different*
Jordan Lee, which also contains a prompt injection, is excluded by the identity check. The full
trace, including the exact writer input, is in [docs/context-handoffs.md](docs/context-handoffs.md).

### Commands
The database URL comes from `GTM_DATABASE_URL`.

```bash
gtm import contacts.csv --source-label "website inquiries"
gtm suppress --email person@mail.example
gtm run --config config/examples/example-homes.yaml --fake-providers
gtm trace --lead <lead-id>
```

## Documentation
- [docs/architecture.md](docs/architecture.md): the program, database, and safeguards
- [docs/context-handoffs.md](docs/context-handoffs.md): what each step receives, with a fictional trace
- [docs/contracts/](docs/contracts/): JSON Schemas for every stored record and model input/output
- [docs/sketch-notes.md](docs/sketch-notes.md): how the original workflow sketch maps to the design
- [docs/STATUS.md](docs/STATUS.md): verified progress, decisions, and the next task
- [AGENTS.md](AGENTS.md): development rules

## Repository layout
```
src/gtm/            cli, config, contracts, db, queue, context, calls, worker, runs, validation, trace, demo
src/gtm/pipeline/   importing, research, checks, write
src/gtm/providers/  provider interfaces and deterministic fakes
supabase/migrations the database schema
prompts/            versioned role instructions (researcher, claim_check, writer, email_check)
config/examples/    a fictional brokerage and fictional contacts
tests/              pipeline, resume, identities, limits, email checks, repo rules
```

## Roadmap
1. Venice and Deepline connections, plus a provider check command.
2. Discovery: Qwen on Venice decides what to search, Deepline searches and finds emails, and new
   people are deduplicated against the master list.
3. Qualification rules for discovered people: which public signals count as a real need to buy or sell.
4. Gmail drafts, created for review only.

## Public repository rules
This repository is public. It contains only fictional people, brokerages, and web pages, all on
`.example` domains. Real brokerage configuration, contacts, and credentials are never committed.
CI runs a public-safety scan and gitleaks.
