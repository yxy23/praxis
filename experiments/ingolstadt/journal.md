# Ingolstadt scientific journal

This archive preserves all **1,532,391 scientific journal records**, in order, from sequence 0 through 1,532,390. It includes the recorded model requests, provider responses, observations, settlements, controls, recoveries and outcomes. It is a public projection of the experiment journal, not the private research room's conversation ledger.

The original journal is 75,088,281,448 bytes. A fresh complete scan verified every original sequence, previous hash, content hash and canonical serialization, with no errors. The original SHA-256 is `7777ae1a4c10abcfa7ad354d216d7509530f8a1ef3b22486bb0122a685b64e02`; its final chain hash is `1828d84d901471067a5058758954e64ce71dcdf14d37a687777d427e6cd8b402`. The original remains unchanged and is not the byte stream distributed here.

The public projection changes **215 local path fields in 132 records** to portable `archive://` locators. It retains the scientific contents, including scientific author instructions and attribution. Every public row carries the original sequence, previous hash and row hash in `source`, alongside a separate public hash chain. The field map records original-value hashes and exact public replacements without revealing private paths. Unchanged original row hashes can be recomputed from public content; the original hashes of changed rows remain commitments, because the original local paths are withheld.

## Download and verify

Download the three numbered release assets into a `release-assets` directory beside these metadata files. Copy `PARTS-MANIFEST.json` into that directory. The files form one zstd stream split at 1,000,000,000 bytes; concatenate them in numeric order. They total 2,481,269,121 compressed bytes and expand to 75,352,240,251 bytes.

With Python 3 and the `zstd` command installed:

```sh
mkdir -p release-assets
cp PARTS-MANIFEST.json release-assets/
# Place ingolstadt-journal.public.jsonl.zst.part-000 through part-002 there.
python3 verify_parts.py .
```

The verifier checks each part's SHA-256, the joined compressed stream, decompression, every public row hash and sequence, original commitment continuity, all 1,532,259 unchanged original row hashes, and the field map for the 132 changed rows. It streams the decompressed data and does not need to save the 75 GB journal.

To extract it after verification:

```sh
cat release-assets/ingolstadt-journal.public.jsonl.zst.part-* | zstd -d -c > journal.public.jsonl
```

Public journal SHA-256: `8d8f657d188645198eecf63e294b0a549228bfd70ae29eb5ba48daca277e3554`.

Public final chain hash: `f8516e7dc7c793c4ce86f01f5094e79e751800a082c8aba6cbf897ce1b44d31e`.

## Format and evidence limits

The original row hash is SHA-256 of compact ASCII JSON with ordered keys `seq`, `prev`, `event`. The public row hash uses ordered keys `seq`, `prev`, `event`, `source`. Both use Python-compatible `ensure_ascii=True`, separators `(',', ':')`, no NaN values and no key sorting; `hash` is appended after computing the digest. Each row ends with a newline.

`archive://run/` refers to the scientific run-state package, `archive://study-record/` to scientific control/provenance artifacts and `archive://decisions/` to adopted scientific decisions. These are portable archive locators, not network endpoints or executable local paths.

Hash verification establishes byte identity, chain continuity and detectable modification. It does not establish factual truth, correct scientific interpretation, independent substantive review or externally signed immutability. Scientific results require the protocol, code, analysis and limitations supplied with the experiment record. Private correspondence, off-ledger consultation contents, personal workflow/configuration and private reflection drafts are outside the public archive.

The included verification reports describe local preparation and checks. They do not claim that any files have already been uploaded or that the original private journal is publicly downloadable.
