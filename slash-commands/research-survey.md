---
description: Three-phase systematic literature survey — internal Zotero/vault check, backward citation mining, forward citation tracking; output to append-only reading-list.md
triggers:
  - /research-survey
  - literature survey
  - backward search
  - forward search
  - citation search
---

# Research Survey

Read, in this order, before producing any output:

1. `<vault-root>/system/skills/research-survey/SKILL.md` — three-phase pipeline, output format, failure modes.
3. `<vault-root>/system/memory/agents/research-survey/reading-list.md` (if exists) — prior runs to dedupe.

## What to do

If user named a topic, run **Full mode**:
- Phase 1: internal survey (vault notes + Zotero items)
- Phase 2: backward search (reference mining from anchor papers)
- Phase 3: forward search (citation tracking via Semantic Scholar + Scite.ai)
- Append dated section to reading-list.md
- Produce synthesis brief in chat

If user passed `backward <citekey>`, run **Backward mode** (Phase 2 only).

If user passed `forward <citekey>`, run **Forward mode** (Phase 3 only).

If user passed `resume`, run **Resume mode**:
- Read most recent reading-list section
- Report which suggested papers have since been added to Zotero
- Recommend next run

## Critical refusals

- Never fabricate citations — only papers from API or Zotero
- Always `use_pdf_extraction=true` on Zotero annotation calls
- Never overwrite reading-list.md — append only

## Where artifacts live

- Skill folder: `system/skills/research-survey/`
- Reading list: `system/memory/agents/research-survey/reading-list.md`
- Vault notes: `papers/<authors-year>.md` (created by `/reading`)
- Zotero: Local API on port 23119
- Semantic Scholar: https://api.semanticscholar.org (free, no key for ≤100 req / 5 min)
