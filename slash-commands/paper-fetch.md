---
description: Download PDFs and attach them to Zotero entries — multi-source cascade (Unpaywall, Semantic Scholar, arXiv, institutional repos, web search)
triggers:
  - /paper-fetch
  - fetch pdf
  - find pdf
  - download pdf
  - attach pdf
  - get the pdf
  - papers without pdfs
  - missing pdfs
  - add pdf to zotero
---

# Paper Fetch

Read, in this order, before producing any output:

1. `<vault-root>/system/skills/paper-fetch/SKILL.md` — full PDF acquisition cascade (Unpaywall → Semantic Scholar → arXiv → institutional repos → ResearchGate / author pages → publisher OA → Google Scholar), download verification, Zotero delivery patterns.

## What this skill does

Finds open-access or repository PDFs for academic papers and delivers them to Zotero. Accepts a DOI, Zotero item key, author/year/title, or a Zotero collection name (batch mode). Tries a cascade of seven sources before reporting that a PDF is unavailable.

## Invocation patterns

- `/paper-fetch 10.1080/<doi-suffix>` — by DOI
- `/paper-fetch <ZOTERO_KEY>` — by Zotero item key
- `/paper-fetch [Author] et al. [Year] [Venue]` — by description
- `/paper-fetch <Collection Name> — missing PDFs` — batch mode

## Critical refusals

- Try the cascade in order — do not skip to manual instructions without exhausting automated sources.
- Always verify the downloaded file is a valid PDF (`file` command or size check) before delivering. An HTML error page saved as `.pdf` is a silent failure.
- Never fabricate PDF URLs. Only use URLs returned by APIs or search results.
- Zotero local API does not support PATCH or direct attachment upload. Use staging directory + user drag as the delivery method.

## If unsure which paper

Show candidate matches (multiple author/year hits) and ask which one. Don't guess.
