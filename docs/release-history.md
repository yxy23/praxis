# Release history

This repository reconstructs the available historical versions as sanitized snapshots in a new Git history. Original development commits and private operational records are not republished. Dates below identify earlier release records where available. A historical feature description is not a claim of current compatibility.

| Generation | What changed | Migration boundary |
|---|---|---|
| 1.0, April 2026 | Initial skill, template and installation structure. | Historical snapshots must exclude personal workflow categories. |
| 1.1, April 2026 | Expansion of task decomposition and worker configuration. | Only nonpersonal implementation material belongs in the public archive. |
| Later 1.x | Source reading and retrieval expansion. | Integration dependencies required local setup. |
| 2.0, May 2026 | Procedure/state separation, local runtime skill copies, shared knowledge, deployment records and projection tooling. | Preserve knowledge and state when moving procedures out of a shared vault. |
| 3.0, July 2026 | One canonical skill procedure deployed to Claude Code and Codex, including auxiliary files. | Retire duplicate provider workflow wrappers; check both discovery roots. |
| 4.x | Named roles, registry-based routing, shared institutional records, rooms and host-owned worker execution. | A generic skill toolkit does not by itself constitute an institutional deployment. |
| 4.5 | Evidence projection, constrained graph integration, source and output contracts, research/fact-check entry points, and adaptive workflow supervision. | External engine dependencies, host capabilities and user-supplied registries are required for configured runtime use. |

The current public edition retains technical/source methods and exposes the later runtime's configuration boundaries. Personal workflow categories are excluded from all historical material. Do not use an old tag as an installation recommendation merely because it remains available.

The [migration guide](migration.md) explains the restricted public catalog. The [feature limits](features-and-limits.md) identify what publication does and does not establish.

## Archived snapshots

The `public/` tag prefix distinguishes these reviewed reconstructions from original development tags. Each snapshot has its own file manifest and source identity. They retain only material within the current publication boundary; removing whole personal workflow categories makes some early snapshots deliberately small.

| Public tag | Original version or checkpoint |
|---|---|
| [`public/v1.0.0`](https://github.com/yxy23/praxis/tree/public/v1.0.0) | Original 1.0.0 tag, April 28, 2026 |
| [`public/v1.1.0`](https://github.com/yxy23/praxis/tree/public/v1.1.0) | Original 1.1.0 tag, April 28, 2026 |
| [`public/pre-refactor-2026-05-11`](https://github.com/yxy23/praxis/tree/public/pre-refactor-2026-05-11) | Pre-refactor checkpoint, May 2026 |
| [`public/v2.0.0`](https://github.com/yxy23/praxis/tree/public/v2.0.0) | Original 2.0.0 tag, May 11, 2026 |
| [`public/v3.0.0`](https://github.com/yxy23/praxis/tree/public/v3.0.0) | Original 3.0.0 tag, July 18, 2026 |
| [`public/v4.0-checkpoint-20260906`](https://github.com/yxy23/praxis/tree/public/v4.0-checkpoint-20260906) | September 6, 2026 source checkpoint; no original 4.0.0 release tag was found |
| [`public/v4.5.0`](https://github.com/yxy23/praxis/tree/public/v4.5.0) | Current public runner and configured runtime projection |

No separate original tags were found for every intermediate point version mentioned in the narrative above. The archive does not invent those releases. Historical runtime behavior has not been requalified against current hosts or dependencies.
