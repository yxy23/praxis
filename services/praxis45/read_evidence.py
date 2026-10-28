from pathlib import Path
from adaptive_contract import sha,digest
def validate_read(request, receipt):
    if receipt.get("reading_kind") == "cloud":
        raise ValueError("Unsupported reader profile outside this public package")
    required = {"source_id", "source_sha256", "task_sha256", "output", "output_sha256",
                "reader_completion_id", "status", "coverage_complete", "chunks"}
    if not required <= set(receipt) or receipt["status"] != "complete" or receipt["coverage_complete"] is not True:
        raise ValueError("incomplete local-read receipt")
    if (receipt["source_id"], receipt["source_sha256"], receipt["task_sha256"]) != (
            request["source_id"], request["source_sha256"], request["task_sha256"]):
        raise ValueError("source/task read binding differs")
    if sha(request["path"]) != request["source_sha256"] or sha(receipt["output"]) != receipt["output_sha256"]:
        raise ValueError("source/note hash changed")
    if not receipt["reader_completion_id"] or not Path(receipt["output"]).read_text().strip():
        raise ValueError("reader completion/nonempty note required")
    chunks = receipt["chunks"]
    if len(chunks) != len(request["chunks"]) or {r["id"]: r["sha256"] for r in chunks} != request["chunks"]:
        raise ValueError("complete exact source chunks required")
    if any(not r.get("locators") for r in chunks):
        raise ValueError("coverage locators required")
    return {"state": "succeeded", "source_id": request["source_id"], "receipt_sha256": digest(receipt)}
