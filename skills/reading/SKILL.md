---
name: reading
description: "Process academic papers from Zotero (or uploaded PDFs) into structured vault notes — annotations-first scientific source analysis"
---

# Reading — Paper Processing Pipeline

Authoritative procedure for processing academic papers from Zotero (or uploaded PDFs) into structured vault notes. The output is a per-paper note in `papers/`; nothing else in the vault is touched.


## Preconditions and constraints

### Preconditions

- Zotero running with Local API on port 23119
- Zotero MCP available (see `<vault-root>/system/Zotero MCP Upgrade Guide.md` if you maintain one)
- vault has a `papers/` folder where processed notes are written
- vault has a paper-note template the skill can read at the start of every run

### Constraints

- always pass `use_pdf_extraction: true` to `zotero_get_annotations` — never false
- annotation count must match between Zotero extraction and the final paper note — every reader annotation appears verbatim

## Invocation

```
Process [paper title or citekey] from my Zotero library.
Process all papers in Zotero collection "[Collection Name]".
Process this uploaded PDF following the paper note template.
```

## Tools Required

| Tool | Purpose |
|------|---------|
| `zotero_search_items` | Find paper in Zotero by title or author+year |
| `zotero_get_item_metadata` | Pull full metadata (authors, year, DOI, abstract) |
| `zotero_get_annotations` | Extract ALL highlights and margin notes from PDF |
| `zotero_get_item_fulltext` | Pull full paper text |
| `zotero_get_item_children` | Check for standalone notes attached to item |
| filesystem read | Read templates, hub pages, existing notes |
| filesystem write | Write processed notes to `papers/` |
| filesystem search | Find existing vault notes by title/author |

## Sacred Spaces Rule

> ⚠️ **CRITICAL: `hubs/`, `projects/`, and `concepts/` are the user's active working spaces. NEVER modify files in these folders during paper processing.**



## Two-Template Relationship (if applicable)

If the vault has both:

1. **A comprehensive note template** (e.g., `Templates/paper-note-template.md`) — used for deep analysis via this skill. Creates full notes in `papers/`. This is the primary workflow.

2. **A lightweight Zotero-Integration template** (e.g., a Nunjucks template) — used by the Zotero Integration plugin for quick imports inside the editor. Lightweight metadata + annotations only.

**Which to use:**
- Deep source analysis: full template via this skill
- Quick capture (importing a reference to process later): Zotero Integration plugin

**Upgrading a quick capture to a full note:**
> I have a Zotero import note for [paper]. Please upgrade it to a full analysis using the paper-note template. Keep the existing annotations at the bottom.

## Processing Sequence

### Step 0 — Read the templates and the analysis frame (every run, no exceptions)

```
read_file("<vault-root>/Templates/paper-note-template.md")
```

Read the configured research-note template before processing. Apply the genre-specific analysis steps below.

### Step 0.5 — Retrieve ALL annotations BEFORE reading the paper

> ⚠️ **This step is mandatory. Do it before reading the full text.**

The user's annotations are the primary interpretive layer.

**Source A — Zotero PDF annotations:**

```
zotero_get_annotations(item_key="[key]", use_pdf_extraction=true)
```

> ⚠️ **Always pass `use_pdf_extraction: true`.** Without this flag, margin notes and free-text comments are silently dropped. Non-negotiable.

- Extract ALL annotations: highlights, sticky notes, margin comments
- Note-type annotations with Comment fields = the reader's annotations. Most valuable content.
- Count the annotations before proceeding. The final note must contain all N.

**Source B — Existing vault note:**

```
search_vault_simple(query="[author] [year]")
```

- Check `papers/` and any legacy `Literature/` folder
- If found, read the entire note
- Look for **plain text blocks outside highlight callouts** — these are the user's marginal commentary and are the most valuable content
- Extract all reader annotations verbatim

**Source C — Standalone Zotero notes:**

```
zotero_get_item_children(item_key="[key]")
```

- Check for type: note items — reading summaries or thematic notes written separately

**Integration rule:** Every reader annotation must appear in the Critical Notes and Annotations sections. Annotations that contradict the synthesis: flag explicitly, do not silently override.

### Step 1 — Locate the paper and pull metadata

**Zotero path (citekey or title provided):**
```
zotero_search_items(query="[author surname] [year]")
zotero_get_item_metadata(item_key="[key]")
```
Confirm title match. Set `source: zotero`.

**Uploaded PDF (no Zotero match):**
Extract metadata from header/first page. Check Zotero. If found → switch to Zotero path. If not → set `source: uploaded`.

### Step 2 — Determine paper type (genre)

Classify as: `conceptual | formal-model | quantitative | qualitative | design-science | mixed`

Genre selects the methodological branch of the on-its-own-terms analysis: formal → model walkthrough; quantitative → empirical/identification architecture; qualitative → qualitative architecture (logical construction, evidence chain, discourse); conceptual → conceptual architecture (argument structure, theoretical move, boundary conditions). `mixed` gets the relevant branches, not all of them.

