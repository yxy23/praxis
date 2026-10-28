# Installation and deployment

Install a selected technical method first. Its setup section names the tools and source materials it needs. Installation configures discovery; using the method remains bounded by the requested task.

```sh
git clone https://github.com/yxy23/praxis.git
cd praxis
cp templates/user-config-template.sh ~/.praxis-config.sh
# Edit ~/.praxis-config.sh to point at your workspace.
export PRAXIS_CONFIG_FILE="$HOME/.praxis-config.sh"
bash scripts/install-skill.sh convert
```

The configuration file is executable shell sourced by the installer; use a file you control. To try deployment in a disposable folder first:

```sh
trial_dir=$(mktemp -d)
PRAXIS_DEPLOY_ROOT="$trial_dir" bash scripts/install-skill.sh convert
diff -r "$trial_dir/.claude/skills/convert" "$trial_dir/.agents/skills/convert"
```

`PRAXIS_DEPLOY_ROOT` redirects installer writes beneath the supplied destination. Inspect that folder before removing it. For the complete public skill catalog, `bash scripts/install-all.sh` installs the public skills; it does not activate the 4.5 runner or configure an institutional deployment.

## Skill installation

1. Clone the public repository into a local source folder.
2. Read `scripts/install-skill.sh` and its configuration options. Set a writable corpus root and runtime skill directories in local configuration. Never commit actual values.
3. Back up existing runtime skill directories before replacing a skill. Keep user state and knowledge outside the source checkout.
4. Run `bash scripts/install-skill.sh <skill-name>` for a selected skill. Use the installer supported by this checkout; do not run historical recipes from another release.
5. Verify the installed skill and auxiliary files, then confirm the host discovers it. Check Claude/Codex parity when deploying to both runtimes.
6. Exercise a reversible sample task using invented data before connecting real sources or enabling automation.

The repository's tests and any release validation record describe exactly what was checked. Installation on your machine requires its own verification.

## Runtime configuration

The [runner guide](../services/praxis45/PUBLIC-RUNTIME.md) provides pinned-engine preparation and a standalone rehearsal using the actual Amplifier engine. The current dependency lock targets macOS arm64 with Python 3.14. Prepare dependencies in a new directory and keep the generated engine and rehearsal state out of Git.

The institutional runtime is source for a configured deployment. It needs a compatible execution host, role/model registries, routing policies, storage locations and connectors. See [configuration](configuration.md). Engine-backed operations also require the pinned external engine and dependency closure expected by the code.

Do not run private historical release installers, restore another machine's preimages or copy operational manifests into your installation. An install manifest describes one destination and one set of bytes; it is not transferable authority.

## Schedules and upgrades

No schedule is necessary to use an individual method. Keep one owner for state-changing work and avoid overlapping writers.

For upgrades, inspect the changed source and configuration contract, preserve user state, install into a disposable destination first, and verify the intended operation before switching a live entry point. Record enough identity to restore the preceding version. Rollback must preserve concurrent user edits and unresolved work.
