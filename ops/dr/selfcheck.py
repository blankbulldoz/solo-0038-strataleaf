"""Self-checks for the DR tooling itself (no dependency on the drill run).

Run with::

    python3 -m ops.dr.selfcheck

These checks use throwaway temporary databases and assert the properties the
runbook relies on.  They are deliberately not a replacement for the deferred
library test suite; they only validate the operational wrappers.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from strataleaf import Archives, Conflict, Database, ReadOnly, SnapshotTooOld

from .backup import (compare_bare_copy, produce_backup, reject_bare_copy,
                     restore_and_verify, verify_manifest)
from .facts import read_facts
from .replication import catch_up
from .workload import Workload


def _seed(path: Path) -> None:
    workload = Workload(str(path), prefix="chk")
    workload.bootstrap()
    for _ in range(20):
        workload.step()
    workload.db.indexes.create("orders", "unique_code", "code", unique=True)
    workload.close()


def check_facts_detect_index_drift() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "db.db"
        _seed(path)
        facts = read_facts(path)
        assert facts["ok"], facts["issues"]
        assert facts["index_projection"]["ok"] is True
        assert facts["commit_contiguity"]["ok"] is True
        summary = read_facts(path)["documents"]
        assert summary["count"] > 0


def check_bare_copy_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "db.db"
        _seed(path)
        copy = Path(directory) / "copy.db"
        comparison = compare_bare_copy(path, copy)
        assert comparison["accepted_as_backup"] is False
        rejection = reject_bare_copy(copy)
        assert rejection["accepted"] is False
        assert "manifest" in rejection["reason"]


def check_backup_verify_and_restore() -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "db.db"
        store = Path(directory) / "backups"
        _seed(source)
        manifest = produce_backup(source, store, "chk")
        verified = verify_manifest(store / manifest["archive"]["name"])
        assert verified["ok"], [c for c in verified["checks"] if not c["ok"]]

        restored = restore_and_verify(store / manifest["archive"]["name"],
                                      Path(directory) / "restored.db", mode="primary")
        assert restored["ok"], [c for c in restored["checks"] if not c["ok"]]

        follower = Path(directory) / "follower.db"
        receipt = Archives.restore(store / manifest["archive"]["name"], follower,
                                   manifest["archive"]["sha256"], follower=True)
        assert receipt["mode"] == "replica"
        with Database(follower) as replica:
            try:
                replica.batch().put("orders", "x", {"region": "south", "sequence": 1}).commit()
            except ReadOnly:
                pass
            else:
                raise AssertionError("replica accepted a primary write")


def check_manifest_tamper_is_detected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "db.db"
        store = Path(directory) / "backups"
        _seed(source)
        manifest = produce_backup(source, store, "tamper")
        archive = store / manifest["archive"]["name"]
        with open(archive, "r+b") as stream:
            stream.seek(0, 2)
            stream.write(b"corruption")
        verified = verify_manifest(archive)
        assert not verified["ok"]
        assert any(c["check"] == "archive_sha256" and not c["ok"] for c in verified["checks"])
        try:
            restore_and_verify(archive, Path(directory) / "nope.db", mode="primary")
        except Exception:
            pass
        else:
            raise AssertionError("tampered archive restored")


def check_backup_refuses_overwrite() -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "db.db"
        store = Path(directory) / "backups"
        store.mkdir()
        _seed(source)
        occupied = store / "occupied.db"
        occupied.write_bytes(b"keep")
        with Database(source) as db:
            try:
                db.archives.backup(occupied)
            except Conflict:
                pass
            else:
                raise AssertionError("library overwrote an existing destination")
        assert occupied.read_bytes() == b"keep"


def check_interrupted_and_resumed_catch_up() -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "db.db"
        store = Path(directory) / "backups"
        workload = Workload(str(source), prefix="rep")
        workload.bootstrap()
        for _ in range(10):
            workload.step()
        workload.close()
        manifest = produce_backup(source, store, "rep")
        replica = Path(directory) / "replica.db"
        Archives.restore(store / manifest["archive"]["name"], replica,
                         manifest["archive"]["sha256"], follower=True)

        writer = Workload(str(source), prefix="rep")
        for _ in range(15):
            writer.step()
        writer.close()

        first = catch_up(source, replica, packet_limit=3, stop_after_packets=1)
        assert first["status"] == "interrupted", first
        resumed = catch_up(source, replica, packet_limit=3, corrupt_probe=True)
        assert resumed["status"] == "caught_up", resumed
        assert resumed["corrupt_probe"]["revision_unchanged"] is True


def check_reclaimed_history_requires_rebootstrap() -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "db.db"
        store = Path(directory) / "backups"
        workload = Workload(str(source), prefix="rc")
        workload.bootstrap()
        for _ in range(6):
            workload.step()
        workload.close()
        manifest = produce_backup(source, store, "rc")
        stale = Path(directory) / "stale.db"
        Archives.restore(store / manifest["archive"]["name"], stale,
                         manifest["archive"]["sha256"], follower=True)
        stale_revision = read_facts(stale)["head"]

        with Database(source) as db:
            for index in range(4):
                db.batch().put("orders", "rc-new-%d" % index,
                               {"region": "south", "sequence": index}).commit()
            db.feed.register("tmp_consumer", db.revision)
            db.retention.compact(db.revision)
            db.feed.release("tmp_consumer")
        result = catch_up(source, stale, packet_limit=5)
        assert result["status"] == "requires_rebootstrap", result
        with Database(source) as db:
            try:
                db.replication.export(stale_revision)
            except SnapshotTooOld:
                pass
            else:
                raise AssertionError("source exported below its floor")


CHECKS = [
    check_facts_detect_index_drift,
    check_bare_copy_is_rejected,
    check_backup_verify_and_restore,
    check_manifest_tamper_is_detected,
    check_backup_refuses_overwrite,
    check_interrupted_and_resumed_catch_up,
    check_reclaimed_history_requires_rebootstrap,
]


def main() -> int:
    results = []
    for check in CHECKS:
        try:
            check()
            results.append({"check": check.__name__, "ok": True})
        except Exception as exc:  # noqa: BLE001 - report every failure
            results.append({"check": check.__name__, "ok": False,
                            "error": type(exc).__name__, "message": str(exc)})
    print(json.dumps({"ok": all(item["ok"] for item in results), "results": results},
                     ensure_ascii=False, indent=2))
    return 0 if all(item["ok"] for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
