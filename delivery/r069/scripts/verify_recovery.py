"""Public-API recovery and retention verification for the handoff materials.

Uses only the public Database / Archives API against the delivered backup
files. All writable work happens in an external scratch directory that is
removed afterwards; the delivery tree is never opened read-write and any
SQLite sidecar left next to a read-only input is cleaned up. Run with:
    python3 -I verify_recovery.py <root_dir> <package_root>
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[2])

from strataleaf import Database, Archives, SnapshotTooOld
from strataleaf.archive import file_digest


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2),
                    encoding="utf-8")


def main(root):
    root = Path(root).resolve()
    evidence = root / "evidence"
    work = Path(tempfile.mkdtemp(prefix="handoff-verify-"))
    report = {}
    try:
        # 1) Reading a revision below the T3 floor must fail explicitly.
        with Database(root / "materials/t3/backup_t3.db") as db:
            try:
                db.get("segments", "seg_00", revision=1)
                report["t3_read_revision_1"] = "UNEXPECTED SUCCESS"
            except SnapshotTooOld as exc:
                report["t3_read_revision_1"] = {
                    "error": type(exc).__name__, "message": str(exc)}
            report["t3_head"] = db.revision

        # 2) Every time-point backup restores to an unused path, checksum-checked.
        for t in ("t1", "t2", "t3"):
            source = root / f"materials/{t}/backup_{t}.db"
            target = work / f"restored_{t}.db"
            receipt = Archives.restore(source, target,
                                       expected_sha256=file_digest(source))
            with Database(target) as restored:
                report[f"restore_{t}"] = {
                    "mode": receipt["mode"], "head": restored.revision,
                    "fingerprint": restored.diagnostics.fingerprint(),
                    "inspect_deep_ok": restored.diagnostics.inspect(deep=True)["ok"]}

        # 3) Restore refuses to overwrite an existing destination.
        try:
            Archives.restore(root / "materials/t1/backup_t1.db", work / "restored_t1.db")
            report["overwrite_existing_destination"] = "UNEXPECTED SUCCESS"
        except Exception as exc:
            report["overwrite_existing_destination"] = {
                "error": type(exc).__name__, "message": str(exc)}

        # 4) T2 and T3 share the same current read view fingerprint although
        #    they are different backups at different heads/floors; T1 differs.
        fps = {t: report[f"restore_{t}"]["fingerprint"]["sha256"]
               for t in ("t1", "t2", "t3")}
        report["cross_view_fingerprints"] = fps
        report["t2_t3_current_view_equal_despite_different_files"] = fps["t2"] == fps["t3"]

        write_json(evidence / "restore_and_floor_checks.json", report)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        # Read-only opens can still leave empty -wal/-shm files next to inputs.
        for path in root.rglob("*"):
            if path.is_file() and (path.name.endswith(("-wal", "-shm"))
                                   or path.name.startswith(".strataleaf")):
                path.unlink()
    print(json.dumps({"ok": True,
                      "evidence": str(evidence / "restore_and_floor_checks.json")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
