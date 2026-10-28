---
name: research-survey
version: 2026-04-29
triggers: ["/research-survey", "literature survey", "backward search", "forward search", "citation search", "find papers on"]
tools: [filesystem, zotero, web_fetch]
preconditions:
  - Zotero running with Local API on port 23119
  - Zotero MCP available (see `<vault-root>/system/Zotero MCP Upgrade Guide.md`)
  - Semantic Scholar API reachable (https://api.semanticscholar.org)
  - vault has `system/memory/agents/research-survey/reading-list.md` (created on first run if absent)
constraints:
  - never propose papers not actually returned by API or Zotero — never fabricate citations
  - reading-list.md is append-only; each run adds a dated section, never rewrites prior runs
  - Semantic Scholar API key is set in `~/.claude/settings.json` as `$SEMANTIC_SCHOLAR_API_KEY`; pass on every call as `-H "x-api-key: $SEMANTIC_SCHOLAR_API_KEY"`. Authenticated rate limit is 1 req/sec sustained, with bursts of several hundred per minute tolerated
  - if Scite.ai integration unavailable, fall back to Semantic Scholar only — graceful degradation
  - dial-back: missing DOI is recoverable (use title search); missing API access is recoverable (Zotero-only mode)
---

# Research Survey — Systematic Literature Search

Three-phase literature survey skill: internal Zotero/vault survey → backward citation mining → forward citation tracking. Builds an iteratively-deepening reading list of papers worth adding to Zotero.

Earlier versions of this harness routed research/literature work through a dedicated functional agent; this skill replaces that pattern.

## When to Run

- Starting work on a new topic ("survey the literature on runtime AI inference architectures")
- Expanding coverage on an active project ("backward search from a specific anchor paper's citations")
- Tracking recent developments on a key paper ("who has cited <anchor paper> recently")
- User invokes `/research-survey <topic>` directly

## Modes

| Mode | Invocation | Behavior |
|---|---|---|
| Full | `/research-survey <topic>` | All three phases — internal survey, backward, forward |
| Backward | `/research-survey backward <citekey>` | Reference-mining only on one or more anchor papers |
| Forward | `/research-survey forward <citekey>` | Citation tracking only on one or more anchor papers |
| Resume | `/research-survey resume` | Pick up from prior reading-list run; show unprocessed suggestions |

## Three-Phase Workflow (Full mode)

### Phase 1 — Internal survey

Check what's already in vault and Zotero before searching outward.

**Step 1a — Vault notes**

```
search_vault_simple(query="<topic keywords>")
```

Read all matching `papers/*.md` notes. These are fully-processed primary sources. Use them as anchors for Phase 2/3.

**Step 1b — Zotero items not yet processed**

```
zotero_search_items(query="<topic>")
zotero_get_annotations(item_key="<key>", use_pdf_extraction=true)
```

Papers in Zotero but not yet in vault: extract annotations, list as candidates for `/reading` processing. Critical: always use `use_pdf_extraction=true` (memory rule: without this, margin notes and free-text comments are silently dropped).

### Phase 2 — Backward search (reference mining)

For each anchor paper from Phase 1:

**Step 2a — Extract reference list**

```
zotero_get_item_fulltext(item_key="<key>")
```

Parse the references section. Extract title, authors, year for each cited paper.

Alternative for cleaner output: Semantic Scholar references endpoint:

```
GET https://api.semanticscholar.org/graph/v1/paper/DOI:<doi>/references
    ?fields=title,authors,year,abstract
    &limit=100
```

**Step 2b — Cross-check against Zotero**

For each reference, check if it's already in the library:

```
zotero_search_items(query="<title or author year>")
```

**Step 2c — Flag missing as backward suggestions**

References NOT in Zotero → backward search suggestions.

Priority signals (flag first):
- Cited by multiple anchor papers (high consensus)
- Author calls them "foundational" or "seminal"
- Appear in theory section (not just empirical citations)

### Phase 3 — Forward search (citation tracking)

For each anchor, find recent citing papers.

**Method A — Semantic Scholar (primary, authenticated)**

```
GET https://api.semanticscholar.org/graph/v1/paper/DOI:<doi>/citations
    ?fields=title,authors,year,abstract,citationCount
    &limit=50
```

If no DOI:

```
GET https://api.semanticscholar.org/graph/v1/paper/search
    ?query=<title>
    &fields=title,authors,year,citationCount,externalIds
```

Use the returned `paperId` to fetch citations.

**Method B — Scite.ai (via Zotero MCP)**

Adds qualitative dimension — supporting vs contrasting citations:

```
zotero:scite_enrich_item(item_key="<key>")
```

Useful for theory papers where stance matters. Returns papers that support, contrast with, or mention the anchor.

**Step 3a — Filter citing papers**

Prioritize:
- Last 3 years (recent developments)
- High citation counts (impact)
- Abstract connects to current research questions
- IS / management journals (MISQ, ISR, Org Sci, SMJ, AMJ, AMR, JAIS, etc.)

Discard:
- Book reviews, conference abstracts, unrelated fields, predatory journals

**Step 3b — Add to reading list as forward suggestions**

## Output — Reading List

Append to `<vault-root>/system/memory/agents/research-survey/reading-list.md`. **Always append, never overwrite.** Each run adds a dated section.

### Format

```markdown
## YYYY-MM-DD — Reading List: <Topic>

**Research question:** <what was investigated>
**Anchor papers:** <list of citekeys>
**Mode:** <full | backward | forward>

### Backward Search Suggestions
Papers found in references of anchor articles — not yet in Zotero:

| Priority | Title | Authors | Year | Found in | Why relevant |
|---|---|---|---|---|---|
| High | <title> | <authors> | <year> | <citekey> | <reason> |

### Forward Search Suggestions
Papers that cited anchor articles — recent developments:

| Priority | Title | Authors | Year | Cites | Citations | Why relevant |
|---|---|---|---|---|---|---|
| High | <title> | <authors> | <year> | <citekey> | <N> | <reason> |

### Processing Recommendations
**Process with `/reading` next:**
- <citekey or title> — <reason>

**Add to Zotero first, then process:**
- <title> — <how to find: DOI / Google Scholar / journal>
```

## Session Output

Beyond the reading list file, produce a synthesis brief in the chat:

```
# Research Survey — <Topic> (YYYY-MM-DD)

## Internal coverage
- <N> processed vault notes used as anchors
- <N> Zotero items unprocessed (recommend /reading)

## Backward suggestions
<top-5 with one-line reason each>

## Forward suggestions
<top-5 with one-line reason each>

## Patterns observed
<theoretical tensions, gaps, convergence points>

## Recommended next run
<what to search after these papers are added and processed>

Reading list saved to system/memory/agents/research-survey/reading-list.md
```

## Iteration Loop

Designed for repeated runs:

```
Run 1: Survey Zotero + backward + forward on initial anchor papers
  ↓ produces reading-list.md
User reviews → selects papers → adds to Zotero
  ↓
/reading processes new papers → vault notes
  ↓
Run 2: Re-survey with expanded library + new backward/forward from new papers
  ↓ reading-list.md grows
repeat until coverage is sufficient
```

`/research-survey resume` reads the most recent reading-list section and reports which suggested papers have since been added to Zotero (i.e., processed and removed from the suggestion queue).

## Failure Modes

| Condition | Action |
|---|---|
| Zotero not running | Report `zotero://` API unreachable; ask user to start Zotero |
| `use_pdf_extraction` would be omitted | Refuse; always pass `true` (memory rule) |
| No DOI for anchor paper | Fall back to title-search via Semantic Scholar |
| Semantic Scholar rate-limited (429) | Confirm `$SEMANTIC_SCHOLAR_API_KEY` is being sent on the request. If yes and still 429, backoff 60 seconds and resume |
| Scite.ai unavailable | Skip Method B; proceed with Method A only |
| reading-list.md missing | Create with section header; do not error |
| No anchor papers found in Phase 1 | Ask user to provide anchor citekeys directly; do not proceed with empty Phase 2 |
| Anchor paper not in Zotero | Ask user to add it (Zotero is source of truth) |

## Semantic Scholar API Quick Reference

Pass the API key as `x-api-key` header on every call. Env var is `$SEMANTIC_SCHOLAR_API_KEY` (set in `~/.claude/settings.json`).

```bash
# Forward search — citations of a paper
curl -H "x-api-key: $SEMANTIC_SCHOLAR_API_KEY" \
  "https://api.semanticscholar.org/graph/v1/paper/DOI:<doi>/citations?fields=title,authors,year,abstract,citationCount&limit=50"

# Backward search — references of a paper
curl -H "x-api-key: $SEMANTIC_SCHOLAR_API_KEY" \
  "https://api.semanticscholar.org/graph/v1/paper/DOI:<doi>/references?fields=title,authors,year,abstract&limit=100"

# Title search
curl -H "x-api-key: $SEMANTIC_SCHOLAR_API_KEY" \
  "https://api.semanticscholar.org/graph/v1/paper/search?query=<encoded-title>&fields=title,authors,year,citationCount,externalIds"
```

