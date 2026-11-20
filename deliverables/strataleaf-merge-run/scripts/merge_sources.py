"""Organize two real StrataLeaf exports into one collision-free key space and import it.

Reads the JSONL produced by build_sources.py.  All rewriting happens here, outside the
database; the new database is written only through Database.transfer.import_lines.

Run with:
    python3 -I /tmp/strataleaf-merge-run/scripts/merge_sources.py
"""
import hashlib
import json
import sys
from pathlib import Path

REPO = Path("/workspace")
sys.path.insert(0, str(REPO))

from strataleaf import Database  # noqa: E402

ROOT = Path("/tmp/strataleaf-merge-run")
INPUTS = ROOT / "inputs"
MERGED = ROOT / "merged"
COLLECTION = "segments"
TARGET_COLLECTION = "merged_segments"
PREFIX = {"a": "a__", "b": "b__"}

# Declared cross-document reference fields.  Only these paths are rewritten.
REFERENCE_PATHS = ("links.parent", "links.peer")

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


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_source(source_id):
    jsonl_path = INPUTS / ("source_" + source_id + ".jsonl")
    manifest_path = INPUTS / ("source_" + source_id + ".manifest.json")
    raw = jsonl_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = sha256_text(raw)
    if actual != manifest["export_sha256"]:
        raise SystemExit(f"source {source_id} export digest mismatch: {actual} != {manifest['export_sha256']}")
    if manifest["document_count"] != len(raw.splitlines()):
        raise SystemExit(f"source {source_id} manifest count disagrees with JSONL")
    documents = []
    for number, line in enumerate(raw.splitlines(), start=1):
        item = json.loads(line)
        if set(item) != {"key", "document", "expires_at"}:
            raise SystemExit(f"source {source_id} line {number} has unexpected fields")
        if not isinstance(item["key"], str) or not isinstance(item["document"], dict):
            raise SystemExit(f"source {source_id} line {number} is malformed")
        documents.append(item)
    keys = [item["key"] for item in documents]
    if len(set(keys)) != len(keys):
        raise SystemExit(f"source {source_id} export contains duplicate keys")
    if sorted(keys) != sorted(manifest["seed_keys"]):
        raise SystemExit(f"source {source_id} keys disagree with manifest")
    return {"manifest": manifest, "documents": documents, "sha256": actual}


def get_path(document, path):
    node = document
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_path(document, path, value):
    parts = path.split(".")
    node = document
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            return False
        node = node[part]
    if parts[-1] not in node:
        return False
    node[parts[-1]] = value
    return True


def walk_paths(document, path):
    """Yield (location, value) for dotted path, expanding lists at each level."""
    parts = path.split(".")

    def step(node, index):
        if index == len(parts):
            yield node
            return
        part = parts[index]
        if isinstance(node, list):
            for item in node:
                yield from step(item, index)
        elif isinstance(node, dict) and part in node:
            yield from step(node[part], index + 1)

    yield from step(document, 0)


def merged_key(source_id, key):
    return PREFIX[source_id] + key


