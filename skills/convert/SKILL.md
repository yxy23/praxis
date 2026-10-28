---
name: convert
description: "Convert documents between Word, LaTeX, and Markdown using pandoc; stage collaborator edits without auto-merging"
---

# Convert — Document Format Conversion

## Preconditions and constraints

### Tools

- `filesystem`
- `bash`

### Preconditions

- pandoc installed (`brew install pandoc` if missing)
- source file readable; target directory writable

### Constraints

- NEVER use `textutil` on a tracked-changes .docx — it flattens `<w:ins>`/`<w:del>` to plain text and corrupts the diff. Use pandoc with `--track-changes=accept|reject|all` or unzip the .docx and read `word/document.xml` directly.
- NEVER auto-merge collaborator edits into a target file. Always stage, diff, and wait for explicit instruction.
- Save Word/.docx outputs to `~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/` (iCloud Downloads — syncs across your machines), NOT `~/Downloads/`, unless target path is explicitly specified.
- For Overleaf-tracked LaTeX repos: pull before any conversion that writes into the repo; commit + push after with the prescribed message format.
- Bibliography file `bibliography.bib` is managed by Zotero Better BibTeX — never edit manually.
- dial-back: missing pandoc is recoverable (offer brew install); a dirty git tree is a hard stop.

Pure procedural skill for converting documents between Word, LaTeX, and Markdown. No content origination — only format transformation with audit trail.

Earlier versions of this harness routed format-conversion work through a dedicated functional agent; this skill replaces that pattern.

## When to Run

- User asks to convert a file between formats: "convert this docx to latex", "make a markdown version of this", "export the section to word for a co-author".
- User shares a collaborator's tracked-changes .docx and wants it staged for review against the current draft.
- User wants to merge approved collaborator edits into the live draft.
- User invokes `/convert` directly with a file path.

## Modes

| Mode | Invocation | Behavior |
|---|---|---|
| Convert | `/convert <file> to <format>` | Straight format conversion |
| Stage | `/convert collab <file>` | Stage collaborator edits as tracked-changes patch — no auto-merge |
| Merge | `/convert merge <staged-file> <target>` | Apply approved collaborator edits to target after explicit confirmation |
| BibTeX check | `/convert bib-check <project>` | Verify all `\cite{key}` references resolve in `bibliography.bib` |

## Format Matrix

### Word (.docx) → LaTeX (.tex)

```bash
pandoc input.docx -o output.tex
```

Post-conversion cleanup:
- Verify section structure mapped to `\section{}`, `\subsection{}`
- Footnotes → `\footnote{}`
- Bold → `\textbf{}`, italic → `\textit{}`
- Tables: simple → `tabular`; complex tables get a flag for manual review
- Figures: `\includegraphics{PLACEHOLDER}` with original filename in a comment
- Append `\bibliography{bibliography}` if absent
- Add comment header at top:

```latex
% Converted from: <source filename>
% Conversion date: <YYYY-MM-DD>
```

For full-paper conversion, split into section files matching the project convention:

```
sections/
  introduction.tex
  theory.tex
  methodology.tex
  conclusion.tex
```

### Word (.docx) → Markdown (.md)

```bash
pandoc input.docx -o output.md
```

Post-conversion:
- Headings preserved as `#`, `##`, `###`
- Add YAML frontmatter skeleton (`title`, `date`, `source`)
- Flag tables and figures for manual review

### LaTeX (.tex) → Word (.docx)

```bash
pandoc input.tex -o output.docx --bibliography=bibliography.bib --citeproc
```

Treat as one-way export — source of truth stays in `.tex`. Save to iCloud Downloads by default. Flag any LaTeX-specific features that won't render cleanly (TikZ, custom macros, complex math environments).

### Tracked-changes .docx — handle with pandoc, never textutil

Three options for tracked-changes:

```bash
# Accept all changes, then convert
pandoc input.docx --track-changes=accept -o output.tex

# Reject all changes, get the original
pandoc input.docx --track-changes=reject -o output.tex

# Show all changes inline (insertions and deletions both visible)
pandoc input.docx --track-changes=all -o output.tex
```

If pandoc misses something subtle (e.g., move operations are flagged as paired ins/del), unzip the .docx and inspect `word/document.xml` directly:

```bash
unzip -p input.docx word/document.xml | head -200
```

Look for `<w:ins>`, `<w:del>`, and `<w:moveFromRangeStart>` / `<w:moveToRangeStart>` markers.

### Collaborator edits — stage, diff, wait

**CRITICAL: never auto-merge.**

