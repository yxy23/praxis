---
name: vault-graph
description: Retrieve Markdown notes through links and backlinks, rank multi-seed connections and report graph gaps without rewriting the user's knowledge.
---

# Graph-aware retrieval

## Setup

Use a Markdown vault with wikilinks and an available Obsidian CLI or equivalent link parser. Identify the actual vault and note paths before querying. Graph retrieval complements keyword and semantic search; it does not replace reading the source.

## Procedure

1. Resolve one or more seed concepts, hubs, projects or notes. Prefer exact paths or unique matches. Preserve ambiguity rather than combining unrelated notes.
2. Retrieve each seed's outgoing links and backlinks. Record link direction and source path. A missing CLI field or truncated result is incomplete retrieval, not an empty graph.
3. Expand one hop by default. Use two hops when requested or when multi-seed exploration makes bridges useful; bound the traversal and avoid cycles.
4. Rank notes reached from several independent seeds above single-seed neighbors. Use path/type information to distinguish concept, hub, project and source notes. Do not interpret link frequency as evidence strength.
5. Read the leading bridge notes before asserting substantive connections. Explain why each connection matters to the question and cite its source.
6. Audit gaps: broken links, unresolved note names, isolated relevant notes and connections supported by text but absent from the graph. Report proposed repairs; do not silently rewrite notes or create speculative links.
7. Return resolved seeds, multi-seed bridges, bounded neighbors, graph gaps and relevant notes the user may have overlooked. Include limits on traversal and coverage.

## Modes

`expand` focuses on neighbors of a named note; `gaps` focuses on broken or missing links. In a larger workflow, return source-linked candidates to the caller and let that workflow own substantive edits.

If the CLI is unavailable, use a bounded read-only link scan when possible and disclose the changed retrieval method. Do not claim a graph query ran when only text search was used.