### Step 3 — Read the full paper

```
zotero_get_item_fulltext(item_key="[key]")
```

If unavailable: search the web (arXiv, journal site) and use `web_fetch`.
Read with annotations from Step 0.5 in mind. The goal is to deepen the user's first-pass reading, not produce an independent summary.

### Step 4 — Search the vault for connections

Before the flat vault lookup, run the shared source-level retrieval path:

```bash
python3 <vault-root>/system/semantic-index/retrieve.py search \
  "[paper topic, key constructs]" --top 10
```

Keep the returned `source`, `path`, `absolute_path`, and score with the
evidence. Read selected sources one at a time through the same interface (or
the authoritative Zotero annotation/full-text tools when annotations are
required):

```bash
python3 <vault-root>/system/semantic-index/retrieve.py read <source> <path>
```

This source-level retrieval interface finds Zotero PDFs, Readwise captures, and Obsidian notes; it does not interpret
the paper or replace the annotation-first rules below.

Then run the vault-specific lookup for connections:

```
search_vault_smart(query="[paper topic, key constructs]")
```

Identify related papers already in `papers/` — use `[[wikilinks]]`.
Identify relevant hub pages in `hubs/` — note them in the Connections section.
Do NOT modify hub pages at this stage.

### Step 5 — Create the paper note

Save to: `papers/[authors-year].md`
Naming convention: `firstauthor-secondauthor-thirdauthor-YYYY.md` (up to three authors) or `firstauthor-etal-YYYY.md` (four+ authors), all lowercase.


**Read on Its Own Terms — Rhetorical moves** (every paper; Burke + move-analysis blend): terministic screen (god-/devil-terms; what the vocabulary selects vs hides); representative anecdote (the case made to stand for the whole — apt or narrowing?); pentad + driving ratio (act/scene/agent/agency/purpose; which ratio carries the persuasion); contribution moves (territory → niche → occupy; is the gap real or manufactured? what assumption is problematized?); identification (what the reader must accept for the argument to feel inevitable). Use the lens that fits; mark "not the salient move here" where one doesn't apply. Do not force the pentad onto a paper it does not illuminate.

**Methodological moves — branch by genre** (apply only the matching branch):
- **Empirical / Quantitative Architecture** *(if `quantitative` or `mixed`)* — research questions in empirical terms; context + generalisability boundary; design with causal-inference assessment; data/sample (N, span, selection, limits); identification strategy with full threat table (endogeneity, OVB, measurement, selection, external validity); findings with effect sizes and practical magnitude. Findings on the paper's own terms — no connection to the user's frameworks here.
- **Qualitative Architecture** *(if `qualitative` or `mixed`)* — logical construction (grounded / process / comparative-case / interpretive / critical); evidence chain (data → codes → constructs → claims; is each step warranted?); analytic moves (constant comparison, temporal bracketing, narrative/discourse analysis); the work of the representative quote (illustrating vs being asked to prove); trustworthiness (triangulation, negative cases, reflexivity).
- **Conceptual Architecture** *(if `conceptual`)* — argument architecture (premises → mechanism → claims; where is the unstated leap?); type of theoretical move (borrow/blend/bridge/problematize, and which order of assumption); boundary conditions + falsifiability; internal consistency.

**Critical Notes** — the user's assessment of the paper on its own terms (strengths, limits, disagreements), anchored in their annotations.

**Annotations Section** — all highlights as `> [!quote]` callouts; all reader margin notes as `> [!critical]` callouts. No highlight may be omitted.

## Output Checklist

Before marking a paper processed, verify:

- [ ] Templates read fresh at start of run
- [ ] `use_pdf_extraction: true` passed to `zotero_get_annotations`
- [ ] All N annotations retrieved and counted
- [ ] Annotation count matches what appears in the note
- [ ] Every reader margin note appears in Critical Notes and Annotations sections
- [ ] Rhetorical-move analysis present (terministic screen, representative anecdote, pentad/ratio, contribution moves, identification)
- [ ] Genre-appropriate methodological branch completed (formal / quantitative / qualitative / conceptual)
- [ ] Connections section links to existing `papers/` and `hubs/` entries

## Quality Standard


## Failure Modes

| Condition | Action |
|---|---|
| Zotero not running | Report `zotero://` API unreachable; ask user to start Zotero |
| `use_pdf_extraction` would be omitted | Refuse; always pass `true` |
| Zotero MCP page cap suspected (paper > N pages, processing looks thin) | Check `~/.config/zotero-mcp/config.json` `pdf_max_pages` and `fulltext_display_max_pages`; default of 10 silently truncates — typical fix is raise to 100 on both |
| Paper not in Zotero, only as uploaded PDF | Process from PDF; set `source: uploaded`; offer to add to Zotero |
| Annotation count zero | Verify the user has actually annotated the PDF in Zotero; do not fabricate |
| Existing vault note found at target path | Read it first; preserve annotations; offer to upgrade or replace |
| Paper-note template missing | Stop and ask for template path; do not proceed with an invented structure |

