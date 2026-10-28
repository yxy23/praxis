# Technical and source-method catalog

Install one method with `bash scripts/install-skill.sh <name>`, then ask the active assistant to use it. Claude command stubs and Codex skill discovery load the same procedure. Supply the tools and source materials listed below.

| Method | Use it for | Main setup |
|---|---|---|
| [convert](../skills/convert/SKILL.md) | Word/LaTeX/Markdown conversion and fidelity checks. | Converter, renderer and source documents. |
| [paper-fetch](../skills/paper-fetch/SKILL.md) | Acquiring a lawful PDF and verifying bibliographic identity. | Reference-manager integration and network access. |
| [reading](../skills/reading/SKILL.md) | Source-grounded scientific paper analysis. | Full source and analysis-note template. |
| [pdf-annotations](../skills/pdf-annotations/SKILL.md) | Recovering PDF text comments, highlights and ink. | Python `pypdf`, Poppler `pdftoppm`, image-capable reader. |
| [research-survey](../skills/research-survey/SKILL.md) | Internal, backward and forward literature search. | Source library and scholarly metadata/search tools. |
| [vault-graph](../skills/vault-graph/SKILL.md) | Link/backlink retrieval and graph-gap reports. | Markdown corpus and link query/parser. |
| [vault-semantic](../skills/vault-semantic/SKILL.md) | Semantic retrieval with checked source connections. | Configured embedding index or integration. |

For a source-analysis task, use `paper-fetch` when acquisition is needed, `pdf-annotations` when comments or ink matter, and `reading` for the scientific argument. `research-survey` prepares a verified reading list. Graph and semantic retrieval provide candidates; neither replaces reading the source.

These public derivatives contain reusable technical procedures. Personal routines, editorial and writing workflows, administration, teaching, their related skills and private state are excluded. Source-specific restrictions and the authorized task scope still apply when using any method.

The 4.5 runner and its agent/host/engine contracts are documented separately in [architecture](architecture.md) and [verification and traceability](verification-traceability.md).
