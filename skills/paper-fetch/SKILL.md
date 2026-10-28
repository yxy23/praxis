---
name: paper-fetch
description: "Download PDFs and attach them to Zotero entries — multi-source cascade (Unpaywall, Semantic Scholar, arXiv, institutional repos, web search)"
---

# paper-fetch — Download PDFs and attach them to Zotero entries

## Preconditions and constraints

### Tools

- `zotero`
- `web_fetch`
- `web_search`
- `bash`
- `filesystem`

### Preconditions

- Zotero running with Local API on port 23119
- Zotero MCP available
- a writable staging directory for downloads (default: ~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/ on macOS, or any local path the user prefers)

### Constraints

- try the PDF cascade in order — do not jump to manual instructions without exhausting automated sources
- always verify the downloaded file is a valid PDF before attaching (file command or size check)
- never fabricate PDF URLs — only use URLs returned by APIs or search results
- Zotero local API does not support PATCH or direct attachment upload — use a staging directory + user drag as the attachment method
- batch mode must report results per-paper (not aggregate summary)
- always save PDFs with readable names: LastnameYear-Venue.pdf

Finds open-access or repository PDFs for academic papers and delivers them to Zotero. Accepts a DOI, Zotero item key, author/year/title, or a Zotero collection name (batch mode). Tries a cascade of sources before reporting that a PDF is unavailable. Handles single papers or entire collections with missing PDFs.

This skill bridges `/research-survey` (which identifies papers to read) and `/reading` (which processes them into vault notes). A paper without a PDF cannot be fully processed by `/reading`.

---

## How to Use

**Single paper — by DOI:**
```
/paper-fetch <DOI>
```

**Single paper — by Zotero key:**
```
/paper-fetch <ZOTERO_KEY>
```

**Single paper — by description:**
```
/paper-fetch <Author> et al. <Year> <Venue>
```

**Batch — collection with missing PDFs:**
```
/paper-fetch <Collection Name> — missing PDFs
```

---

## Core Workflow

### Phase 1 — Resolve the paper

If given a Zotero key: call `zotero_get_item_metadata` to get title, authors, year, venue, DOI.
If given a DOI: call Semantic Scholar (`/graph/v1/paper/DOI:{doi}?fields=externalIds,title,year,authors`) to confirm metadata.
If given a description: search Zotero first (`zotero_search_items`); if not found, search Semantic Scholar by title.

### Phase 2 — PDF acquisition cascade

Try each source in order. Stop at the first successful PDF download.

**Step 1 — Unpaywall**
```
GET https://api.unpaywall.org/v2/{doi}?email=<your-email>
```
Check `best_oa_location.url_for_pdf`. If not null, download.

**Step 2 — Semantic Scholar openAccessPdf**
```
GET https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=openAccessPdf
Header: x-api-key: $SEMANTIC_SCHOLAR_API_KEY
```
Check `openAccessPdf.url`. Download if present.

**Step 3 — arXiv**
For CS, economics, and IS papers: search arXiv by title.
```
GET https://export.arxiv.org/api/query?search_query=ti:"{title}"&max_results=3
```
Check for an exact or near-exact title match. If found, download the PDF via `https://arxiv.org/pdf/{arxiv_id}`.

**Step 4 — Institutional repositories**
Web search:
```
"{first_author_last}" "{year}" "{abbreviated_title}" filetype:pdf site:.edu OR site:.ac.uk OR site:.edu.au
```
Check the top 3 results. Fetch each URL; confirm it returns a PDF (Content-Type: application/pdf or file starts with `%PDF`).

**Step 5 — ResearchGate / Academia.edu / author pages**
Web search:
```
"{first_author_last}" "{title_first_five_words}" filetype:pdf
```
Fetch the URL. Verify it's a PDF.

**Step 6 — Publisher OA (CC BY)**
If the paper's license is CC BY (check Unpaywall `license` field), try the publisher's direct PDF URL pattern. For Tandfonline: `https://www.tandfonline.com/doi/pdf/{doi}` with a browser User-Agent. For Emerald: `https://doi.org/{doi}`. For Sage: `https://journals.sagepub.com/doi/pdf/{doi}`.

**Step 7 — Google Scholar cache (last resort)**
Web search:
```
"{title}" "{first_author_last}" "pdf" OR "[PDF]"
```
Check returned URLs for direct PDF links (not landing pages).

### Phase 3 — Download and verify

```bash
curl -L -o "/tmp/zotero-pdfs/{AuthorYear-Venue}.pdf" "{pdf_url}" \
  -H "User-Agent: Mozilla/5.0" --max-time 30
file "/tmp/zotero-pdfs/{AuthorYear-Venue}.pdf"
```

If the file command returns "PDF document" → success. If it returns HTML or the file is < 10 KB → failure (fetched an error page). Try next source.

### Phase 4 — Deliver to Zotero

**If Zotero entry already exists (has item key):**

Copy PDF to the staging directory with a readable name:
```bash
cp "/tmp/zotero-pdfs/{AuthorYear-Venue}.pdf" \
  "<staging-dir>/{AuthorYear-Venue}.pdf"
```

Default staging dir on macOS is `~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/` (iCloud Downloads is convenient because it syncs to the device running Zotero). Substitute any directory the user prefers.

Report to user:
> PDF saved to `<staging-dir>` as `{AuthorYear-Venue}.pdf`. In Zotero, right-click the `{title}` entry → **Add Attachment** → **Attach Stored Copy of File** → select the PDF. Takes 10 seconds.

**If no Zotero entry exists yet:**

Use `zotero_add_from_file` with the PDF path and the target collection. Zotero will attempt DOI extraction. Then use `zotero_add_by_doi` for the full metadata entry and ask the user to merge.

**If no PDF found after all 7 steps:**

Report clearly:
- Which sources were tried
- Whether an OA version is expected (check Unpaywall `is_oa` field)
- The best access route: institutional login, VPN, or interlibrary loan
- If the user's institution has a link-resolver, surface that pattern (e.g., `https://<institution-domain>/link?url=https://doi.org/{doi}`)

### Batch mode

If the user names a Zotero collection:
1. `zotero_get_collection_items` to get all items
2. Filter for items with no PDF children (check `zotero_get_item_children` for each)
3. Run the cascade for each missing-PDF paper
4. Report a table: paper | result (PDF found / source / staging filename, or: not found / access route)

---

## Decision Rules

- **Confirm before downloading** if the paper is ambiguous (multiple matches for author/year). Show candidate matches and ask which.
- **Do not retry** a source that returned 403 or HTML unless the URL format changes.
- **Tandfonline blocks direct PDF even for CC BY** papers in many cases. Try institutional repo or author page instead.
- **ResearchGate PDF pages** require JS — WebFetch won't render them. Use web search to find direct PDF links, not ResearchGate landing pages.
- **National institutional repos** (e.g., UK: Warwick WRAP, White Rose, UCL Discovery, Edinburgh Research Explorer; US: HBS Working Knowledge, MIT DSpace; etc.) are reliable. Search them by name when the authors are affiliated with those institutions.

---

