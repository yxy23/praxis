---
name: paper-fetch
description: Find a lawful full-text PDF for a paper, verify its identity and attach it to a reference-manager entry using an available integration.
---

# Fetch a paper

## Setup

Use the user's reference manager, an authorized network/search tool and a writable staging directory. Configure API credentials locally when needed. Do not bypass access controls or assume a publisher subscription is available.

## Procedure

1. Resolve the paper by DOI, title, author/year or library key. Confirm the exact library item before adding an attachment. Search for duplicates before creating an item.
2. Check whether a usable PDF is already attached. If so, verify it and avoid downloading another copy without a reason.
3. Search a bounded acquisition cascade: existing attachments; lawful open-access location from DOI/Unpaywall metadata; Semantic Scholar or equivalent metadata; arXiv/preprint archive; institutional or author repository; publisher access available to the user; targeted web search. Prefer authoritative metadata and stable source URLs.
4. Download into staging. Confirm the file is a readable PDF rather than a login/error page. Check title, authors, date/version and DOI against the intended paper. Label accepted manuscripts and preprints honestly.
5. Attach the verified PDF to the intended item using the actual supported integration. Verify the attachment by reading back the library item. A downloaded file is not evidence of a completed attachment.
6. If attachment is unavailable, preserve the verified local PDF and provide the path and source. Report the attachment gap rather than inventing a successful library update.

For a batch, preserve per-paper outcomes and avoid creating duplicate items. Retry only the failed step with its original identity.

## Report

State the paper, version, acquisition source, local file and verified attachment status. If no lawful copy was found, report the searched routes and the unresolved access gap. Do not substitute a different paper with a similar title.
