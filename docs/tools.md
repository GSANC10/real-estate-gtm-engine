# Tools

How the outside tools fit together, and what is actually set up. "Verified" means checked
directly on the shared VM or in this repo on 2026-10-30. Anything not verified says so.

## Who does what
| Tool | Job | Status on 2026-10-30 |
|---|---|---|
| **Pi** | The coding agent ("harness") on the shared VM. It runs a model in a loop and carries out the model's tool calls, including shell commands. Used for development and for trying tools by hand. | Verified: Pi 1.4.0 (exe.dev's build) is installed on the VM. |
| **Venice** | The model provider: it hosts models behind an API and bills per use. It also runs web searches for the model ($0.01 each) and returns the results as citations. | Verified: set up in Pi as a provider at `https://api.venice.ai/api/v1`. The key goes in Pi's credential file through the `gtm-save-venice-key` helper on the VM (not saved yet). `gpi` starts Pi on the pilot model; plain `pi` keeps its usual default. |
| **The model** | Reasons, judges evidence, writes structured output. | Verified on Venice's model list: `deepseek-v4-1-flash-uncensored` (a DeepSeek model, not Qwen) supports web search, function calling, and response schemas. Pricing in [`pilot/criteria.yaml`](../pilot/criteria.yaml), checked 2026-10-30. |
| **Deepline** | Enrichment: contact details and verification. | Verified: the Deepline command-line tool (v0.3.450) is installed in `~/.local/bin` and signed in. Auto-update is off. The balance was checked on 2026-10-30. No monthly spending cap is set yet: the workspace is shared, so the cap is agreed first. Not used by the pilot's discovery. |
| **Claude Code** | Runs the `/score-leads` skill that scores verified opportunities. | Verified: installed on the VM, with Deepline's own skills. The pilot's skill lives in this repo, `.claude/skills/score-leads/`, and runs only when asked. |
| **Parallel** | Web search for finding candidate leads. | Not used: the pilot uses Venice's built-in web search instead. No trace of Parallel on the VM. |
| **Supabase** | Hosted PostgreSQL for the master list, results, and evidence. | Not used by the pilot, which keeps files in `~/gtm-data/pilot/`. The `gtm run` pipeline's schema is in `supabase/migrations/` and the tests run it on local PostgreSQL 17. |
| **This repo (`gtm`)** | `gtm pilot`: discover → verify → score → report. `gtm run`: research → check → write for imported contacts. | Verified with fakes (60 tests pass). The pilot's live discovery hasn't run yet. |

Jev (a separate scoring and ranking tool) is left out. Add it only if labeled results show it
picks better leads than the current qualification rules.

## Two ways to work
- **A Pi session (by hand):** explore a tool, check a balance or a price, inspect a tool's inputs,
  debug. This repo's spending caps, call log, and saved evidence do **not** apply inside a Pi
  session. Your approval and the rules in [`AGENTS.md`](../AGENTS.md) are the only guardrails.
- **The repo's commands (repeatable):** `gtm pilot` for the opportunity report
  ([`pilot/README.md`](../pilot/README.md)), `gtm run` for imported contacts, and anything you'll
  run more than once. Every paid call is saved with its exact input and cost, spending is capped
  per run, evidence is kept, and a stopped run resumes without repeating paid calls.

Try things in Pi. Once a step works, move it into the repo so it runs the same way every time.

**On the VM, use a login shell.** Tools in `~/.local/bin` (Deepline, `gpi`, the key helper) are on
the `PATH` only in login shells. Over SSH, run commands as `ssh <vm> 'bash -lc "<command>"'`;
an earlier check without `-l` wrongly reported the Deepline tool as missing.

## Starting Pi
By default Pi finds and loads `AGENTS.md` and `CLAUDE.md` files (from `pi --help`: the
`--no-context-files` option turns that off). Start Pi from this repo's folder so the repo's
rules are in scope. Pi's saved sessions appear to be grouped by folder, so `pi --continue` from
the repo folder picks up repo work. Use `pi --resume` to choose a session from a list, which
matters because the VM's single account is shared and the most recent session may be someone else's.

## First Deepline session (no spending)
Run Deepline commands with `--maintenance off` so a read-only command doesn't start an update.
Before any paid call, agree on a monthly cap for the shared workspace (`deepline billing
set-limit <credits>` applies to everyone in it). Then start Pi in this repo's folder and give it:

> Use the Deepline command-line tool to look at the available enrichment tools. Do not run any
> paid tool. Check the account balance and recent transactions, find the most relevant
> contact-enrichment tool, and show its input schema and its price per call. Explain how it would
> plug into this repo's research step. Then stop and wait for my approval before testing it on
> one contact the brokerage supplied.

Write down what you learn (tool names, prices, how the balance is reported) with the date. Phase 2
builds the Deepline adapter from those checked facts, not from memory.
