---
description: Graph-aware vault retrieval — seed from concepts/hubs/projects, expand via Obsidian CLI backlinks + outlinks, rank by multi-seed intersection + folder weight + recency; surfaces graph gaps as a side effect
triggers:
  - /vault-graph
  - graph search
  - graph-aware search
  - vault graph
  - graph retrieval
  - graph neighborhood
  - what links to
---

# Vault-Graph

Read, in this order, before producing any output:

1. `~/.claude/skills/vault-graph/SKILL.md` — seed/expand/rank pipeline, modes, output template.

## What to do

If user passed a `<query>`, run **Full mode**:
- Phase 0: resolve seeds across `concepts/`, `hubs/`, `projects/` (with grep fallback)
- Phase 1: one-hop expansion via `obsidian backlinks` + `obsidian links`
- Phase 2: two-hop on intersection bridges (only if `deep` flag or 2+ seeds)
- Phase 3: rank (multi-seed > folder > recency)
- Phase 4: gap audit (orphans, dead-ends)
- Phase 5: ranked output + gaps section

If user passed `expand "<note-name>"`, run **Expand mode** — single-seed neighborhood for a known note.

If user passed `gaps`, run **Gaps mode** — vault-wide orphan/dead-end audit, no query.

If user appended `deep`, enable two-hop expansion.

## Critical refusals

- Never fabricate links — only nodes returned by `obsidian` CLI or `grep`.
- Never deduplicate concept/hub twins (singular form in `concepts/` and plural form in `hubs/` of the same construct).
- Never write to the vault — gap fixes are recommendations.

## Where artifacts live

- Skill folder: `~/.claude/skills/vault-graph/`
- CLI: `obsidian` on PATH (typically `/usr/local/bin/obsidian`); requires Obsidian Desktop running with vault open
- Wire-in: augment `/research-survey` alongside `/vault-graph` and flat keyword search.
