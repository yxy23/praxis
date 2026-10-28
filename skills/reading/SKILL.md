---
name: reading
description: "Process academic papers from Zotero (or uploaded PDFs) into structured vault notes — annotations-first scientific source analysis"
---

# Reading — Paper Processing Pipeline

## Preconditions and constraints

### Tools

- `zotero`
- `filesystem`

### Preconditions

- Zotero running with Local API on port 23119
- Zotero MCP available (see `<vault-root>/system/Zotero MCP Upgrade Guide.md` if you maintain one)
- vault has a `papers/` folder where processed notes are written
- vault has a paper-note template the skill can read at the start of every run

### Constraints

- always pass `use_pdf_extraction: true` to `zotero_get_annotations` — never false
- annotation count must match between Zotero extraction and the final paper note — every reader annotation appears verbatim

Authoritative procedure for processing academic papers from Zotero (or uploaded PDFs) into structured vault notes. The output is a per-paper note in `papers/`; nothing else in the vault is touched.

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

### Step 0 — Read the templates (every run, no exceptions)

```
read_file("<vault-root>/Templates/paper-note-template.md")
```

Read the note template before every processing run. This file (the skill itself) is the authoritative source of truth for the pipeline; the template provides the note structure. Adjust the template path to whatever your vault uses.

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

### Step 2 — Determine paper type

Classify as: `theoretical | formal-model | empirical | design-science | mixed`

This determines which sections are required (formal model walkthrough, empirical architecture, or both).

### Step 3 — Read the full paper

```
zotero_get_item_fulltext(item_key="[key]")
```

If unavailable: search the web (arXiv, journal site) and use `web_fetch`.
Read with annotations from Step 0.5 in mind. The goal is to deepen the user's first-pass reading, not produce an independent summary.

### Step 4 — Search the vault for connections

```
search_vault_smart(query="[paper topic, key constructs]")
```

Identify related papers already in `papers/` — use `[[wikilinks]]`.
Identify relevant hub pages in `hubs/` — note them in the Connections section.
Do NOT modify hub pages at this stage.

### Step 5 — Create the paper note

Save to: `papers/[authors-year].md`
Naming convention: `firstauthor-secondauthor-thirdauthor-YYYY.md` (up to three authors) or `firstauthor-etal-YYYY.md` (four+ authors), all lowercase.

Follow the paper-note template exactly. Every section in the template must appear.

**Architecture of the Argument** — what move does the paper make? What is the minimum machinery needed? Why is a model or study required at all?

**Formal Model Walkthrough** *(if `paper_type` includes `formal-model`)* — walk through every key equation with three layers: (1) the math, (2) plain English, (3) intuition + what's doing the work. Explain the model variables and the role each plays in the result.

**Empirical Architecture** *(if `paper_type` includes `empirical` or `mixed`)* — mandatory, all six components:
1. Research questions in empirical (causal/predictive) terms
2. Study context including generalisability boundary
3. Method and research design with explicit causal inference assessment
4. Data and sample: N, time span, selection, limitations
5. Analytical approach with full threat assessment table (endogeneity, OVB, measurement, selection, external validity)
6. Major findings with effect sizes, practical magnitude, and connection to the user's frameworks




**Annotations Section** — all highlights as `> [!quote]` callouts; all reader margin notes as `> [!critical]` callouts. No highlight may be omitted.

## Output Checklist

Before marking a paper processed, verify:

- [ ] Templates read fresh at start of run
- [ ] `use_pdf_extraction: true` passed to `zotero_get_annotations`
- [ ] All N annotations retrieved and counted
- [ ] Annotation count matches what appears in the note
- [ ] Every reader margin note appears in Critical Notes and Annotations sections
- [ ] If empirical or mixed — all six Empirical Architecture fields completed
- [ ] Connections section links to existing `papers/` and `hubs/` entries

## Quality Standard

Pick one fully-processed formal-model note in your `papers/` folder as the benchmark exemplar. Match that depth for every formal model: three-layer explanations for each equation, variables explained and assumptions assessed.

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

