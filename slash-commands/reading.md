---
description: Process academic papers from Zotero (or uploaded PDFs) into structured vault notes — annotations-first scientific source analysis
triggers:
  - /reading
  - process paper
  - process collection
  - paper note
---

# Reading

Read, in this order, before producing any output:

1. `~/.claude/skills/reading/SKILL.md` — full processing sequence, sacred-spaces rule, output checklist.

## What this skill does

Processes academic papers from Zotero (or uploaded PDFs) into structured vault notes in `papers/`. Annotations-first: pulls all highlights and margin comments before reading the body. Outputs one paper note. Nothing else in the vault is touched.

## Invocation patterns

- `Process [paper title or citekey] from my Zotero library`
- `Process all papers in Zotero collection "[Collection Name]"`
- `Process this uploaded PDF following the paper note template`

## Critical refusals

- Do not modify `hubs/`, `projects/`, or `concepts/` during source processing.

- `use_pdf_extraction: true` always on `zotero_get_annotations` — never false. Without it, margin notes and free-text comments silently drop.
- Every reader annotation must appear verbatim in the final note's Critical Notes and Annotations sections. No fabrication, no silent omission.

## If unsure which paper

Ask for the citekey or Zotero collection name. Don't guess.


`/reading` analyzes scientific sources and retains source-linked notes.
