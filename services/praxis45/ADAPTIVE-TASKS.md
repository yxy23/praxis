# General adaptive tasks

`/adaptive <objective>` commissions Athena to plan and canonical Leo to execute
registered native seats through the existing dispatcher and pinned engine. Ordinary
writing, editable decks, code/tests and websites use `native_task`, with actual
file declarations and explicit checks. The user need not type filesystem paths
or URLs: Leo resolves them from the request's canonical context and applicable
skills, then binds that scoped assignment to the real Switchboard request.

## Scope and authority

A node declares `task`, registered `route: {seat: ...}`, `scope` and actual
`outputs: [{path, format}]`, plus `checks: [{argv, cwd, timeout_seconds}]`.
Scope has exactly `workspace`, `write_roots`, `skills`, `tools`, `network`
(`none`, `read`, `publish`) and `publication` (null for preparation).
Project-local dependency installation inside write scope is supported when the
task requires it. System-wide/Praxis installation and nested workers are excluded.
Available host tools, registered models, original request and current instructions
remain authoritative. Scope is an instruction boundary, not an OS sandbox.

The graph's `authorizing_event` binds `request_id`, `event_id`,
`user_event_sha256`, `context_room_id` and `context_digest` to the existing
machine-created request record and exact `request.md` bytes. Graph prose cannot
supply authority. After reconciling the plan against that request and its context,
Leo writes a coordinator assignment JSON keyed by native task node ID:

```json
{"write_brief": {"scope": {"workspace": "/resolved/project", "write_roots": ["/resolved/project/draft"], "skills": [], "tools": ["shell", "filesystem"], "network": "read", "publication": null}, "authority": {"request_quote": "Create a briefing for the project", "reason": "Resolved the existing project folder and bounded draft task", "context_bindings": {"project": {"path": "/canonical/project.md", "sha256": "ACTUAL_PROJECT_HASH"}}, "publication_authorized": false}}}
```

Run `adaptive_general.py authorize --root ROOT --logical GRAPH --graph-id ID
--assignment ASSIGNMENTS`, then the existing `compile` and `execute` commands.
Authorize writes immutable per-node scope handoff files inside the original
request output directory. It creates no role, policy, scheduler or parallel task
store and asks for no duplicate human approval. **The coordinator is trusted to
interpret request/context authority**, including negative constraints; this is
not a semantic authorization proof by keyword matching. The graph must match
the request-bound assignment exactly, including its pinned task, outputs, route and check commands. A new/revised node needs its own assignment.

For authorized publication, scope includes `{argv, cwd, readback_url}` and the
coordinator's assignment explicitly authorizes publication from supporting
request/context evidence. HTTPS or explicit HTTP loopback is accepted; no public
HTTP fallback or redirects away from the exact destination are allowed. Workers
perform the single authorized publication and retain its real receipt; the runner
only reads back. The response body must hash-match a declared output, as well as
the worker's readback hash. Declare the actual built entry file when deployment
transforms source. Dynamic or authenticated hosting can add optional `verification` to the
publication declaration: `{artifact_path, marker}` uses independent HTTP marker
readback, while `{artifact_path, marker, argv, cwd, timeout_seconds}` runs an
existing authenticated host tool through the same independent check mechanism.
The command receives the exact resolved destination as its own argv element;
its successful stdout must be JSON `{url, content}` reporting the actual final
destination and returned content. A different final destination is refused; the
content and the declared output/build artifact must both contain
the approved stable content/version marker. The runner records the actual build
hash and command argv/cwd/exit/stdout evidence, not a worker boolean. Use an
explicit version marker where versions matter. A content marker establishes only
that content check, not a visual or full functional acceptance claim.

Authentication stays in existing host tool sessions/environment. Never put literal
secrets in graph data, receipts or command output. Native HTTP readback refuses
redirects away from the exact destination. Authenticated check commands are
trusted repeatable verifiers: they must enforce that same destination/redirect
boundary and return meaningful content evidence. Their behavior is commissioned
and hash-bound by the coordinator, not inferred from an arbitrary worker claim.
No provider-specific publishing adapter or credential store is introduced.


## Evidence and continuation

Outputs must be nonempty regular files inside resolved write roots. The runner
checks UTF-8/JSON/HTML, PDF signature/trailer and ZIP/OOXML members/XML when
declared; container checks establish structure, not appearance or prose quality.
Explicit build/test/check argv run independently with `shell=False`, declared
cwd and finite timeout; exit code and stdout/stderr are durable evidence.
Checks must be repeatable verification, never publication or consequential actions.
Original output, worker-artifact and publication-receipt bytes are captured before
independent validation/checks/readback, with original path/hash and source/native
identity recorded. Missing bytes cannot be supplied later as an original. Successful
validation and reconciliation consume these retained bytes.

Bound read-only references use `role: reference`; they are copied before launch
and retain original path/hash and current-source drift disclosure. Mutable edit
targets cannot be bound references. Operating sources use `role: operating`:
current path/hash is recorded per launch and workers read the live instructions.
An unrelated GLOBAL-RULES edit therefore does not invalidate accepted reference
proof. Known operating instruction filenames are classified live even when an
older planner omits the role; they cannot be frozen as references. Neither
snapshots nor old assignments override current operating instructions.

