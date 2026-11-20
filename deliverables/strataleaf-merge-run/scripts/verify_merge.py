"""Independently read the merged database and verify references and source provenance.

Every check re-reads the database file rather than trusting merge-time variables.

    python3 -I /tmp/strataleaf-merge-run/scripts/verify_merge.py
"""
import json
import sys
from pathlib import Path

REPO = Path("/workspace")
sys.path.insert(0, str(REPO))

from strataleaf import Database  # noqa: E402

ROOT = Path("/tmp/strataleaf-merge-run")
MERGED = ROOT / "merged"
COLLECTION = "merged_segments"

REFERENCE_PATHS = ("links.parent", "links.peer")

failures = []


def check(name, condition, detail=None):
    entry = {"check": name, "ok": bool(condition)}
    if detail is not None:
        entry["detail"] = detail
    if not condition:
        failures.append(entry)
    return entry


def refs(document):
    out = []
    for path in REFERENCE_PATHS:
        parts = path.split(".")
        node = document
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                node = None
                break
        if isinstance(node, str):
            out.append((path, node))
    return out


def main():
    merged_rows = [json.loads(line) for line in
                   (MERGED / "merged.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    source_map = [json.loads(line) for line in
                  (MERGED / "source_map.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    conflicts = json.loads((MERGED / "conflicts.json").read_text(encoding="utf-8"))
    import_report = json.loads((MERGED / "import_report.json").read_text(encoding="utf-8"))

    checks = []
    with Database(MERGED / "merged.db") as db:
        records = db.query(COLLECTION, limit=1000).records
        keys = [record.key for record in records]
        checks.append(check("no duplicate keys on disk", len(keys) == len(set(keys)), {"count": len(keys)}))
        checks.append(check("disk document count equals JSONL", len(keys) == len(merged_rows),
                            {"disk": len(keys), "jsonl": len(merged_rows)}))
        checks.append(check("disk keys equal JSONL keys", sorted(keys) == sorted(item["key"] for item in merged_rows)))
        checks.append(check("import revision is the frozen export revision",
                            import_report["import_revision"] == 1 and db.revision == 1,
                            {"head": db.revision}))

        # Documents on disk must be byte-identical to the delivered JSONL documents.
        jsonl_by_key = {item["key"]: item["document"] for item in merged_rows}
        same = all(jsonl_by_key[record.key] == record.document for record in records)
        checks.append(check("stored documents equal merged JSONL", same))

        # Every source_map row round-trips back to its original source line.
        sources = {source_id: [json.loads(line) for line in
                               (ROOT / "inputs" / ("source_" + source_id + ".jsonl"))
                               .read_text(encoding="utf-8").splitlines() if line.strip()]
                   for source_id in ("a", "b")}
        originals = {(source_id, item["key"]): item for source_id, items in sources.items() for item in items}
        map_ok = True
        reverse_ok = True
        for row in source_map:
            original = originals[(row["source_id"], row["source_key"])]
            if row["merged_key"] != f"{row['source_id']}__{row['source_key']}":
                map_ok = False
            stored = next(record.document for record in records if record.key == row["merged_key"])
            # Reverse the declared reference rewrites and expect the exact original document.
            reverted = json.loads(json.dumps(stored))
            for edit in row["reference_edits"]:
                parts = edit["field"].split(".")
                node = reverted
                for part in parts[:-1]:
                    node = node[part]
                if node.get(parts[-1]) != edit["to"]:
                    reverse_ok = False
                node[parts[-1]] = edit["from"]
            if reverted != original["document"]:
                reverse_ok = False
        checks.append(check("source_map covers each source document exactly once",
                            len(source_map) == len(originals) and
                            {(row["source_id"], row["source_key"]) for row in source_map} == set(originals),
                            {"rows": len(source_map), "source_documents": len(originals)}))
        checks.append(check("source_map key naming is reversible", map_ok))
        checks.append(check("removing declared rewrites restores original documents", reverse_ok))

        # Cross-source leakage: no reference points into a different source's namespace.
        source_of = {row["merged_key"]: row["source_id"] for row in source_map}
        leakage = []
        for record in records:
            own = source_of[record.key]
            for path, target in refs(record.document):
                if target.startswith("a__") or target.startswith("b__"):
                    if not target.startswith(own + "__"):
                        leakage.append({"key": record.key, "field": path, "target": target})
        checks.append(check("no reference crosses source namespaces", not leakage, {"leakage": leakage}))

        # In-source rewrites must land on an existing merged key of the same source.
        resolved_targets = set(row["merged_key"] for row in source_map)
        dangling = []
        for record in records:
            for path, target in refs(record.document):
                if target.startswith("a__") or target.startswith("b__"):
                    if target not in resolved_targets:
                        dangling.append({"key": record.key, "field": path, "target": target})
        checks.append(check("all rewritten references resolve inside the merged database",
                            not dangling, {"dangling": dangling}))

        # Unresolved references must remain exactly as declared, never silently retargeted.
        unresolved = {entry["merged_key"] + "|" + entry["field"]: entry["target"]
                      for entry in conflicts["unresolved_references"]}
        observed_unresolved = {}
        for record in records:
            for path, target in refs(record.document):
                if not target.startswith(("a__", "b__")):
                    observed_unresolved[record.key + "|" + path] = target
        checks.append(check("unresolved references match the conflict list exactly",
                            observed_unresolved == unresolved,
                            {"observed": observed_unresolved, "listed": unresolved}))

        # The ambiguous cases specifically: same name exists in the other source, still untouched.
        ambiguous = []
        for entry in conflicts["ambiguous_references"]:
            other = "b" if entry["source_id"] == "a" else "a"
            stored = next(record.document for record in records if record.key == entry["merged_key"])
            parts = entry["field"].split(".")
            node = stored
            for part in parts[:-1]:
                node = node[part]
            ambiguous.append({
                "merged_key": entry["merged_key"],
                "field": entry["field"],
                "value_on_disk": node[parts[-1]],
                "target": entry["target"],
                "other_source_has_same_name": (other + "__" + entry["target"]) in source_of,
                "untouched": node[parts[-1]] == entry["target"],
            })
        checks.append(check("ambiguous references left untouched even though the other source has the name",
                            all(item["untouched"] and item["other_source_has_same_name"] for item in ambiguous),
                            {"cases": ambiguous}))

        # Full revision chain: one atomic import commit, and history reads agree.
        commits = db.feed.read(0)
        checks.append(check("import created exactly one commit", len(commits) == 1, {"commits": len(commits)}))
        historical = db.get(COLLECTION, "a__block_a", 1).document
        checks.append(check("historical read at import revision equals head read",
                            historical == db.get(COLLECTION, "a__block_a").document))

        inspection = db.diagnostics.inspect(deep=True)
        checks.append(check("diagnostics inspection passes", inspection["ok"], inspection["issues"]))
        checks.append(check("logical fingerprint matches import report",
                            db.diagnostics.fingerprint() == import_report["fingerprint"]))

    # Nesting of the collision scenario itself: both same-named documents survive.
    shared = conflicts["key_space"]["shared_keys"]
    maps_ok = all(f"a__{key}" in {row["merged_key"] for row in source_map} and
                  f"b__{key}" in {row["merged_key"] for row in source_map} for key in shared)
    checks.append(check("every shared key survives as two distinct documents", maps_ok,
                        {"shared_keys": shared}))

    report = {"ok": not failures, "checks": checks, "failures": failures}
    (MERGED / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "checks": len(checks), "failures": failures},
                     ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