```bash
# 1. Convert the collaborator's edits to staged file in collaborator/ subfolder
pandoc <name>-revisions.docx -o collaborator/<name>-revisions-<YYYY-MM-DD>.tex

# 2. Diff against the current section
diff sections/<section>.tex collaborator/<name>-revisions-<YYYY-MM-DD>.tex

# 3. Show the diff to user — do NOT apply changes automatically

# 4. Commit the staged file (not merged yet) if in a git repo
git add collaborator/<name>-revisions-<YYYY-MM-DD>.tex
git commit -m "collaborator draft: <name> <section> revisions <YYYY-MM-DD>"

# 5. Wait for explicit "apply" or "merge" instruction
```

After approval (`/convert merge ...`):
```bash
# Apply the changes (manually or via patch)
cp collaborator/<name>-revisions-<date>.tex sections/<section>.tex
git add sections/<section>.tex
git commit -m "agent-convert merge: <name> revisions approved <date>"
```

## Save Locations

| Output | Default location |
|---|---|
| Word (.docx) export | `~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/` (iCloud Downloads) |
| LaTeX section in Overleaf project | `<project-folder>/sections/<section>.tex` |
| Markdown standalone | Same directory as source unless specified |
| Collaborator stage | `<project-folder>/collaborator/<name>-<section>-<YYYY-MM-DD>.tex` |

iCloud Downloads is preferred over `~/Downloads/` because it syncs across your machines.

## Git Discipline (for Overleaf-tracked repos)

### Pull before every conversion that writes into the repo

```bash
cd ~/Library/CloudStorage/Dropbox/overleaf-repos/<project-name>
git pull
git log --oneline -3
```

### Commit format

```
<prefix> <action>: <description> <YYYY-MM-DD>
```

| Prefix | Use when |
|---|---|
| `agent-convert convert:` | Format conversion (e.g., docx → tex) |
| `collaborator draft:` | Staged collaborator edits, not yet merged |
| `agent-convert merge:` | Approved collaborator edits applied to live section |
| `agent-convert export:` | LaTeX → Word export for sharing |

### Mandatory commit triggers

- After any conversion that writes into a tracked repo
- Before ending a session
- Before handing off to user for review

### Push only on milestone

Push after a logical milestone — end of section, after review cycle. Not after every commit.

## BibTeX hygiene check

`/convert bib-check <project>`:

```bash
cd ~/Library/CloudStorage/Dropbox/overleaf-repos/<project>

# Extract all citation keys from .tex files
grep -rohE '\\cite[a-z]*\{[^}]+\}' sections/ main.tex | \
    grep -oE '\{[^}]+\}' | tr -d '{}' | tr ',' '\n' | sed 's/^ *//' | sort -u > /tmp/cite-keys.txt

# Extract all keys defined in bibliography.bib
grep -E '^@[a-zA-Z]+\{' bibliography.bib | sed 's/^@[a-zA-Z]*{//' | sed 's/,.*//' | sort -u > /tmp/bib-keys.txt

# Find missing keys
comm -23 /tmp/cite-keys.txt /tmp/bib-keys.txt
```

Report:
- Total cite keys used: N
- Total bib keys defined: M
- Missing keys: list (these need to be added to Zotero, then re-exported)

## Failure Modes

| Condition | Action |
|---|---|
| `pandoc` not installed | Report `brew install pandoc`; do not proceed |
| Tracked-changes .docx + textutil suggested | Refuse; redirect to pandoc with `--track-changes` |
| Source file does not exist | Report path; suggest checking iCloud sync if Mobile Documents path |
| Output path exists | Confirm overwrite with user before writing |
| Git working tree dirty (Overleaf repo) | Ask user to commit or stash before write-conversion |
| Conversion lossy (e.g., complex tables, TikZ) | Convert anyway; flag specific elements requiring manual review |
| BibTeX check finds missing keys | Report list; do not auto-create entries (Zotero is single source of truth) |

## Pandoc Quick Reference

```bash
# Check installation
pandoc --version

# Word → LaTeX
pandoc in.docx -o out.tex

# Word → Markdown
pandoc in.docx -o out.md

# LaTeX → Word with bibliography
pandoc in.tex -o out.docx --bibliography=bibliography.bib --citeproc

# Tracked changes — accept all
pandoc in.docx --track-changes=accept -o out.tex

# Tracked changes — show inline
pandoc in.docx --track-changes=all -o out.tex

# Markdown → LaTeX (often needed for vault-to-Overleaf transfer)
pandoc in.md -o out.tex
```

