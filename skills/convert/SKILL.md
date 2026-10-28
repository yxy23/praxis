---
name: convert
description: Convert Word, LaTeX and Markdown documents while preserving originals, tracked-change information, citations and rendered fidelity.
---

# Document conversion

## Setup and constraints

Use an available converter such as Pandoc and a compatible TeX installation when compilation is needed. Resolve the input, destination, bibliography, images and document template. Preserve originals and existing destination files; write conversions into a new staging file unless replacement is explicitly intended.

## Procedure

1. Inspect the source format and requested output. Identify embedded media, equations, tables, footnotes, cross-references, citations and revision tracking that may need special handling.
2. Establish how tracked changes should be represented. Use a converter that explicitly supports revisions or inspect the document XML. A plain text extractor cannot preserve tracked-change semantics. Keep accepted/rejected or review views distinct.
3. Convert into staging. Typical commands are `pandoc source.docx -o staged.md`, `pandoc source.docx -o staged.tex`, or `pandoc source.tex --bibliography=references.bib -o staged.docx`. Supply the actual citation, media and reference-document options required by the input.
4. Compare the converted structure and substantive content with the source. Preserve missing/changed content as a fidelity issue. Do not rewrite the source's argument to conceal a conversion problem.
5. Check headings, emphasis, tables, equations, footnotes, images and cross-references. Render the output when layout matters. Report unsupported features explicitly.
6. For LaTeX output, confirm every citation key exists and compile with the project's build command. When a reference manager generates the bibliography, update through its authorized interface or report missing keys instead of hand-editing the generated file.
7. For a version-controlled destination, inspect the diff and preserve unrelated edits. Do not stash, reset or commit another contributor's changes to clear a conversion obstacle. Follow the repository's authorized commit/push policy.
8. Return the converted file, validation performed and unresolved fidelity gaps. A successful converter exit is not evidence of a successful document build or visual match.

This is a technical format-conversion method, not a writing, voice or editorial workflow. No source document, private template or personal example is included.
