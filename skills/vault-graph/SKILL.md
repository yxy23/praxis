---
name: vault-graph
description: "Graph-aware vault retrieval — seed from concepts/hubs/projects, expand via Obsidian CLI backlinks + outlinks, rank by multi-seed intersection + folder weight + recency; surfaces graph gaps as a side effect"
---

# Vault-Graph — Graph-Aware Vault Retrieval

Replaces flat keyword search as the default retrieval primitive for the vault. Surfaces notes that are *graph-adjacent* to a query — connected via wikilinks, backlinks, hubs, and shared concepts — not just textually matching. Years of curated `[[wikilink]]` structure carry signal that flat grep throws away.

Use `/vault-graph <query>` directly or as retrieval support for `/research-survey`. Combine graph and keyword results with provenance.

## Preconditions and constraints

### Preconditions

- Obsidian Desktop is running with the vault open (the CLI talks to the running app)
- `obsidian` CLI on PATH (typically `/usr/local/bin/obsidian`)
- vault has `concepts/`, `hubs/`, `projects/` folders for seed lookup

### Constraints

- never fabricate links — only nodes returned by `obsidian backlinks` / `obsidian links` / filesystem `grep`
- if Obsidian Desktop is not running, all `obsidian` CLI calls fail silently; fall back to filesystem grep + frontmatter parse for backlinks
- `obsidian search` is known broken in installer 1.12.x; use filesystem `grep -r -l` for keyword seeds, not CLI search
- cap expansion to 2 hops total; cap output to top 30 ranked nodes by default

## How to Use

| Mode | Invocation | Behavior |
|---|---|---|
| Full | `/vault-graph <query>` | Seed → 1-hop expand → rank → gap audit |
| Expand | `/vault-graph expand "<note-name>"` | Single-seed neighborhood; for a known note |
| Gaps | `/vault-graph gaps` | Vault-wide orphan + dead-end audit (no query) |
| Two-hop | `/vault-graph <query> deep` | Adds a second hop on intersection nodes |

`<query>` is a natural-language phrase. Quote multi-word queries. Multiple seeds can be implied (e.g., `"Project-Beta ERC"` will resolve both).

## Core Workflow (Full mode)

### Phase 0 — Resolve seeds

For each noun phrase in the query:

1. **Direct file match** — Look up filenames (case-insensitive, ignoring punctuation) in:
   - `<vault-root>/concepts/`
   - `<vault-root>/hubs/`
   - `<vault-root>/projects/`
2. **Plural / variant tolerance** — Try the phrase as given, also pluralized and singularized.
3. **Grep fallback** — If no filename match, `grep -r -l` for the phrase across `concepts/`, `hubs/`, `projects/`, `briefings/`. Take the top 5 by frequency as seeds.
4. **Multi-term queries** — Treat each distinct noun phrase as its own seed; do not merge.

Output: list of `(seed_path, source_term)` tuples. If zero seeds, abort with a "no anchors found" message — do not silently degrade to vault-wide listing.

### Phase 1 — One-hop expansion

For each seed, in parallel:

```bash
obsidian backlinks path="<seed>" format=tsv
obsidian links path="<seed>"
```

For each returned neighbor, record:
- `neighbor_path`
- `from_seed` (which seed brought it in)
- `direction` (in = backlink; out = outlink)

Union across seeds. Keep full provenance — a node hit by N seeds is more valuable than a node hit by one.

### Phase 2 — Two-hop expansion (only if `deep` flag OR query passes 2+ seeds)

For nodes that appear in **multiple seeds' neighborhoods** (intersection set), fetch their backlinks/links too. Bridges are where the graph value lives — these are the notes that connect the user's seeds without being the seeds themselves.

Cap: do not expand more than 10 bridge nodes; do not go past hop 2.

### Phase 3 — Rank

Compute a score per node:

| Signal | Weight |
|---|---|
| Hit by 2+ seeds (bridge node) | +5 |
| Hit by 1 seed | +1 |
| In `concepts/`, `hubs/` | +2 |
| In `projects/`, `book/` | +2 |
| In `briefings/`, `papers/` | +1 |
| In `inbox/`, `readwise/` | 0 |
| Direction = "in" (a note CITES the seed) | +1 |
| Direction = "out" (seed cites this — already curated by user) | +2 |
| `date_updated` within 30 days OR mtime < 30 days | +1 |


### Phase 4 — Gap audit (side effect — always runs)

For each seed AND for each top-10 ranked node:
- If `obsidian backlinks path=X` returns "No backlinks found" → flag as **orphan**.
- If `obsidian links path=X` returns "No links found" → flag as **dead-end**.

Surface in output as "Graph gaps to fix."

Rationale: graph-aware retrieval surfaces gaps as a byproduct of its main work. A note that should be a hub but has zero backlinks is undisco­verable from any other note — fix it now.

