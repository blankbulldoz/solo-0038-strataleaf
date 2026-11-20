import json
import sys
import tempfile
from pathlib import Path
from strataleaf import (Database, Archives, BackupVault, Conflict, InvalidDocument, InvalidCursor,
                        ReadOnly, SnapshotTooOld, ReplicationError, VaultError)

SCHEMA = {"fields": {"region": "string", "sequence": "integer", "code": "string"}, "required": ["region", "sequence"]}


def seed(db):
    db.catalog.create("segments", SCHEMA)
    batch = db.batch("initial_segments")
    for index in range(6):
        batch.put("segments", f"seg_{index:02}", {"region": "east" if index % 2 == 0 else "west", "sequence": index, "code": "c" + str(index)})
    return batch.commit().revision


def rejected(call, error):
    try:
        call()
    except error as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
    raise RuntimeError("operation unexpectedly succeeded")


def _compact_view(entry):
    return {"id": entry["id"], "state": entry["state"], "revision": entry["revision"],
            "file": entry.get("file"), "lease_holder": entry.get("lease_holder")}


def _vault_flow(db, folder, first):
    vault_dir = folder / "vault"
    vault = BackupVault(vault_dir)
    # Two normal captures establish verified backups at different revisions.
    first_backup = vault.capture(db)
    db.batch().put("segments", "seg_00", {"region": "south", "sequence": 71}, expected=first).commit()
    second_backup = vault.capture(db)
    # Two captures of the same revision are both valid; retention must choose one.
    duplicate_one = vault.capture(db)
    duplicate_two = vault.capture(db)
    # An interrupted prepare: a staged file plus a pending registry entry.
    interrupted = vault.prepare(db)
    # A second process dying before the registry entry becomes durable leaves
    # an orphan staged file with no registry record at all.
    other_vault = BackupVault(vault_dir)
    orphan = other_vault.forget_pending(other_vault.prepare(db)["id"])
    orphan_path = vault_dir / orphan["file"]
    # Reconciliation reopens the interrupted pending entry and adopts the orphan.
    reopen = vault.reconcile()
    reopened_ids = {item["id"] for item in reopen["reopened"]}
    if interrupted["id"] not in reopened_ids or orphan_path.name not in [item["file"] for item in reopen["reopened"]]:
        raise RuntimeError("reconcile did not reopen both interrupted artifacts")
    adopted_orphan = next(item for item in vault.list_backups("pending") if item["file"] == orphan_path.name)
    vault.publish(interrupted["id"])
    vault.publish(adopted_orphan["id"])
    # Bit rot on a published backup: verification failure must quarantine it,
    # and it must no longer count toward the recoverable set.
    corrupt_path = vault_dir / duplicate_one["file"]
    with open(corrupt_path, "r+b") as damaged:
        damaged.seek(64)
        damaged.write(b"\xff" * 16)
    check_reopen = vault.reconcile()
    corrupt_entry = next(item for item in vault.list_backups("quarantined") if item["id"] == duplicate_one["id"])
    discarded = vault.discard(corrupt_entry["id"], "checksum failure confirmed by operator")
    # A restore lease protects an in-use backup from every prune plan.
    leased = next(item for item in vault.list_backups("published") if item["id"] == second_backup["id"])
    blocked_prune = None
    with vault.lease(leased["id"], "incident-restore"):
        blocked_prune = rejected(lambda: vault.prune(2), Conflict)
        restored = vault.restore_verified(leased["id"], folder / "restored-from-lease.db", "incident-restore")
    prune = vault.prune(2)
    # Foreign-source files and compacted history must not change retention facts.
    foreign = Database(folder / "other-source.db")
    foreign.catalog.create("segments", SCHEMA)
    foreign.archives.backup(vault_dir / "foreign-unregistered.db")
    foreign.close()
    compact_receipt = db.retention.compact(db.revision)
    final_reconcile = vault.reconcile()
    final_listing = vault.list_backups("published")
    if len(final_listing) != 2:
        raise RuntimeError("vault did not keep the agreed number of verified backups")
    if any(item["source_id"] != first_backup["source_id"] for item in final_listing):
        raise RuntimeError("vault retained a backup from another source")
    for item in final_listing:
        vault.verify(item["id"])
    return {
        "captured": [_compact_view(first_backup), _compact_view(second_backup),
                     _compact_view(duplicate_one), _compact_view(duplicate_two)],
        "interrupted_pending": _compact_view(interrupted),
        "orphan_staged_file": orphan_path.name,
        "reopen_report": {key: [_compact_view(item) if isinstance(item, dict) and "state" in item else item
                                for item in value] for key, value in reopen.items()},
        "checksum_failure": {
            "quarantined": [_compact_view(item) for item in check_reopen["quarantined"]],
            "operator_discard": _compact_view(discarded),
            "file_moved_away": not corrupt_path.exists(),
        },
        "same_revision_pair": {"revision": duplicate_one["revision"],
                               "duplicate_ids": [duplicate_one["id"], duplicate_two["id"]]},
        "lease_protection": {"blocked_prune": blocked_prune,
                             "restored_receipt": {"revision": restored["revision"], "mode": restored["mode"]}},
        "prune": {"deleted_ids": [item["id"] for item in prune["deleted"]],
                  "survivors": [_compact_view(item) for item in prune["survivors"]],
                  "duplicate_revisions_removed": prune["duplicate_revisions_removed"],
                  "age_pruned": prune["age_pruned"]},
        "foreign_file_left_untouched": "foreign-unregistered.db" in final_reconcile["foreign_files"],
        "history_compact_is_separate": compact_receipt,
        "final_verified_count": len(final_listing),
        "registry_file": (vault_dir / "vault-registry.json").name,
    }


