---
name: vault-semantic
description: Retrieve semantically related Markdown notes through a configured embedding index, inspect candidates and distinguish similarity from substantive agreement.
---

# Semantic retrieval

## Setup

Use a configured semantic index or Smart Connections integration for the selected vault. Confirm the index's scope, query interface and freshness. Do not assume that the presence of a plugin means its index is complete or reachable.

## Procedure

1. Identify the query or seed note, requested result count and any folder exclusions. Preserve the user's conceptual question rather than replacing it with a broad keyword list.
2. Check the integration and index scope. Submit the query through the actual supported interface; for asynchronous queries, collect the original result rather than launch duplicates.
3. Parse results into note path, score and returned text. Remove duplicate paths and invalid/nonexistent notes. Report missing or partial results explicitly.
4. Rank by similarity while preserving any user-specified exclusions. Scores depend on the embedding model and corpus; do not label an uncalibrated numerical threshold as a universal quality boundary.
5. Read the leading candidate notes and identify the actual connection: shared mechanism, contrary claim, analogous case or vocabulary overlap. Similarity alone is not agreement, relevance or source verification.
6. When combined with graph retrieval, distinguish semantic-only candidates from notes supported by both routes. Avoid counting the same note twice.
7. Return concise source-linked neighbors with reasons, score context and retrieval gaps. Do not modify notes or promote concepts based solely on embedding distance.

If the semantic integration is unavailable, stop this semantic query and report the missing coverage. The calling workflow can separately choose keyword or graph retrieval; do not silently present those results as this skill's semantic output. Never fabricate scores or claim the absent index was queried.