### Phase 5 — Output

```markdown
# Vault-Graph — <query> (YYYY-MM-DD)

## Seeds
- `concepts/<X>.md` (from "<term>")
- `hubs/<Y>.md` (from "<term>")
- `projects/<Z>.md` (from "<term>")

## Bridge nodes (multi-seed hits)
| Rank | Path | Seeds | Why |
|---|---|---|---|
| 1 | `hubs/<...>` | A, B | outlink of A; backlink of B |
| ... | | | |

## Single-seed neighbors (top 20)
<grouped by tier: concepts → hubs → projects → book → papers → briefings → other>

## Graph gaps to fix
- **Orphan**: `<path>` — no backlinks. Suggest linking from: `<candidate>`.
- **Dead-end**: `<path>` — no outlinks. Suggest linking to: `<candidate>`.

## Notes the user may not have remembered
<1–3 nodes from "Single-seed neighbors" that are NOT obvious from the query but rank in the top 10>
```

## Expand Mode

`/vault-graph expand "<note-name>"`:

1. Resolve the note (exact filename match or fuzzy across `concepts/`, `hubs/`, `projects/`, `papers/`, `briefings/`).
2. Skip Phase 0; this is a single-seed run.
3. Run Phase 1 (backlinks + outlinks).
4. Skip Phase 2 (one seed, no intersection set).
5. Run Phase 3 with single-seed scoring.
6. Run Phase 4 gap audit.
7. Output as above, but section header is "## Neighborhood of `<note>`".

## Gaps Mode

`/vault-graph gaps`:

No query. Pure audit. Runs:

```bash
obsidian orphans
obsidian deadends
obsidian unresolved
```

Outputs:
- **Orphans** grouped by folder (focus on `concepts/`, `hubs/`, `projects/`).
- **Dead-ends** grouped by folder.
- **Unresolved wikilinks** — these are `[[broken-link]]` references with no target file. Surface the top 20 by count.

For each high-value orphan (in `concepts/`, `hubs/`, `projects/`, `briefings/`), propose one likely link target by running a 1-hop search on filename keywords.

## Wire-In Behavior

`vault-graph` is the **structural leg** of a three-leg retrieval stool. The two other legs are:
- **Lexical** (flat keyword search: `search_vault_simple` MCP, filename substring match, or shared-memory scan depending on calling skill)
- **Semantic** ([[vault-semantic]] — Smart Connections embeddings via `obsidian eval`)

Calling skills run all three in parallel and merge. The leg-of-origin determines how each hit is interpreted:

| Hit pattern | Confidence | Routing |
|---|---|---|
| Graph + Flat + Semantic | Highest | Lead anchor candidates |
| Two of three | High | Strong candidates |
| Graph-only | Curated structure | Standard graph value |
| Flat-only | **Graph gap** | "May need wiring" — keyword match without wikilink |
| Semantic-only | **Cross-domain reach** | "Content-similar but unconnected" — the most informative novel signal |

Five wire-in points (as of 2026-05-25):

| Skill | Phase | Graph mode | Wire-in details |
|---|---|---|---|
| `/research-survey` | Phase 1a | Full | Three-leg merge: graph + `search_vault_simple` (MCP) + `/vault-semantic`. Anchor candidates for Phase 2/3. |

Rationale: each leg has different recall failures. Graph misses material not yet wikilinked. Flat misses material wikilinked despite keyword mismatch. Semantic misses material that doesn't yet exist in the embedding index (newly added notes). The three legs are complementary, not interchangeable.

## CLI Reliability Notes

| Command | Reliable? | Fallback |
|---|---|---|
| `obsidian backlinks` | ✓ | None needed |
| `obsidian links` | ✓ | None needed |
| `obsidian files folder=X` | ✓ | `ls "<vault-root>/X"` |
| `obsidian read path=X` | ✓ | `cat` |
| `obsidian search query=X` | ✗ broken (installer 1.12.x) | `grep -r -l "<query>" "<vault-root>"` |
| `obsidian orphans` / `deadends` / `unresolved` | ✓ | None |

If Obsidian Desktop is closed, ALL CLI calls fail silently (empty output). Detection: `obsidian vault info=name` returns nothing → Obsidian is not running. Tell the user to open Obsidian, do not attempt partial degradation.

## Constraints

- **Never invent links.** Only nodes returned by `obsidian` CLI or grep count as graph edges.
- **Never deduplicate concept/hub twins** (e.g., a singular form in `concepts/` and plural form in `hubs/` of the same construct). They are distinct nodes by curatorial choice; different folders carry different intent.
- **Cap output at top 30** by default. The user can request `top=N` as a parameter for deeper exploration.
- **Do not write to vault** in any mode. This skill is read-only. Gap fixes are *recommendations*, not edits.

