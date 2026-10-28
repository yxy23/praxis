---
name: research-survey
description: Build a source-verified literature reading list through internal coverage checks, backward reference search and forward citation tracking.
---

# Research survey

## Setup

Use the user's reference library and workspace, plus authoritative scholarly metadata/search sources. Configure API credentials locally. A shared semantic index is optional; record retrieval limitations and use available keyword/graph search.

The output is a reading list and coverage map. Do not treat unread abstracts, citation counts or machine suggestions as a completed literature review.

## Procedure

1. Resolve the topic, research question, known seed papers and requested scope. Preserve the user's question; do not replace it with a more convenient theoretical framing.
2. **Internal survey:** search the library and existing paper notes first. Identify what is already stored, what has been read, relevant annotation evidence and gaps. Deduplicate by DOI or verified bibliographic identity.
3. **Backward search:** inspect the seed papers' actual reference lists. Identify foundational, competing and method-relevant sources with an explicit reason for inclusion. Confirm title, authors, year and identifier against reliable metadata.
4. **Forward search:** find papers citing the seeds through available citation indexes. Distinguish direct citation evidence from topical similarity. Verify bibliographic identity and inspect enough source material to justify the suggested connection.
5. Group candidates by how they advance the question: foundations, extensions, contrary evidence, methods or unresolved connections. Keep already-read, available-unread and access-missing papers distinct.
6. Append a dated entry to the configured reading-list note. Include verified citations/links, why each is relevant, how it was found and a suggested reading priority. Preserve earlier survey entries.
7. Report internal coverage, backward/forward suggestions, supported patterns and a bounded next iteration. Route downloads to `paper-fetch` and substantive reading to `reading`.

## Limits

Do not invent citations, merge records with similar titles or claim systematic completeness without an explicit search protocol and coverage evidence. A failed index is a search gap, not evidence that no related work exists. Respect source access restrictions and rate limits.
