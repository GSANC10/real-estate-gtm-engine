# Sketch notes

The original workflow sketch is: discovery model + Deepline → saved lead list → research →
writing model + instructions → email. This table records how each label was interpreted. Labels
that could not be read are flagged (⚠) rather than guessed, and they stay open until someone
who knows the sketch answers.

| Sketch element | Interpretation | Open question |
|---|---|---|
| Top-left box: "Dec…ed Model + Deepline" | Discovery: a **decensored** (uncensored) model on Venice, DeepSeek V4.1 Flash Uncensored, finds opportunities with Venice's built-in web search (`gtm pilot`, [pilot/README.md](../pilot/README.md)). Deepline enriches only opportunities the agent chooses, by hand, with approval. | Confirmed 2026-10-28; built as the pilot 2026-10-30 |
| Circled "List / CSV" | The saved lead list is the `leads` / `lead_records` tables. CSV is only for import and export. | |
| Middle box: "Research Agents" | Researcher, one job per lead | |
| Struck-through "+ Outbound Skill" in the middle box | Writing instructions belong to the Writer, not to research | ⚠ Inferred from the strike-through |
| "Claude" written over the middle box's model label | No special handling. The model is configurable per role. | ⚠ Was research meant to run on Claude? |
| Right box: "Dec…ed Model + Skill" | Writer: the same Venice-hosted model plus versioned instructions (`prompts/writer.v1.md`). Code, not the model, enforces what an email may say. | |
| Arrow from research back to the List | Research results are saved against the lead in the database | Inferred |
| "? Forn…" box leading out of the List | Not designed | ⚠ Unreadable (Formatter? Formatted?) |
| Box with a diagonal slash under the CSV | Treated as CSV export | ⚠ Unclear |
| Hatched, circled, and shaded columns under the Writer box | Not designed | ⚠ A table of lead states? A stack of drafts? |
| "Email" box at bottom left | A Gmail **draft**. Nothing is sent. | |
| "Deepline Account" | A Deepline workspace API key | |
| "ArliAI \| GLM v…(?) – Unrestri…" | Pi on the shared VM also has an ArliAI provider with `GLM-4.6-Derestricted-v5` and `Qwen3.5-27B-Derestricted`, so the label probably refers to one of those. The pipeline uses Venice. | Probable match, checked 2026-10-30 |
| "Exa…'s Account" | Exa through Deepline's managed credits | ⚠ Confirm no separate Exa account is needed |
| "GitHub Repo for the skill" | Versioned role instructions in `prompts/` in this repo | ⚠ The sketch suggests a separate repo |
| "pi" (with a small superscript) | Pi, the coding agent installed on the shared VM. It's used for development and trying tools by hand; `gtm run` is the repeatable pipeline. See `docs/tools.md`. | Resolved 2026-10-30 |
| "Gmail" | Gmail API through Google Workspace OAuth, `gmail.compose` scope, draft-only in code | |
| "Produce a list of [3/5] names that fit the ICP…" | Discovery target. The working goal is about 50 qualified leads. | ⚠ Count overwritten (3 or 5) |
