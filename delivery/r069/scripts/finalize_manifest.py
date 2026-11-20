"""Emit SHA-256 checksums and the machine-readable MANIFEST.json.

Pure file hashing plus summarization of the two evidence files; opens nothing
read-write. Run with:
    python3 -I finalize_manifest.py <root_dir> <package_root>
"""
import hashlib
import json
import sys
from pathlib import Path


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(root):
    root = Path(root).resolve()
    files = sorted(p for p in root.rglob("*") if p.is_file()
                   and p.name not in ("SHA256SUMS", "MANIFEST.json"))
    checksums = {}
    for path in files:
        checksums[str(path.relative_to(root))] = sha256_file(path)
    (root / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in checksums.items()),
        encoding="utf-8")

    observations = json.loads((root / "evidence/observations.json").read_text())
    recovery = json.loads((root / "evidence/restore_and_floor_checks.json").read_text())
    ledger = json.loads((root / "build_ledger.json").read_text())

    def point_summary(t):
        point = observations["time_points"][t]
        logical = point["logical_view_via_public_api"]
        physical = point["physical_readonly"]
        return {
            "directory": f"materials/{t}",
            "head": logical["head"], "floor": logical["floor"],
            "catalog_epoch": logical["catalog_epoch"],
            "schema_generation": physical["collections"][0]["generation"],
            "index_definitions": physical["index_defs"],
            "tombstones": [{"key": r["key"], "revision": r["rev"]}
                           for r in physical["tombstone_rows"]],
            "consumers": physical["consumers"], "pins": physical["pins"],
            "retained_commit_revisions": physical["commit_revisions"],
            "visible_document_keys": [r["key"] for r in
                                      logical["visible_documents"]["segments"]["rows"]],
            "logical_fingerprint": logical["fingerprint"],
            "files": point["files"],
            "replication_pack": {
                "header": point["replication_pack_header"],
                "commit_revisions": point["replication_pack_commit_revisions"]},
            "logical_export": point["logical_export"],
            "sqlite_integrity_check": physical["physical"]["integrity_check"],
            "foreign_key_check_rows": physical["physical"]["foreign_key_check_rows"],
            "backup_receipt_hash_reverified": point["receipt_matches_recomputed_sha256"],
        }

    manifest = {
        "schema_version": 1,
        "generated_by": "handoff scripts (public strataleaf API + read-only SQLite)",
        "package": "strataleaf 0.1.0, on-disk format 1",
        "material_classes": {
            "live_documents": "versions rows whose body IS NOT NULL at the read head",
            "historical_baseline": "pre-floor version rows plus the T1 backup that still serves them",
            "tombstones": "versions rows with body IS NULL (deletes, including TTL sweep deletes)",
            "index_definitions": "index_defs rows plus their current-head index_values projection",
            "consumer_positions": "consumers.ack acknowledgements and pins read-retention points",
        },
        "time_points": {t: point_summary(t) for t in ("t1", "t2", "t3")},
        "cross_cutting_evidence": {
            "head_vs_catalog_epoch": observations["head_vs_catalog_epoch"],
            "independent_origins": observations["independent_origins"],
            "byte_copy_control": {k: v for k, v in
                                  observations["byte_copy_control"].items()
                                  if k != "path"},
            "retention_and_feed_boundary": observations["retention_and_feed_boundary"],
            "recovery_and_floor": recovery,
        },
        "build_ledger": ledger,
        "file_checksums": checksums,
    }
    (root / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=False, indent=2),
        encoding="utf-8")
    print(json.dumps({"ok": True, "files_indexed": len(files)}, ensure_ascii=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
