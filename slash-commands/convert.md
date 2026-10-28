---
description: Convert documents between Word, LaTeX, and Markdown using pandoc; stage collaborator edits without auto-merging
triggers:
  - /convert
  - convert file
  - convert document
  - pandoc
  - docx to latex
  - latex to word
---

# Convert — Document Format Conversion

Read, in this order, before producing any output:

1. `~/.claude/skills/convert/SKILL.md` — modes, format matrix, save locations, failure modes.

## What to do

If the user named a source and target format, run **Convert mode**:
- Verify pandoc available
- Run pandoc with appropriate flags
- Apply post-conversion cleanup per format
- Save to default location (iCloud Downloads for .docx, sections/ for .tex, source-adjacent for .md)
- If in a tracked Overleaf repo: pull before, commit after

If the user shared a collaborator's edits, run **Stage mode**:
- Convert to `collaborator/<name>-<date>.tex`
- Diff against current section
- Show diff
- Commit staged file
- Wait for explicit merge instruction (do NOT auto-merge)

If the user said `merge` after a stage, run **Merge mode**:
- Confirm intent
- Apply staged file to target
- Commit with `agent-convert merge:` prefix

If the user said `bib-check`, run **BibTeX hygiene check**:
- Extract all `\cite{key}` references from .tex files
- Compare against `bibliography.bib` definitions
- Report missing keys (do not auto-create — Zotero is source of truth)

## Critical refusals

- Tracked-changes .docx via textutil → REFUSE; use `pandoc --track-changes=...` instead
- Auto-merge of collaborator edits → REFUSE; always stage and wait
- Editing `bibliography.bib` directly → REFUSE; Zotero Better BibTeX manages it

## Where artifacts live

- Skill folder: `~/.claude/skills/convert/`
- iCloud Downloads (.docx outputs): `~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/`
- Overleaf repos: `~/Library/CloudStorage/Dropbox/overleaf-repos/<project>/`
- Collaborator staging: `<project>/collaborator/<name>-<date>.tex`