def run(flow):
    with tempfile.TemporaryDirectory(prefix="strataleaf-example-") as directory:
        folder = Path(directory)
        with Database(folder / "source.db") as db:
            first = seed(db)
            if flow == "catalog":
                output = {"collections": db.catalog.list(), "duplicate": rejected(lambda: db.catalog.create("segments"), Conflict)}
            elif flow == "schema":
                updated = {**SCHEMA, "fields": {**SCHEMA["fields"], "note": "string"}}
                generation = db.catalog.evolve("segments", updated, 1)
                failure = rejected(lambda: db.catalog.evolve("segments", {**updated, "required": ["note"]}, generation), InvalidDocument)
                output = {"generation": generation, "after_rejection": db.catalog.describe("segments"), "incompatible": failure}
            elif flow == "commit":
                db.catalog.create("marks")
                result = db.batch("dual_write").put("segments", "seg_00", {"region": "south", "sequence": 7}, expected=first).put("marks", "m0", {"segment": "seg_00"}).commit()
                replay = db.batch("dual_write").put("segments", "seg_00", {"region": "south", "sequence": 7}, expected=first).put("marks", "m0", {"segment": "seg_00"}).commit()
                conflict = rejected(lambda: db.batch().put("marks", "discard", {}).put("segments", "seg_01", {"region": "x", "sequence": 1}, expected=0).commit(), Conflict)
                if db.get("marks", "discard") is not None or not replay.replayed:
                    raise RuntimeError("atomicity or replay observation failed")
                output = {"commit": result.__dict__, "replay": replay.__dict__, "conflict": conflict}
            elif flow == "snapshot":
                db.batch().delete("segments", "seg_00", expected=first).commit()
                output = {"past": db.get("segments", "seg_00", first).__dict__, "present": db.get("segments", "seg_00"),
                          "future": rejected(lambda: db.get("segments", "seg_00", db.revision + 1), InvalidDocument)}
            elif flow == "query":
                query = {"field": "sequence", "op": "ge", "value": 0}
                page = db.query("segments", query, limit=2)
                db.batch().delete("segments", "seg_02").put("segments", "seg_07", {"region": "west", "sequence": 7}).commit()
                keys = [row.key for row in page.records]
                cursor = page.cursor
                while cursor:
                    next_page = db.query("segments", query, limit=2, cursor=cursor)
                    keys.extend(row.key for row in next_page.records)
                    cursor = next_page.cursor
                if keys != [f"seg_{i:02}" for i in range(6)]:
                    raise RuntimeError("read revision changed during pagination")
                output = {"revision": page.revision, "keys": keys, "foreign_query": rejected(lambda: db.query("segments", cursor=page.cursor), InvalidCursor)}
            elif flow == "index":
                db.indexes.create("segments", "by_region", "region")
                db.indexes.create("segments", "unique_code", "code", unique=True)
                predicate = {"field": "region", "op": "eq", "value": "east"}
                indexed = db.query("segments", predicate)
                scanned = db.query("segments", predicate, force_scan=True)
                if indexed.records != scanned.records:
                    raise RuntimeError("index and scan differ")
                collision = rejected(lambda: db.batch().put("segments", "new", {"region": "north", "sequence": 8, "code": "c0"}).commit(), Conflict)
                output = {"plan": indexed.plan, "keys": [row.key for row in indexed.records], "collision": collision, "integrity": db.diagnostics.inspect(True)}
            elif flow == "feed":
                db.feed.register("mirror", 0)
                db.batch().delete("segments", "seg_00").commit()
                commits = db.feed.read(0, limit=1)
                db.feed.acknowledge("mirror", commits[-1]["revision"])
                output = {"complete_commit": commits, "ack": db.feed.position("mirror"),
                          "backward": rejected(lambda: db.feed.acknowledge("mirror", 0), Conflict)}
            elif flow == "backup":
                backup = db.archives.backup(folder / "backup.db")
                db.batch().delete("segments", "seg_00").commit()
                bad = rejected(lambda: Archives.restore(folder / "backup.db", folder / "invalid.db", "wrong"), Exception)
                Archives.restore(folder / "backup.db", folder / "restored.db", backup["sha256"])
                with Database(folder / "restored.db") as restored:
                    output = {"backup": backup, "restored": restored.diagnostics.fingerprint(), "bad_checksum": bad}
                    if restored.revision != first:
                        raise RuntimeError("backup read boundary changed")
            elif flow == "replica":
                db.archives.backup(folder / "base.db")
                Archives.restore(folder / "base.db", folder / "replica.db", follower=True)
                db.batch().put("segments", "seg_00", {"region": "north", "sequence": 19}).commit()
                db.batch().delete("segments", "seg_01").commit()
                db.indexes.create("segments", "by_region", "region")
                with Database(folder / "replica.db") as replica:
                    part = db.replication.export(replica.revision, limit=1)
                    applied = replica.replication.apply(part)
                    tail = db.replication.export(replica.revision)
                    replica.replication.apply(tail)
                    repeat = replica.replication.apply(tail)
                    readonly = rejected(lambda: replica.batch().delete("segments", "seg_00").commit(), ReadOnly)
                    if replica.diagnostics.fingerprint() != db.diagnostics.fingerprint() or not replica.diagnostics.inspect(True)["ok"]:
                        raise RuntimeError("replica differs from source")
                    output = {"first_packet": applied, "repeat": repeat, "readonly": readonly, "fingerprint": replica.diagnostics.fingerprint()}
            elif flow == "compact":
                db.retention.pin("reader", first)
                db.feed.register("slow_copy", first)
                db.batch().put("segments", "seg_00", {"region": "north", "sequence": 8}).commit()
                blocked = db.retention.compact(db.revision)
                old = db.get("segments", "seg_00", first)
                db.retention.unpin("reader")
                db.feed.acknowledge("slow_copy", db.revision)
                advanced = db.retention.compact(db.revision)
                expired = rejected(lambda: db.get("segments", "seg_00", first), SnapshotTooOld)
                replay = db.batch("initial_segments")
                for index in range(6):
                    replay.put("segments", f"seg_{index:02}", {"region": "east" if index % 2 == 0 else "west", "sequence": index, "code": "c" + str(index)})
                output = {"blocked": blocked, "old_value": old.document, "advanced": advanced, "expired_read": expired, "replay_after_compaction": replay.commit().__dict__}
            elif flow == "expire":
                written = db.batch().put("segments", "timed", {"region": "north", "sequence": 10}, expires_at=100).commit()
                before = db.get("segments", "timed")
                swept = db.retention.sweep(100)
                if db.get("segments", "timed") is not None or db.get("segments", "timed", written.revision) is None:
                    raise RuntimeError("expiry did not preserve historical visibility")
                output = {"before": before.__dict__, "sweep": swept, "feed": db.feed.read(written.revision), "idle": db.retention.sweep(100)}
            elif flow == "aggregate":
                output = db.aggregates.group("segments", "region", "sequence", revision=first)
                if sum(item["count"] for item in output["groups"]) != 6:
                    raise RuntimeError("aggregation lost visible documents")
                output["invalid"] = rejected(lambda: db.aggregates.group("unknown", "region"), Exception)
            elif flow == "patch":
                result = db.patch("segments", "seg_00", {"code": None, "detail": {"block": 4}}, expected=first)
                output = {"patch": result.__dict__, "document": db.get("segments", "seg_00").document,
                          "stale": rejected(lambda: db.patch("segments", "seg_00", {"sequence": 8}, expected=first), Conflict)}
            elif flow == "inspect":
                db.indexes.create("segments", "by_code", "code")
                output = {"inspection": db.diagnostics.inspect(deep=True), "fingerprint": db.diagnostics.fingerprint(), "checkpoint": db.checkpoint()}
                if not output["inspection"]["ok"]:
                    raise RuntimeError("integrity inspection failed")
            elif flow == "transfer":
                exported = db.transfer.export_lines("segments")
                db.catalog.create("copy_segments", SCHEMA)
                imported = db.transfer.import_lines("copy_segments", exported["lines"])
                output = {"revision": exported["revision"], "imported": imported.__dict__,
                          "duplicate": rejected(lambda: db.transfer.import_lines("copy_segments", exported["lines"]), Conflict)}
                if len(db.query("copy_segments").records) != 6:
                    raise RuntimeError("logical import differs")
            elif flow == "vault":
                output = _vault_flow(db, folder, first)
            else:
                raise ValueError("unknown example workflow: " + flow)
            print(json.dumps({"workflow": flow, "result": output}, ensure_ascii=False, sort_keys=True, default=str))


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "commit")
