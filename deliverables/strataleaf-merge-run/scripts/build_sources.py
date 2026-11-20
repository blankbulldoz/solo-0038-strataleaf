"""Create two independent StrataLeaf databases and export their real logical streams.

Run from anywhere with:
    python3 -I /tmp/strataleaf-merge-run/scripts/build_sources.py

The repository is imported explicitly; the script directory contains no project files.
"""
import hashlib
import json
import sys
from pathlib import Path

REPO = Path("/workspace")
sys.path.insert(0, str(REPO))

from strataleaf import Database  # noqa: E402

ROOT = Path("/tmp/strataleaf-merge-run")
WORK = ROOT / "work"
INPUTS = ROOT / "inputs"
COLLECTION = "segments"

SCHEMA = {
    "fields": {
        "region": "string",
        "sequence": "integer",
        "label": "string",
        "links.parent": "string",
        "links.peer": "string",
    },
    "required": ["region", "sequence", "label"],
}

SOURCE_A = [
    ("block_a", {"region": "east", "sequence": 1, "label": "A-root", "links": {"parent": "seg_01", "peer": "shared_note"}}),
    ("seg_01", {"region": "east", "sequence": 2, "label": "A-seg01", "links": {"parent": "seg_02"}}),
    ("seg_02", {"region": "east", "sequence": 3, "label": "A-seg02", "links": {}}),
    ("shared_note", {"region": "east", "sequence": 4, "label": "A-note", "links": {"peer": "block_a"}}),
    ("a_only", {"region": "east", "sequence": 5, "label": "A-only", "links": {"peer": "seg_05"}}),
    ("missing_probe", {"region": "east", "sequence": 6, "label": "A-dangling", "links": {"parent": "ghost_key"}}),
]

SOURCE_B = [
    ("block_a", {"region": "west", "sequence": 11, "label": "B-root", "links": {"parent": "seg_01", "peer": "shared_note"}}),
    ("seg_01", {"region": "west", "sequence": 12, "label": "B-seg01", "links": {"parent": "shared_note"}}),
    ("seg_02", {"region": "west", "sequence": 13, "label": "B-seg02", "links": {}}),
    ("shared_note", {"region": "west", "sequence": 14, "label": "B-note", "links": {}}),
    ("seg_05", {"region": "west", "sequence": 15, "label": "B-seg05", "links": {"peer": "seg_01"}}),
    ("remote_probe", {"region": "west", "sequence": 16, "label": "B-remote", "links": {"parent": "a_only"}}),
]


def build(source_id, documents):
    folder = WORK / ("source_" + source_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "source.db"
    if path.exists():
        raise SystemExit("source database already exists, refusing to reuse: " + str(path))
    with Database(path) as db:
        db.catalog.create(COLLECTION, SCHEMA)
        batch = db.batch("seed_" + source_id)
        for key, document in documents:
            batch.put(COLLECTION, key, document)
        result = batch.commit()
        exported = db.transfer.export_lines(COLLECTION)
        if exported["revision"] != db.revision:
            raise RuntimeError("export did not freeze the current head")
        observed = {json.loads(line)["key"] for line in exported["lines"]}
        if observed != {key for key, _ in documents}:
            raise RuntimeError("exported keys differ from seeded keys")
        info = db.diagnostics.inspect(deep=True)
        if not info["ok"]:
            raise RuntimeError("source inspection failed: " + repr(info["issues"]))
        fingerprint = db.diagnostics.fingerprint()
        database_id = info["database_id"]
        schema_generation = db.catalog.describe(COLLECTION)["generation"]
    lines = exported["lines"]
    payload = "".join(line + "\n" for line in lines)
    (INPUTS / ("source_" + source_id + ".jsonl")).write_text(payload, encoding="utf-8")
    manifest = {
        "source_id": source_id,
        "database_path": str(path),
        "database_id": database_id,
        "collection": COLLECTION,
        "schema_generation": schema_generation,
        "commit_revision": result.revision,
        "export_revision": exported["revision"],
        "document_count": len(lines),
        "export_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "fingerprint": fingerprint,
        "seed_keys": [key for key, _ in documents],
    }
    (INPUTS / ("source_" + source_id + ".manifest.json")).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    INPUTS.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    build("a", SOURCE_A)
    build("b", SOURCE_B)
