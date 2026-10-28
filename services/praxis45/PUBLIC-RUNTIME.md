# Praxis 4.5 runner and configured runtime

Praxis supplies agents, skills, rules, memory contracts, and bounded execution
adapters. Orca supplies the interaction surface and native coordination.
Amplifier's pinned `PipelineEngine` owns graph traversal. The public runner below
executes the actual engine with synthetic node backends, so it can be prepared
and exercised without an author's installation, provider account, or registry.

## Prepare and run the engine

The exact published wheel closure requires **macOS arm64 and Python 3.14**.
Other platforms need a separately reviewed dependency lock; do not silently
replace hashes or install the latest packages. From the public repository root:

```sh
python3 scripts/prepare-engine.py --download --destination "$PWD/.praxis-engine"
python3 scripts/praxis-runner.py verify --engine-root "$PWD/.praxis-engine"
python3 scripts/praxis-runner.py rehearse --engine-root "$PWD/.praxis-engine" --output "$PWD/.praxis-rehearsal"
```

`--download` explicitly retrieves the pinned upstream archive and exact wheels.
For offline preparation, replace it with both `--source-tree /path/to/source`
and `--wheelhouse /path/to/wheels`. The destination must be new. The preparation
command creates only that directory and its virtual environment. It does not
set an active-release pointer, start services, or launch agents.

The public dependency descriptor contains no commissioning-machine paths.
Its hash is bound by `engine-lock.json`. The upstream commit, 265 source file
hashes, package versions, and 42 wheel hashes are retained from the reviewed
source lock. Preparation verifies all of them before execution. The runtime
verifier checks source bytes, descriptor bytes, wheel evidence, and installed
package versions; it does not claim signatures or external attestation.

The rehearsal exercises successful progression, failure stopping successors,
uncertain contact stopping traversal, unsafe retry outcomes being rejected,
and durable entry intents refusing re-entry. It writes a receipt with graph
and artifact hashes. This is synthetic real-engine verification, not native
agent qualification or evidence that an answer is correct. Release preparation
was verified both from local inputs and by downloading the pinned upstream
archive and all 42 wheels into a new isolated environment.

## Native agents and rooms

The package also contains bounded Deep Research and Fact Check adapters,
room planning and continuation, the native dispatcher, and the general adaptive
runtime. Native use needs your own compatible Orca host, selected provider
accounts, role/model/room registries, decision rights, charters, and deployed
skill entrypoints. No personal configuration, credentials, memory, captured
requests, or live state are supplied.

- `VAULT_ROOT` selects your vault (fallback `~/Research`). Registry files belong
  under `system/memory/institution/`. Context discovery includes only research
  projects, papers, and a development context. User-owned context notes remain
  explicit hash-bound inputs.
- `PRAXIS_ENGINE_SOURCE` selects the prepared `source` directory and
  `PRAXIS_ENGINE_CLOSURE` its `engine-closure.json` for source rehearsals.
- `PRAXIS_REHEARSAL_ROOT` selects the source rehearsal namespace. The existing
  explicit source-only ordinary path uses `PRAXIS45_FRESH_REHEARSAL_ROOT` under
  that namespace's `smokes/` directory. Clean committed source, canonical
  registries, checker bootstrap, custody, and output containment still gate it.
- The checker binds repository/global operating rules and the deployed generic
  `council` review entrypoint. The author's editorial and voice-review policy
  is excluded. Configure both provider skill roots required by the source
  bootstrap before a native request.
- Installed ordinary execution and room engine resolution require a validated
  release identity and activation record. This package does not supply a native
  service installer or fabricate these records. The standalone engine runner
  above does not need them. Do not remove the installed-mode guards.
- `PRAXIS_ARTIFACT_HELPER` can select an artifact command with the
  `artifact-link.py` interface. The author's private publisher is excluded.
- `PRAXIS_ARTIFACT_ROOT` must name an absolute directory for dispatcher artifact
  expansion, research trace outputs, and planning briefs. Planning outputs must
  remain inside it. Worker prompts use the request's configured output format
  and delivery location; they do not impose a personal storage provider or
  update long-term memory. Schedule plans require an explicit valid IANA
  `schedule.timezone`; planning brief dates use UTC.
- `scripts/install-switchboard-runtime.sh` deliberately installs only the
  optional local editor dependencies. It does not activate the runner.

Deep Research and Fact Check retain exact source/code hashes, Run/Task/Dispatch
binding, operation logs, acquisition budgets, and missing-evidence results.
The general adaptive runtime retains custody, finite resource bounds, explicit
authority, and a serialized allowlist of terminal close/rename and workspace
display-name/comment effects. It reports `production_admitted=False`.

Personal routines, inbox and calendar workflows, editorial/voice procedures,
teaching and administrative loaders, and their qualification entrypoints are
excluded as categories. Only generic source-read, lease and evidence guards
were extracted from shared modules. Unsupported routine entrypoints fail closed.
No private qualification fixtures or historical run evidence are included.

## Verification and traceability

`research_trace.py` implements an opt-in room hash chain with sequence,
previous-record hash, record hash and a separate head checkpoint. Source access
operations have their own per-dispatch hash chain, checked by
`operation_evidence.read_log`. Native receipts bind source/code and
Run/Task/Dispatch identities to artifact hashes. `adaptive_evidence.Outbox`
holds individually hashed, sequence-checked events; it is not a linked chain.
These are local tamper-evidence mechanisms, not signatures, proof of factual
correctness, or physical prevention of out-of-protocol actions.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/test_public_runtime.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s services/praxis-switchboard -p 'test_room_ui.py'
```

These tests use synthetic inputs and temporary local state. Editor tests skip
when optional interactive dependencies are absent. `runtime-provenance.json`
records exact original source hashes, projected file hashes and transformations;
private identity maps and source narrative are not distributed.
