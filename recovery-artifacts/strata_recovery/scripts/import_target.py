"""Import the recovery materials into an isolated new library (actual import).

Inputs (data dir):
  recovery/mapping.json          - collection schemas for the target library
  recovery/importable_docs.jsonl - recovered lifecycle documents (target keys)
  recovery/importable_refs.jsonl - references rewired to recovered target keys

Output:
  isolated/target.db   - the isolated new library
  import_result.json   - commit receipts of the two atomic imports
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")

from strataleaf import Database


def main(data_dir):
    data = Path(data_dir)
    recovery = data / "recovery"
    isolated = data / "isolated"
    isolated.mkdir(parents=True, exist_ok=True)
    target_path = isolated / "target.db"
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(target_path) + suffix)
        if stale.exists():
            stale.unlink()

    mapping = json.loads((recovery / "mapping.json").read_text())
    docs_lines = [line for line in (recovery / "importable_docs.jsonl").read_text().splitlines() if line]
    refs_lines = [line for line in (recovery / "importable_refs.jsonl").read_text().splitlines() if line]

    with Database(target_path) as db:
        for collection, schema in mapping["collections"].items():
            db.catalog.create(collection, schema)
        docs_result = db.transfer.import_lines("docs", docs_lines, request_key="recover_docs")
        refs_result = db.transfer.import_lines("refs", refs_lines, request_key="recover_refs")
        state = db.diagnostics.inspect(deep=True)

    result = {"target_db": str(target_path),
              "docs_import": docs_result.__dict__, "refs_import": refs_result.__dict__,
              "head": state["head"], "ok": state["ok"]}
    (data / "import_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/strata_recovery/data")