def main():
    MERGED.mkdir(parents=True, exist_ok=True)
    database_path = MERGED / "merged.db"
    if database_path.exists():
        raise SystemExit("merged database already exists, refusing to overwrite: " + str(database_path))

    sources = {source_id: load_source(source_id) for source_id in ("a", "b")}
    key_sets = {source_id: {item["key"] for item in source["documents"]} for source_id, source in sources.items()}

    shared_keys = sorted(key_sets["a"] & key_sets["b"])
    only_keys = {"a": sorted(key_sets["a"] - key_sets["b"]), "b": sorted(key_sets["b"] - key_sets["a"])}

    conflicts = []
    for key in shared_keys:
        conflicts.append({
            "kind": "same_key_in_both_sources",
            "source_key": key,
            "kept_as": [merged_key("a", key), merged_key("b", key)],
            "decision": "keep both under source-prefixed keys; no overwrite, no cross-source merge",
        })

    merged_documents = []
    source_map = []
    unresolved = []
    reference_edits = []

    for source_id in ("a", "b"):
        source = sources[source_id]
        manifest = source["manifest"]
        known_keys = key_sets[source_id]
        other_id = "b" if source_id == "a" else "a"
        for item in source["documents"]:
            original_key = item["key"]
            address = merged_key(source_id, original_key)
            document = json.loads(json.dumps(item["document"], ensure_ascii=False))
            edits = []
            for path in REFERENCE_PATHS:
                for value in list(walk_paths(document, path)):
                    if not isinstance(value, str):
                        unresolved.append({
                            "source_id": source_id,
                            "source_key": original_key,
                            "merged_key": address,
                            "field": path,
                            "target": value,
                            "reason": "reference_value_is_not_a_string",
                        })
                        continue
                    if value in known_keys:
                        target = merged_key(source_id, value)
                        if not set_path(document, path, target):
                            raise SystemExit(f"cannot rewrite {path} in {source_id}/{original_key}")
                        edits.append({"field": path, "from": value, "to": target,
                                      "resolution": "resolved_within_source"})
                    else:
                        reason = "target_absent_in_own_source"
                        if value in key_sets[other_id]:
                            reason = "target_absent_in_own_source_but_same_name_exists_in_other_source"
                        unresolved.append({
                            "source_id": source_id,
                            "source_key": original_key,
                            "merged_key": address,
                            "field": path,
                            "target": value,
                            "reason": reason,
                            "decision": "left as original dangling text; not retargeted to another source",
                        })
            if edits:
                reference_edits.append({"source_id": source_id, "source_key": original_key,
                                        "merged_key": address, "edits": edits})
            merged_documents.append({"key": address, "document": document,
                                     "expires_at": item["expires_at"]})
            source_map.append({
                "source_id": source_id,
                "source_database_id": manifest["database_id"],
                "source_collection": manifest["collection"],
                "source_export_revision": manifest["export_revision"],
                "source_export_sha256": source["sha256"],
                "source_key": original_key,
                "merged_key": address,
                "merged_document_sha256": sha256_text(json.dumps(document, ensure_ascii=False, sort_keys=True,
                                                               separators=(",", ":"))),
                "reference_edits": edits,
            })

    merged_documents.sort(key=lambda item: item["key"])
    lines = [json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
             for item in merged_documents]
    if len({json.loads(line)["key"] for line in lines}) != len(lines):
        raise SystemExit("merged key space is not unique")
    if len(lines) != sum(len(source["documents"]) for source in sources.values()):
        raise SystemExit("merged document count does not equal the sum of sources")

    (MERGED / "merged.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (MERGED / "source_map.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in source_map) + "\n",
        encoding="utf-8")

    conflict_report = {
        "sources": {source_id: {
            "database_id": source["manifest"]["database_id"],
            "collection": source["manifest"]["collection"],
            "export_revision": source["manifest"]["export_revision"],
            "export_sha256": source["sha256"],
            "document_count": len(source["documents"]),
            "source_keys": sorted(key_sets[source_id]),
        } for source_id, source in sources.items()},
        "key_space": {
            "rule": "merged key = source prefix ('a__' or 'b__') + original key",
            "shared_keys": shared_keys,
            "shared_key_count": len(shared_keys),
            "source_only_keys": only_keys,
            "merged_key_count": len(lines),
        },
        "conflicts": conflicts,
        "reference_edits": reference_edits,
        "unresolved_references": unresolved,
        "unresolved_count": len(unresolved),
        "ambiguous_references": [entry for entry in unresolved
                                 if entry["reason"].endswith("other_source")],
    }
    (MERGED / "conflicts.json").write_text(
        json.dumps(conflict_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Import only through the existing transfer API; the batch is atomic and rejects duplicates.
    with Database(database_path) as db:
        db.catalog.create(TARGET_COLLECTION, SCHEMA)
        result = db.transfer.import_lines(TARGET_COLLECTION, lines, request_key="merge_r066")
        info = db.diagnostics.inspect(deep=True)
        if not info["ok"]:
            raise SystemExit("merged database inspection failed: " + repr(info["issues"]))
        report = {
            "merged_database": str(database_path),
            "merged_database_id": info["database_id"],
            "target_collection": TARGET_COLLECTION,
            "import_revision": result.revision,
            "import_count": result.count,
            "head_after_import": db.revision,
            "document_count": len(db.query(TARGET_COLLECTION, limit=1000).records),
            "fingerprint": db.diagnostics.fingerprint(),
            "inspection": info,
            "merged_jsonl_sha256": sha256_text("\n".join(lines) + "\n"),
        }
    (MERGED / "import_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"import_revision": report["import_revision"], "count": report["import_count"],
                      "shared_keys": shared_keys, "unresolved": len(unresolved)}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
