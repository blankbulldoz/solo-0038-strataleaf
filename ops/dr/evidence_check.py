"""Re-verify a drill evidence bundle without trusting its report.

``artifacts.sha256`` pins every database, manifest and report byte.  This
module recomputes those digests, re-verifies each published backup from its
manifest, and re-derives the fact comparison for the restored files.  A
reviewer can therefore challenge the drill's claims with independent numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

from .backup import load_manifest, verify_manifest
from .facts import compare_snapshot, file_digest, read_facts, summary


class EvidenceError(Exception):
    pass


def _parse_sums(path: Path) -> dict:
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split("  ", 1)
        entries[name] = digest
    return entries


def check_artifacts(root: str | Path) -> list:
    root = Path(root).resolve()
    sums_path = root / "evidence" / "artifacts.sha256"
    if not sums_path.is_file():
        raise EvidenceError("evidence bundle has no artifacts.sha256")
    recorded = _parse_sums(sums_path)
    checks = []
    for name, expected in sorted(recorded.items()):
        target = root / name
        actual = file_digest(target) if target.is_file() else None
        checks.append({"check": "artifact_sha256", "artifact": name,
                       "ok": actual == expected, "expected": expected, "actual": actual})
    return checks


def check_backups(root: str | Path) -> list:
    root = Path(root).resolve()
    checks = []
    for manifest_file in sorted(root.rglob("*" + ".manifest.json")):
        if manifest_file.name.endswith(".failed.manifest.json"):
            payload = json.loads(manifest_file.read_text(encoding="utf-8"))
            checks.append({"check": "failed_backup_has_no_archive", "manifest": str(manifest_file),
                           "ok": payload.get("error") is not None, "detail": payload.get("error")})
            continue
        archive = Path(str(manifest_file)[: -len(".manifest.json")])
        result = verify_manifest(archive)
        checks.append({"check": "backup_manifest_verifies", "archive": str(archive),
                       "ok": result["ok"],
                       "failed": [item for item in result["checks"] if not item["ok"]]})
    return checks


def check_restore(root: str | Path, report_name: str = "drill-report.json") -> list:
    """Re-derive the restore comparison the report claims, from the archived facts."""
    root = Path(root).resolve()
    report = json.loads((root / "evidence" / report_name).read_text(encoding="utf-8"))
    checks = []
    by_name = {item["scenario"]: item for item in report.get("scenarios", [])}

    restore = by_name.get("restore_isolated_primary")
    if restore:
        facts = read_facts(restore["destination"], hash_file=True)
        archive_manifest = load_manifest(root / "backups" / Path(restore["archive"]).name)
        recorded = {
            "head": archive_manifest["facts"]["head"],
            "floor": archive_manifest["facts"]["floor"],
            "catalog_epoch": archive_manifest["facts"]["catalog_epoch"],
            "schema_sha256": archive_manifest["facts"]["schema_sha256"],
            "index_definition_sha256": archive_manifest["facts"]["index_definition_sha256"],
            "documents_sha256": archive_manifest["facts"]["documents_sha256"],
            "documents_count": archive_manifest["facts"]["documents_count"],
        }
        derived = compare_snapshot(recorded, facts, identity="primary",
                                   source_database_id=archive_manifest["archive"]["database_id"])
        checks.append({"check": "restore_comparison_rederived",
                       "ok": all(item["ok"] for item in derived),
                       "failed": [item for item in derived if not item["ok"]],
                       "facts": summary(facts)})

    final = by_name.get("final_fact_comparison")
    if final:
        replica = read_facts(final["replica"]["path"])
        recorded = final["recovered_source"]
        derived = compare_snapshot(
            {"head": recorded["head"], "floor": replica["floor"],
             "catalog_epoch": recorded["catalog_epoch"],
             "schema_sha256": recorded["schema_sha256"],
             "index_definition_sha256": recorded["index_definition_sha256"],
             "documents_sha256": recorded["documents"]["sha256"],
             "documents_count": recorded["documents"]["count"]},
            replica, identity="follower",
            source_database_id=recorded["identity"]["database_id"])
        derived = [item for item in derived if item["check"] not in
                   ("head_matches_backup", "floor_matches_backup",
                    "archive_checksum_verified_by_library")]
        checks.append({"check": "final_comparison_rederived",
                       "ok": all(item["ok"] for item in derived),
                       "failed": [item for item in derived if not item["ok"]],
                       "facts": summary(replica)})
    return checks


def check_bundle(root: str | Path) -> dict:
    root = Path(root).resolve()
    report_path = root / "evidence" / "drill-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    checks = check_artifacts(root) + check_backups(root) + check_restore(root)
    return {
        "root": str(root),
        "drill_outcome": report.get("outcome"),
        "checks": checks,
        "ok": report.get("outcome") == "success" and all(item["ok"] for item in checks),
    }
