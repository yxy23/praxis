# Ingolstadt scientific evidence archive

The archive preserves the **1,532,391-record experimental journal**, its hash commitments, the frozen study code and assets, all canonical run-state records, scientific decisions and author instructions, and the saved-data analysis and accepted outputs. It accompanies the [ICIS supplementary materials](../../papers/icis-2026/ICIS-Supplementary-Materials-2026-10-28.pdf) and is distinct from the private research room's conversations.

| Component | Contents | Verification |
| --- | --- | --- |
| [Scientific journal](journal.md) | Every event from sequence 0 through 1,532,390, including recorded model requests/responses, controls, recoveries and outcomes | Separate public chain, original row commitments, 215 path-field projections in 132 records |
| Scientific supporting archive | Frozen study source/assets, 108 scheduled world trees, scientific control records, 59 provenance documents, analysis code/data, all 87 listed corrected-run output files plus their manifest | Original/public file hashes, explicit omissions and a finite package inventory |
| [Document projection map](DOCUMENT-PROJECTION.json) | Exact source hashes and line omissions for scientific provenance documents | Scientific author instructions retained; private orientation/personal state spans omitted |
| [Original journal audit](original-journal-audit.json) and [technical review](independent-technical-review.json) | Fresh full original scan, independent public stream verification and focused tamper checks | Integrity evidence, with stated scientific-review limits |

The public journal expands to 75,352,240,251 bytes. Three numbered zstd parts total 2,481,269,121 bytes. The supporting archive is a separate compressed tar asset; [its manifest](ASSET-ARCHIVE-MANIFEST.json) gives the exact final size and SHA-256. [All four evidence assets are published on GitHub](https://github.com/yxy23/praxis/releases/tag/public/v4.5.0). The [publication receipt](PUBLICATION.json) records successful anonymous downloads with matching byte counts and SHA-256 hashes.

Download the three journal parts and `ingolstadt-scientific-assets.tar.zst` into a `release-assets/` directory beside this index. With Python 3 and the `zstd` command installed:

```sh
mkdir -p release-assets
cp PARTS-MANIFEST.json release-assets/
python3 verify_parts.py .
python3 verify_assets.py .
```

Both verifiers stream the data. Journal verification needs no 75 GB extracted copy. Supporting-archive verification checks the compressed identity, every member hash, the explicit package inventory and normalized archive headers without extracting files. After successful verification, the supporting archive can be unpacked into an empty directory with `zstd -d -c release-assets/ingolstadt-scientific-assets.tar.zst | tar -xf - -C YOUR_EMPTY_DIRECTORY`.

The original journal SHA-256 is `7777ae1a4c10abcfa7ad354d216d7509530f8a1ef3b22486bb0122a685b64e02`; the public journal SHA-256 is `8d8f657d188645198eecf63e294b0a549228bfd70ae29eb5ba48daca277e3554`. The original private journal remains unchanged. The public stream has its own chain and does not pretend to be the original byte stream. See [the format and redaction rules](journal.md) and [field commitments](redactions.jsonl).

The archive's `README.md`, `ASSET-MANIFEST.json`, `PACKAGE-MANIFEST.json`, `DOCUMENT-PROJECTION.json` and `projection-changes.jsonl` describe its finite scientific scope. It excludes personal workflows/configuration, private correspondence, off-ledger consultation contents, private reflections, private orientation bookkeeping, virtual environments and redundant local caches. Retained scientific author instructions, attribution and contact email are not treated as personal-workflow material.

This is an evidence archive, not a turnkey live deployment. Portable `archive://` locators need local resolution; original source manifests and their public projections have different identities where paths or private context were changed. All 396 included Python files passed [syntax parsing](python-syntax-verification.json), but packaging does not claim a fresh experimental run or a complete independent replication. Historical qualification, noncompletion, exposure and scientific-analysis limitations remain part of the record. Hashes establish identity and detectable modification; they do not prove scientific correctness or externally signed immutability.
