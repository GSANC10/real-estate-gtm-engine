---
name: score-leads
description: Score the verified real estate opportunities in one pilot run folder (verified.json) with the pilot rubric and write scored.json. Use only when asked to score or rank pilot opportunities, for example "/score-leads ~/gtm-data/pilot/<run-id>".
---

# Score leads (score-leads.v1)

You score real estate selling opportunities for a licensed agent, using only evidence the
pipeline has already verified. You make no web requests and call no paid tools.

## Input
One run folder, for example `~/gtm-data/pilot/2026-10-31/20261031T001500Z-first-five/`. It contains:
- `run.json`: the run's criteria, including `areas`, `priority_areas`, and `lookback_days`.
- `verified.json`: every candidate with its `status`, checks, warnings, and the original discovery record.
- `sources/*.txt`: text snapshots of the source pages, named in each check's `snapshot_file`.

## Steps
1. Read `verified.json`. Score **only** records with `"status": "verified"`. Records that are
   `unverified` (site blocked), `rejected`, or `duplicate` are never scored; the report lists the
   unverified ones for checking by hand.
2. For each one, read its source snapshots. Every fact you state must be on a snapshot whose
   check has `"quote_found": true`.
3. Apply the rubric below and write `scored.json` in the run folder: a JSON list of `scored.v1`
   records. If `scored.json` already
   exists, stop and ask before replacing it, because pilot files are never overwritten.
   (Schema: `pilot/schemas/scored.schema.json`.)
4. Run `uv run gtm pilot check-scored --run-dir <run-folder>` from the repo and fix every problem
   it reports. Then tell the person the scores, and that `uv run gtm pilot report --run-dir
   <run-folder>` builds the report.
5. Never edit `discovered.json`, `verified.json`, or anything in `sources/` or `raw/`.

## Rubric (draft weights to validate with the agent)
`score` = intent + evidence + relevance + recency.

| Dimension | Points | Guide |
|---|---|---|
| `intent` | 0–40 | 35–40: the owner is selling now (an active for-sale-by-owner listing, or a dated statement that they are selling), or a buyer asks for an agent or financing for a stated area. 20–34: a strong but indirect signal (a business announced it's moving and the source says it owns the property). 5–19: weak or ambiguous. 0: none. |
| `evidence` | 0–25 | Quote verified on the page (required for any points). More for a primary source (the listing or announcement itself) than an aggregator, for a second independent source, and for a clearly identified property (street address). Subtract for warnings such as `date_only_from_model` or `area_not_stated_on_source`. |
| `relevance` | 0–20 | Inside the configured `areas` (more inside `priority_areas`), a property type the agent handles, no agent already involved, and enough detail that he can act. |
| `recency` | 0–15 | From `newest_source_date`: within 14 days 15, within 30 days 10, older but within the run's lookback 5. **0 if no date was confirmed.** |

- `confidence`: **high** when the quote is verified, a source is dated within 30 days, and the
  address is known. **medium** when one of those is missing. **low** otherwise.
- `recommendation`: **include** for 60+ with medium or high confidence; **research_more** for
  40–59 or low confidence; **reject** below 40 or when a contradiction undermines the signal.
- Warnings `source_mentions_sold_or_pending` or `source_mentions_listing_agent` mean a page may
  contradict the signal. Read the snapshot. If it's real (the home sold, an agent has it), reject.
  If it's noise (a "recently sold nearby" list), say so in `contradictions`. `include` with one of
  these warnings and empty `contradictions` fails the check.

## Rules
- `facts`: only statements a snapshot supports, each with its `source_url` (one of the record's
  checked URLs). Put everything else in `inferences`, worded as inference ("may", "suggests").
- Record any `contradictions` between sources, and the `missing_info` an agent would need
  (asking price, how long it's been listed, whether an agent is already involved).
- Never mention, guess, or use race, ethnicity, national origin, religion, sex, age, family
  status, disability, immigration status, or any other personal characteristic. The score is
  about the property and the evidence, never about the people.
- No phone numbers, email addresses, or private people's names in any field.
- `next_step`: one concrete action for the agent, for example "Contact the owner through the
  listing's contact form and ask whether they'd consider representation", or "Confirm the sale
  is still active before reaching out".
- `scored_by`: `"score-leads.v1"` plus the model you are.

## Output example
```json
[
  {
    "schema_version": "scored.v1",
    "opportunity_id": "opp-1a2b3c4d5e6f",
    "score": 80,
    "dimensions": {"intent": 36, "evidence": 19, "relevance": 15, "recency": 10},
    "explanation": "The owner lists the house for sale without an agent on a dated listing page; address and asking price are given.",
    "facts": [{"statement": "The listing says the home is for sale by owner.", "source_url": "https://example.com/listing/123"}],
    "inferences": ["The owner may be open to representation if the home hasn't sold in a few weeks."],
    "contradictions": [],
    "missing_info": ["How long it has been listed"],
    "confidence": "high",
    "recommendation": "include",
    "next_step": "Contact the owner through the listing's contact form and ask whether they'd consider representation.",
    "scored_by": "score-leads.v1 / Claude"
  }
]
```
