# Context handoffs

A model conversation is not memory. The database is. Each step gets a fresh model context that
the program builds from saved records, selected by the job's own references. Passing an ID to a
model gives it nothing; the model receives the loaded content.

## What each call receives
| Call | Receives | Never receives | Tools |
|---|---|---|---|
| Researcher | One lead without its email: name, organization, residence, recorded need, service area, need categories, tool limits | Other leads, earlier conversations, credentials | `exa_search`, `read_page` (only URLs its own searches returned) |
| Claim check | One claim, its quote, date, source, and the identifiers needed to tell people apart | Tools, page text beyond the quote | none |
| Writer | Up to 5 checked findings, unknowns, contradictions, first name, stated language, sender facts marked for outreach, offer, call to action, tone | Email address, page text, excluded findings, private sender background | none |
| Email check | Subject, sentences, claim list, cited findings and sender facts, unknowns, inquiry permission | Tools | none |

Facts, warnings, and contradictions are never shortened to fit. When essential context is too
large, the job is blocked with `context_too_large`.

## Handoff between steps
A finished step saves its result and inserts the next job in the same transaction. The next job
points at that exact result (`input_result_id`) and belongs to the same run, so it uses the same
saved config. Adding newer research later, or editing the config file, doesn't change a queued job.
The exact messages sent with every model call are saved in `provider_calls`.

## Fictional trace from `uv run gtm demo --fake-providers`
Everything below is fictional. IDs are shortened to 8 characters, except in job keys.

```text
Lead 25ea7308-ffab-4d85-9697-5ffcd6566746 - state: ready_for_draft
  record v1 5ac72ae5: Jordan Lee, lives in Seattle, WA; buy_primary_residence in Torrance, CA; recorded 2026-10-20 via website inquiry

[research] job ce2fa604 completed (attempts 1)
  key: research/5ac72ae5-7311-4d35-a78f-b8b8450ce3db/r0
  provider calls (6, $0.007487, model fake-model-1): researcher:succeeded, exa_search:succeeded, researcher:succeeded, read_page:succeeded, read_page:succeeded, researcher:succeeded
  -> research v1 ef8e84d1
     C1 import_record supplied record (recorded_at 2026-10-20): "I am looking for a three-bedroom home in Torrance. Can someone help with my search?"
     C2 web https://news.example/2026/09/community-design-day (published_at 2026-09-14): "Jordan Lee, a product designer at Cedar Lane Studio in Seattle, led a workshop on clear signs for public parks."
     C3 web https://runners.example/results/riverfront-10k (published_at 2026-08-02): "Jordan Lee of Portland finished third in the open division."
     F1 [qualification] Asked for help to buy a home in Torrance, CA (website inquiry, 2026-10-20). <- C1
     F2 [personalization] Jordan Lee, a product designer at Cedar Lane Studio in Seattle, led a workshop on clear signs for public parks. <- C2
     F3 [personalization] Jordan Lee of Portland finished third in the open division. <- C3
     unknowns: Budget, Purchase timeline

[check] job d3a59920 completed (attempts 1)
  key: check/ef8e84d1-aa0f-4f17-9c78-e6fca8c8ca5c
  provider calls (1, $0.000041, model fake-model-1): claim_check:succeeded
  -> checked_research v1 6ad31e55 outcome=qualified
     F1/C1: identity=pass recency=pass meaning=pass -> pass
     F2/C2: identity=pass recency=pass meaning=pass -> pass
     F3/C3: identity=fail recency=pass meaning=not_checked -> FAIL identity_conflict,claim_not_checked
     decision (code): qualified; service_area=met property_need=met; researcher proposed met/met
     kept finding_1 [qualification]: Asked for help to buy a home in Torrance, CA (website inquiry, 2026-10-20).
     kept finding_2 [personalization]: Jordan Lee, a product designer at Cedar Lane Studio in Seattle, led a workshop on clear signs for public parks.
     excluded [personalization]: Jordan Lee of Portland finished third in the open division. (identity_conflict, claim_not_checked)

[write] job 604ac350 completed (attempts 1)
  key: write/6ad31e55-d2ba-4c4c-b51c-a601e7a91f3c
  provider calls (2, $0.000236, model fake-model-1): writer:succeeded, email_check:succeeded
  exact writer input (saved with the call):
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
            "C1"
          ],
          "text": "Asked for help to buy a home in Torrance, CA (website inquiry, 2026-10-20)."
        },
        {
          "id": "finding_2",
          "recorded_at": "2026-09-14",
          "refs": [
            "C2"
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
  -> writing v1 3c72f87c outcome=passed
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

[draft] job bc22661e queued (attempts 0)
  key: draft/3520c370-37fd-42cf-93c7-5893579f93ac
  draft operation: not built yet; job stays queued

outreach item 3520c370 (first_touch) <- writing 3c72f87c
```

Reading the trace:
- **Research** made 3 model rounds, 1 search, and 2 page reads.
- **Check** passed the supplied record (C1) and the page that names Jordan's organization (C2). It
  excluded the Portland page (C3) on identity, so no claim check was spent on it. Code made the
  decision: qualified, because the requested property location (Torrance) is in the service area.
  Jordan's Seattle residence is used only for identity.
- **Write** shows the exact writer input, as saved with the call. It contains no email address,
  no page text, and no sender fact marked `use_in_outreach: false`. Each factual sentence cites a
  finding or sender fact; the rule checks passed and the email check rated every sentence.
- **Draft:** the passing email created the lead's one first-touch outreach item and a queued
  `draft/<outreach_item_id>` job. The Gmail step isn't built yet.