To continue, preserve successful node declarations and reference identities,
add new work/revision nodes and use a **fresh occurrence root**. Bind the prior
compiled `source_bindings` snapshot paths for prior references, including when the
original file has since changed. Do not rebind the changed live original and call
it the same input. New sources may be added for new work:

```
/adaptive continue --continue-from OLD_ROOT --logical GRAPH --policy POLICY --graph-id ID --root NEW_ROOT --origin athena
/adaptive run --root NEW_ROOT --expected-compile-sha256 COMPILE_RECEIPT_FILE_HASH --engine-source PINNED_SOURCE --engine-closure PINNED_CLOSURE
```

The equivalent CLI is `adaptive_general.py continue` with the same arguments.
It binds original compile/intent, validated per-node checkpoints and available
final results. At execution it rechecks original native settlement, reference
identity, dependencies, retained outputs/check hashes and request lineage.
Validated checkpoints can be reused after a missing final engine result. The
new engine traversal returns reused receipts with original native identities;
only new ready work launches. There is no upstream resume or runtime DAG rewrite.
A durable per-request fence prevents new roots or concurrent continuations from
bypassing the original occurrence. No automatic retries are enabled.

A settled failed local-only task can be replaced by an explicit new `revision_of`
node with fresh write roots/output paths and a new scoped assignment; remove the
failed node from the new graph. Preserve successful native nodes; structural
join gates may be replaced. Unsettled/missing original native identity and
uncertain housekeeping effects stay held; they are outside task reconciliation.
A crash before an original validated checkpoint does not certify its output.
Changed or removed original reference inputs cannot reuse stale results; additional
sources may support new revision nodes while prior receipts retain their original
source coverage. Plan revised work explicitly.

## Original task reconciliation

Prefer a separate build/test task before a publication task, and instruct the
publication worker to copy the retained, validated build without rebuilding it.
The dependency gates publication start on successful build checks. Default readback
verifies the publication task's declared output; it does not itself prove that
output is byte-identical to the upstream build. Declare an explicit comparison
against the retained build when that identity is required. Publication still needs
independent readback afterward, and a transient readback failure never permits a
second publish.

For a settled native task with a failed output/check/readback checkpoint, run:

```
adaptive_general.py reconcile --root ORIGINAL_ROOT --node NODE --expected-compile-sha256 ORIGINAL_COMPILE_RECEIPT_FILE_HASH
```

The room equivalent is `/adaptive reconcile` with those same flags. The deterministic
API is `adaptive_general.reconcile_entry(root, node_id, expected_compile_sha256,
*, fixture_authority=None)`. It returns `status: reconciled` only after verifying
original request authority, exact compile/node/assignment/source bindings, captured
native settlement, closed layer custody, and unchanged original output, worker
artifact and publication-receipt bytes. Only declared repeatable checks and
readback run. No worker starts, publication argv, external-effect replays, engine
resume or checkpoint edits occur. Check/readback commands remain trusted repeatable
verifiers under the coordinator boundary above; this is not a new OS sandbox.

Each attempt writes a unique `reconciliations/NODE/ATTEMPT/receipt.json` and separate
check evidence. A failed attempt stays `held`, retaining its reason/check results.
Successful reconciliation records original/retained bindings and a separate verified
receipt; the original failed node checkpoint and engine result remain unchanged.
The request/occurrence fence is shared with traversal and continuation, is held
through validation, and does not advance lineage on a rejected check. Concurrent
operations and reconciliation against an occurrence superseded by a continuation
refuse. A successful original is reconciled once; use continuation afterward.

Reconcile before compiling the fresh continuation so its compile receipt binds the
reconciliation evidence. Preserve the original publication node unchanged, then
continue normally. The new traversal consumes the verified original native identity,
retained outputs/receipt, and verified checks with zero relaunch or second publication.
New or older roots lacking captured original bytes/settlement stay held. Reconciliation
cannot repair missing or edited originals, change check declarations, fix a build,
or widen authority. Plan fresh local-only revision work when appropriate.

## Executable synthetic example

Run `adaptive_task_example.py --fixture --root /fresh/occurrence --workspace
/isolated/output` to prepare fictional writing, editable PPTX, code/unittest and
HTML nodes. `--publish-url http://127.0.0.1:PORT/exact-path` optionally adds a
separate publication node after the site build validation gate, with a single
fictional HTTP PUT to an operator-owned local fixture server. The helper
requires a fresh root, prints exact authorize/compile argv and an execute prefix,
and launches **zero** workers. Execute those argv only for an authorized bounded
native demonstration. Supply the installed pinned interpreter using `--python`.
The fixture authority path/hash must accompany authorize, compile and execute;
ordinary real requests use the canonical request store and omit those flags.

Public offline checks are in `scripts/test_public_runtime.py` and
`scripts/test_adaptive_tasks_public.py`. They use invented inputs and temporary
files; no native agent or publication action is launched. The private source's
larger native transport fixtures and operational records are excluded. Native
end-to-end qualification, visual review and installation belong to each configured
deployment. Format and engine checks do not establish substantive quality or
learning benefit.
