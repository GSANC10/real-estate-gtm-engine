# Context handoffs

Model conversations are not memory. The database is. Each job runs in a fresh model context
that the worker builds from saved records, selected by the job's own pinned references. Passing
an ID to a model gives it nothing. The model receives the loaded content.

## What each call receives
| Call | Receives | Never receives | Tools |
|---|---|---|---|
| Researcher | One lead without its email, its recorded need and provenance, service area, need categories, tool limits | Other leads, prior transcripts, credentials | `exa_search`, `read_page` (only URLs from its own searches) |
| Claim check | One claim, its quote, date, provenance, and the identifiers needed to tell people apart | Tools, page text beyond the quote | none |
| Writer | Up to 5 checked findings, unknowns, contradictions, first name, stated language, sender facts marked for outreach, offer, CTA, tone | Email address, page text, excluded findings, private sender background | none |
| Email check | Subject, sentences, claim list, cited findings and sender facts, unknowns, inquiry permission | Tools | none |

Facts, warnings, and contradictions are never shortened to fit. When essential context is too
large, the job is blocked with `context_too_large`.

## Handoff between jobs
A completed stage commits its result and inserts the next job in the same transaction. The next
job references:

```
tenant_id, campaign_id, run_id, lead_id, lead_record_id, stage, job_key,
input_result_id, input_result_version, config_version_id, prompt_version, prompt_sha256, model_id
```

The writer job points at the exact checked result, never at "the newest research". Adding a
newer research result or editing the campaign later does not change a queued job.

## Fictional trace from `uv run gtm demo --fake-providers`
Everything below is fictional. IDs are shortened to 8 characters, except in job keys.

