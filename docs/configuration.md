# Configuration

Configure only what the selected technical method needs. Keep actual machine paths, credentials and data outside Git.

| Dependency | Required input |
|---|---|
| Document conversion | Source/destination format, converter, render/build command and bibliography authority. |
| PDF annotations | `pypdf`, `pdftoppm`, source PDF and a scratch-output directory. |
| Reference acquisition | Reference-manager connector, bibliographic identifiers and lawful source access. |
| Scientific reading | Full source, source-version identity and an analysis-note template. |
| Graph retrieval | Markdown corpus and a link/backlink query interface or parser. |
| Semantic retrieval | A configured index with known corpus scope and an available query interface. |
| Runner integration | Compatible coordination host, agent contracts, execution adapters and pinned external engine dependencies. |

The generic [analysis-note template](../templates/reading-note.md) is included. It contains no source data or personal profile.

Installer configuration is distinct from integration setup. Copy `templates/user-config-template.sh` outside the checkout and point `PRAXIS_CONFIG_FILE` at it. The template contains only corpus and runtime-discovery locations; it does not configure unrelated workflows.

Ordinary source reading does not require a local model. Honor explicit restrictions attached to a source and report inaccessible material as a coverage gap. Use only capabilities exposed by the active host; a command in a historical example is not proof that a host supports it.
