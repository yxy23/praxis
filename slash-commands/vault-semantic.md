---
description: Semantic similarity retrieval via Smart Connections plugin — third leg of the retrieval stool alongside /vault-graph (structural) and flat keyword search (lexical). Pipes through pre-computed vault embeddings to find notes with content semantically similar to a query, including notes that match neither wikilinks nor keywords.
triggers:
  - /vault-semantic
  - semantic search
  - semantic retrieval
  - find similar
  - semantically similar
---

# Vault-Semantic

Read, in this order, before producing any output:

1. `~/.claude/skills/vault-semantic/SKILL.md` — pre-flight, async-lookup pattern, output template, failure modes.

## What to do

Default: `/vault-semantic <query>` → top-30 semantic neighbors via Smart Connections lookup, section-level granularity, ranked by cosine score.

Modes:
- `top=N` — adjust output size
- `dedupe=file` — collapse section chunks to one entry per file
- `min=0.X` — filter below cosine threshold

## Critical refusals

- Never fabricate matches; only Smart Connections lookup results.
- Abort if Obsidian Desktop is not running OR Smart Connections is not loaded. Do not silently fall back to graph or grep — those are separate skills.
- Read-only; never edit the vault.

## Where artifacts live

- Skill folder: `~/.claude/skills/vault-semantic/`
- Smart Connections index: `<vault-root>/.smart-env/multi/` (~1 `.ajson` per indexed note)
- CLI: `obsidian` on PATH (typically `/usr/local/bin/obsidian`); requires Obsidian Desktop running with Smart Connections enabled
- Wire-in: augment `/research-survey` alongside `/vault-graph` and flat keyword search.
