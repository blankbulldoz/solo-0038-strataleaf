#!/usr/bin/env python3
"""Self-check the StrataLeaf history evidence package.

Runs with the Python standard library only and does not import ``strataleaf``.
It re-derives the parts that can be re-derived from the delivered files, so a
recipient can tell whether the package is internally consistent without access
to the temporary source database (which is intentionally not delivered).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
EXPECTED_KINDS = {
    "data/change-process.json": "change_process",
    "data/document-inventory.json": "document_inventory",
    "data/key-explanations.json": "key_explanation",
    "data/gaps.json": "gap_register",
    "data/source-metadata.json": "source_metadata",
}
REQUIRED_VIEW_KIND = "historical_read_view"


def fail(issues, message):
    issues.append(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_sha(value):
    if value is None:
        return None
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def main():
    issues = []
    manifest_path = PACKAGE / "manifest.json"
    if not manifest_path.is_file():
        print(json.dumps({"ok": False, "issues": ["manifest.json is missing"]}, ensure_ascii=False, indent=2))
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # 1. File digests.
    for entry in manifest.get("files", []):
        path = PACKAGE / entry["path"]
        if not path.is_file():
            fail(issues, f"missing file: {entry['path']}")
            continue
        data = path.read_bytes()
        if len(data) != entry["bytes"]:
            fail(issues, f"size mismatch: {entry['path']}")
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            fail(issues, f"sha256 mismatch: {entry['path']}")

    # 2. Record kinds.
    for relative, kind in EXPECTED_KINDS.items():
        path = PACKAGE / relative
        if not path.is_file():
            fail(issues, f"missing file: {relative}")
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("record_kind") != kind:
            fail(issues, f"unexpected record_kind in {relative}: {document.get('record_kind')!r}")
        if not document.get("record_kind_zh"):
            fail(issues, f"missing record_kind_zh in {relative}")

    change_process = json.loads((PACKAGE / "data/change-process.json").read_text(encoding="utf-8"))
    inventory = json.loads((PACKAGE / "data/document-inventory.json").read_text(encoding="utf-8"))
    explanations = json.loads((PACKAGE / "data/key-explanations.json").read_text(encoding="utf-8"))
    gaps = json.loads((PACKAGE / "data/gaps.json").read_text(encoding="utf-8"))

    # 3. Commit sequence and operation uniqueness.
    revision_numbers = [commit["revision"] for commit in change_process["commits"]]
    if revision_numbers != list(range(1, len(revision_numbers) + 1)):
        fail(issues, f"commit revisions are not contiguous from 1: {revision_numbers}")
    expected_operations = {}
    for commit in change_process["commits"]:
        seen = set()
        for operation in commit["operations"]:
            identity = (operation["collection"], operation["key"])
            if identity in seen:
                fail(issues, f"duplicate key in commit {commit['revision']}: {identity}")
            seen.add(identity)
            expected_operations.setdefault(operation["key"], []).append((commit, operation))
            derived = content_sha(operation["document"])
            if derived != operation["content_sha256"]:
                fail(issues, f"operation content hash mismatch for {operation['key']} r{commit['revision']}")

    # 4. Inventory equals change process.
    inventory_keys = [entry["key"] for entry in inventory["keys"]]
    if inventory_keys != list(expected_operations):
        fail(issues, f"inventory key order differs from change process: {inventory_keys}")
    for entry in inventory["keys"]:
        expected = expected_operations.get(entry["key"], [])
        if len(entry["versions"]) != len(expected):
            fail(issues, f"version count mismatch for {entry['key']}")
            continue
        for version, (commit, operation) in zip(entry["versions"], expected):
            if version["actual_version"] != commit["revision"]:
                fail(issues, f"actual_version mismatch for {entry['key']} r{commit['revision']}")
            if version["commit_request_key"] != commit["commit_request_key"]:
                fail(issues, f"commit_request_key mismatch for {entry['key']} r{commit['revision']}")
            if version["operation"] != operation["action"]:
                fail(issues, f"operation mismatch for {entry['key']} r{commit['revision']}")
            if version["content_sha256"] != operation["content_sha256"]:
                fail(issues, f"content hash mismatch for {entry['key']} r{commit['revision']}")

    # 5. Views, derived visibility, and fingerprints.
    view_files = sorted((PACKAGE / "data/views").glob("revision-*.json"))
    if not view_files:
        fail(issues, "no view files exported")
    view_revisions = []
    for path in view_files:
        view = json.loads(path.read_text(encoding="utf-8"))
        if view.get("record_kind") != REQUIRED_VIEW_KIND:
            fail(issues, f"unexpected record_kind in {path.name}")
        revision = view["read_revision"]
        view_revisions.append(revision)
        if path.stem != f"revision-{revision:04d}":
            fail(issues, f"view filename does not match revision: {path.name}")
        records = {record["key"]: record for record in view["records"]}
        if len(records) != len(view["records"]):
            fail(issues, f"duplicate keys in {path.name}")
        if view["document_count"] != len(view["records"]):
            fail(issues, f"document_count mismatch in {path.name}")
        for record in view["records"]:
            if record["actual_version"] > revision:
                fail(issues, f"view {revision} shows a future version for {record['key']}")
            if content_sha(record["document"]) != record["content_sha256"]:
                fail(issues, f"content hash mismatch in {path.name} for {record['key']}")

        # Recompute the API fingerprint from the delivered records.
        content = [{"collection": view["collection"], "key": record["key"],
                    "revision": record["actual_version"], "document": record["document"],
                    "expires_at": record["expires_at"]} for record in view["records"]]
        recomputed = hashlib.sha256(canonical(content).encode("utf-8")).hexdigest()
        if view["fingerprint"]["sha256"] != recomputed:
            fail(issues, f"fingerprint does not match view records: {path.name}")
        if view["fingerprint"]["documents"] != len(content):
            fail(issues, f"fingerprint count mismatch: {path.name}")
        if view["fingerprint"]["revision"] != revision:
            fail(issues, f"fingerprint revision mismatch: {path.name}")

        # Absent keys must equal the keys not visible at this revision.
        visible_keys = set(records)
        for entry in inventory["keys"]:
            visibility = entry["visibility_in_selected_views"].get(str(revision))
            if visibility is None:
                fail(issues, f"key {entry['key']} has no visibility entry for r{revision}")
                continue
            if visibility["visible"] != (entry["key"] in visible_keys):
                fail(issues, f"visibility disagreement for {entry['key']} at r{revision}")
            if visibility["visible"]:
                record = records[entry["key"]]
                if record["actual_version"] != visibility["actual_version"]:
                    fail(issues, f"actual_version mismatch for {entry['key']} at r{revision}")

    if view_revisions != manifest.get("selected_view_revisions"):
        fail(issues, f"view revisions differ from manifest: {view_revisions}")

    # 6. Equal-content explanations must satisfy their own preconditions.
    for item in explanations["explanations"]:
        first = item["first_endpoint"]
        last = item["last_endpoint"]
        if first["content_sha256"] != last["content_sha256"]:
            fail(issues, f"explanation endpoints differ in content: {item['key']}")
        if first["actual_version"] == last["actual_version"]:
            fail(issues, f"explanation endpoints share a version: {item['key']}")
        if not item["intermediate_versions"]:
            fail(issues, f"explanation has no intermediate version: {item['key']}")
        entry = next((entry for entry in inventory["keys"] if entry["key"] == item["key"]), None)
        if entry is None:
            fail(issues, f"explanation references unknown key: {item['key']}")
            continue
        versions = {version["actual_version"]: version for version in entry["versions"]}
        for version in item["intermediate_versions"]:
            recorded = versions.get(version["actual_version"])
            if recorded is None:
                fail(issues, f"intermediate version missing from inventory: {item['key']} r{version['actual_version']}")
            elif recorded["content_sha256"] != version["content_sha256"]:
                fail(issues, f"intermediate content hash mismatch: {item['key']} r{version['actual_version']}")

    # 7. Gap register completeness.
    gap_ids = set()
    for gap in gaps.get("gaps", []):
        for field in ("gap_id", "kind", "summary_zh", "impact_zh", "recovery_zh", "completeness"):
            if not gap.get(field):
                fail(issues, f"gap missing {field}: {gap.get('gap_id')}")
        if gap["gap_id"] in gap_ids:
            fail(issues, f"duplicate gap_id: {gap['gap_id']}")
        gap_ids.add(gap["gap_id"])
    if not any(gap["kind"] == "source_history_reclaimed_after_export" for gap in gaps.get("gaps", [])):
        fail(issues, "gap register does not mark the reclaimed source history")
    reclaimed = next((gap for gap in gaps.get("gaps", [])
                      if gap["kind"] == "source_history_reclaimed_after_export"), None)
    if reclaimed is not None:
        probes = reclaimed.get("probe_results", [])
        if not probes or any(probe["outcome"] != "rejected" for probe in probes):
            fail(issues, "reclaimed-history probes did not all fail as expected")

    # 8. Observations are local and sequential.
    observation_path = PACKAGE / "data" / "observations.jsonl"
    if not observation_path.is_file():
        fail(issues, "observations.jsonl is missing")
    else:
        sequences = []
        for line in observation_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            sequences.append(entry["seq"])
            if entry.get("observation_source") != "local-strataleaf-api":
                fail(issues, f"observation {entry.get('seq')} is not a local API source")
        if sequences != list(range(1, len(sequences) + 1)):
            fail(issues, "observation sequence is not contiguous from 1")

    result = {"ok": not issues, "issues": issues,
              "checked": {"files": len(manifest.get("files", [])),
                          "commits": len(revision_numbers),
                          "views": view_revisions,
                          "keys": len(inventory_keys),
                          "explanations": len(explanations["explanations"]),
                          "gaps": len(gaps.get("gaps", []))}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