```text
Lead 03b62e30-8c0b-46c2-9d5c-8fef99fc4633 - state: ready_for_draft
  record v1 c40ef7f2: Jordan Lee, lives in Seattle, WA; buy_primary_residence in Torrance, CA; recorded 2026-10-20 via website inquiry

[research] job 47342c23 completed (attempts 1)
  key: research/c40ef7f2-c541-4fb6-92e7-87a639b731c1/675268cbdecdf261b270ea9b
  pinned: config v1 | model fake-model-1 | prompt researcher.v1 sha 2d563412014f
  provider calls (6, $0.007487): researcher:succeeded, exa_search:succeeded, researcher:succeeded, read_page:succeeded, read_page:succeeded, researcher:succeeded
  -> research v1 45550b13
     C1 import_record supplied record (recorded_at 2026-10-20): "I am looking for a three-bedroom home in Torrance. Can someone help with my search?"
     C2 web https://news.example/2026/09/community-design-day (published_at 2026-09-14): "Jordan Lee, a product designer at Cedar Lane Studio in Seattle, led a workshop on clear signs for public parks."
     C3 web https://runners.example/results/riverfront-10k (published_at 2026-08-02): "Jordan Lee of Portland finished third in the open division."
     F1 [qualification] Asked for help to buy a home in Torrance, CA (website inquiry, 2026-10-20). <- C1
     F2 [personalization] Jordan Lee, a product designer at Cedar Lane Studio in Seattle, led a workshop on clear signs for public parks. <- C2
     F3 [personalization] Jordan Lee of Portland finished third in the open division. <- C3
     unknowns: Budget, Purchase timeline

[check] job 17ea8aa5 completed (attempts 1)
  key: check/45550b13-5aab-4dc7-a59b-91869d91b448/30854683df6527460122ae73
  pinned: config v1 | model fake-model-1 | prompt claim_check.v1 sha 1de1cb8dade4 | input research 45550b13 v1
  provider calls (1, $0.000041): claim_check:succeeded
  -> checked_research v1 4d725319 outcome=qualified
     F1/C1: identity=pass recency=pass meaning=pass -> pass
     F2/C2: identity=pass recency=pass meaning=pass -> pass
     F3/C3: identity=fail recency=pass meaning=not_checked -> FAIL identity_conflict,claim_not_checked
     decision (code): qualified; service_area=met property_need=met; researcher proposed met/met
     kept finding_1 [qualification]: Asked for help to buy a home in Torrance, CA (website inquiry, 2026-10-20).
     kept finding_2 [personalization]: Jordan Lee, a product designer at Cedar Lane Studio in Seattle, led a workshop on clear signs for public parks.
     excluded [personalization]: Jordan Lee of Portland finished third in the open division. (identity_conflict, claim_not_checked)

[write] job 952a099a completed (attempts 1)
  key: write/4d725319-66ac-485f-87e9-a49a2bd4a474/2eb6abcf2c31f86b0cada647
  pinned: config v1 | model fake-model-1 | prompt writer.v1 sha 8263f43d6933 | input checked_research 4d725319 v1
  provider calls (2, $0.000238): writer:succeeded, email_check:succeeded
  exact writer input (context snapshot 5ceef7f7):
    {
      "campaign": {
        "cta": "Would a brief call this week be useful?",
        "max_words": 140,
        "offer": "A short call to talk through a home search or sale in the South Bay.",
        "tone_reference": "Hi there, thanks for getting in touch. I help people buy and sell homes in this area, and I'm happy to answer questions with no pressure. Would a short call be helpful?",
        "tone_rules": [
          "Warm and clear",
          "Short sentences",
          "No assumed prior relationship"
        ]
      },
      "contradictions": [],
      "findings": [
        {
          "id": "finding_1",
          "recorded_at": "2026-10-20",
          "refs": [
            "9347f804-f5f6-47a0-bbdb-f8106e8ae6bb"
          ],
          "text": "Asked for help to buy a home in Torrance, CA (website inquiry, 2026-10-20)."
        },
        {
          "id": "finding_2",
          "recorded_at": "2026-09-14",
          "refs": [
            "32ada61a-c89a-4f14-ab10-b6e2339d9816"
          ],
          "text": "Jordan Lee, a product designer at Cedar Lane Studio in Seattle, led a workshop on clear signs for public parks."
        }
      ],
      "outreach_context": {
        "may_mention_inquiry": true
      },
      "prior_outreach": [],
      "recipient": {
        "first_name": "Jordan",
        "language_preference": null
      },
      "schema_version": "writer.input.v1",
      "sender": {
        "brokerage": "Example Homes",
        "facts": [
          {
            "id": "sender_fact_1",
            "text": "Taylor Morgan helps buyers find homes across Torrance and the South Bay."
          },
          {
            "id": "sender_fact_2",
            "text": "Taylor Morgan has worked in residential real estate for twelve years."
          }
        ],
        "name": "Taylor Morgan",
        "title": "Real estate broker"
      },
      "unknowns": [
        "Budget",
        "Purchase timeline"
      ]
    }
  -> writing v1 fcd5bde8 outcome=passed
     subject: Your home search
     | Hi Jordan,
     | 
     | Thank you for your recent inquiry about your home search. Taylor Morgan helps buyers find homes across Torrance and the South Bay.
     | 
     | Would a brief call this week be useful?
     code checks: passed
     email check: completed
       not_factual  Your home search
       not_factual  Hi Jordan,
       supported    Thank you for your recent inquiry about your home search.
       supported    Taylor Morgan helps buyers find homes across Torrance and the South Bay.
       not_factual  Would a brief call this week be useful?
     claim -> finding_1: Thank you for your recent inquiry about your home search.
     claim -> sender_fact_1: Taylor Morgan helps buyers find homes across Torrance and the South Bay.

[draft] job da44e711 queued (attempts 0)
  key: draft/ef5e3762-833c-495a-9c57-0b8d472f9631
  pinned: config v1 | input writing fcd5bde8 v1
  draft operation: not implemented yet (next task); job stays queued

outreach item ef5e3762 (first_touch) <- writing fcd5bde8
```

Reading the trace:
- **Research** made 3 model rounds, 1 search, and 2 page reads. Each one has a `provider_calls`
  row with its exact input and cost.
- **Check** passed the supplied record (F1) and the matching web page (F2, which names Jordan's
  organization). It excluded the Portland page (F3) on identity, so no claim check was spent on
  it. Code made the decision: qualified, because the requested property location (Torrance) is
  in the service area. Jordan's Seattle residence is used only for identity.
- **Write** shows the exact writer input saved in the context snapshot. It contains no email
  address, no page text, and no sender fact marked `use_in_outreach: false`. Each factual
  sentence cites a finding or sender fact, the code checks passed, and the tool-free email check
  rated each sentence.
- **Draft**: the passing writing result created the first-touch outreach item and a queued
  `draft/<outreach_item_id>` job. The draft operation is the next task.

If the worker stops after any commit, a new worker picks up the next queued job and loads the
saved result. Completed stages are not repeated (`tests/test_restart.py`).
