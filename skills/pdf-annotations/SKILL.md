---
name: pdf-annotations
description: Inspect typed and handwritten annotations in PDFs before reading or revising from them; render ink and transcribe notes with page-level provenance.
---

# Read PDF annotations

## Setup

Use a PDF parser and renderer that expose annotation objects and page images. The bundled `pdf_annotations.py` helper inventories annotations and produces review artifacts; inspect its command-line help for the supported invocation and dependencies. Keep the source PDF unchanged.

The helper needs Python `pypdf` and Poppler's `pdftoppm` available in the selected environment. From this skill directory:

```sh
python3 pdf_annotations.py summary /path/to/annotated.pdf
python3 pdf_annotations.py render /path/to/annotated.pdf --out /path/to/scratch
```

## Procedure

1. Identify the exact annotated PDF, preferring the owning source copy over an older email/cache copy, and inspect it for text comments, highlights, ink annotations and other annotation types. Do not assume handwriting is absent because extracted text is empty.
2. Use the helper or equivalent PDF tooling to inventory every annotated page and render the pages and relevant annotation crops. Retain page-number mapping, especially where printed and PDF page numbers differ.
3. Visually inspect every rendered annotated page and crop. Read typed comments and highlighted passages in their surrounding argument.
4. Transcribe the user's handwriting by printed page, distinguishing literal transcription from interpretation. Mark uncertain words and unreadable strokes; do not replace ambiguity with a confident guess.
5. Summarize the user's questions, objections and proposed changes separately from the document author's argument and from your own analysis.
6. Use these annotations as input to the requested reading, feedback or revision. Preserve the source-specific meaning of a comment; a local correction is not automatically a general rule.
7. If the source hash changes or the user adds notes, render and read the changed source again. Confirm color meaning visually when it affects a judgment. An unannotated page is not evidence of agreement; an unfinished annotation set is not a finished review.

## Completion

Report annotation types, pages inspected, transcription coverage and any remaining uncertainty. A parser reporting zero text annotations is not evidence that the PDF contains no handwritten comments. If a rendering or page is missing, preserve that gap before claiming annotation review is complete.
