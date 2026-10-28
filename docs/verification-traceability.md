# Verification and traceability

Praxis records the chain of evidence connecting source bytes, code identity, execution and outputs. Hashes help detect a changed record or artifact. Execution identifiers preserve which worker and attempt produced it. Contract checks determine whether it meets the requested shape; substantive review determines whether it answers the question well.

The implementation uses several related mechanisms rather than one universal ledger:

| Mechanism | Implementation | What it records/checks |
|---|---|---|
| Optional room research trace | `services/praxis-switchboard/research_trace.py` | Sequence, previous-record hash and record hash, with a separate head checkpoint. Entries bind request/prompt/bundle identity, run, response and artifacts. |
| Source-helper operation log | `services/praxis45/source_access.py` and `operation_evidence.py` | Chained helper operations with sequence, nonce and dispatch identity; checks connect returned passages, cached source hashes, worker specification/transcript and receipt. |
| Native provenance records | `services/praxis45/native_ci.py` | Source, Run/Task/Dispatch and artifact hashes, plus primary receipt/decision pointers. |
| Adaptive event outbox | `services/praxis45/adaptive_evidence.py` | Individually hashed, sequence-checked SQLite events. This is not a previous-record hash chain. |
| Public release manifest | The release inventory | Published file identity and hashes. This is package verification, not a runtime execution ledger. |

These records can make alteration evident when their trusted comparison evidence remains available. They are not externally signed or anchored immutability, proof of factual truth, or physical prevention of an out-of-contract operation. Room tracing is optional and must not be described as universal coverage.

An operator should be able to trace a claim or artifact back to its actual source and execution record, identify missing coverage, and distinguish an original attempt from a retry. A matching hash cannot establish that a source was authoritative, an interpretation was sound, or an outcome was delivered to its intended recipient.
