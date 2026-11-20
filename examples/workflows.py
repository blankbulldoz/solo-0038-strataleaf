import json
import sys
import tempfile
from pathlib import Path
from strataleaf import (Database, Archives, Conflict, InvalidDocument, InvalidCursor, ReadOnly,
                        SnapshotTooOld, ReplicationError, FormatMigrationRequired, MigrationInterrupted,
                        inspect_archive, inspect_format, migrate)
from strataleaf.catalog import Catalog
from strataleaf.storage import Storage
from strataleaf.transaction import WriteBatch
from strataleaf.codec import digest
from strataleaf.cursors import encode_cursor
from strataleaf.migration import fingerprint

SCHEMA = {"fields": {"region": "string", "sequence": "integer", "code": "string"}, "required": ["region", "sequence"]}


def seed(db):
    db.catalog.create("segments", SCHEMA)
    batch = db.batch("initial_segments")
    for index in range(6):
        batch.put("segments", f"seg_{index:02}", {"region": "east" if index % 2 == 0 else "west", "sequence": index, "code": "c" + str(index)})
    return batch.commit().revision


class _CursorStore:
    """Minimal read-only metadata view for minting a pre-migration cursor."""

    def __init__(self, path):
        import sqlite3
        self.connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)

    def get(self, key):
        return self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()


def seed_legacy(path):
    """Build a format-1 database through the same write path an older build used."""
    store = Storage(path, create_format=1)
    try:
        Catalog(store).create("segments", SCHEMA)
        batch = WriteBatch(store, "initial_segments")
        for index in range(6):
            batch.put("segments", f"seg_{index:02}", {"region": "east" if index % 2 == 0 else "west",
                                                       "sequence": index, "code": "c" + str(index),
                                                       "note": "分区" + str(index), "large": 2 ** 70 + index})
        return batch.commit().revision
    finally:
        store.close()


def index_tokens(path):
    """Materialised index tokens read straight from SQLite, independent of the codec."""
    import sqlite3
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return [row[0] for row in connection.execute(
            "SELECT token FROM index_values WHERE name='by_code' ORDER BY key")]
    finally:
        connection.close()


def replication_round_trip(source):
    """Export from the migrated primary and apply to a fresh replica; check identity."""
    with Database(source) as primary:
        primary.archives.backup(source.parent / "replica_base.db")
        Archives.restore(source.parent / "replica_base.db", source.parent / "replica.db", follower=True)
        envelope = primary.replication.export(primary.revision)
    with Database(source) as primary, Database(source.parent / "replica.db") as replica:
        applied = replica.replication.apply(envelope)
        repeat = replica.replication.apply(envelope)
        return {"package_format": envelope["body"]["format"], "applied": applied["applied"],
                "repeat": repeat["applied"], "source_format": primary.format,
                "replica_format": replica.format,
                "matched": replica.diagnostics.fingerprint() == primary.diagnostics.fingerprint()}


def rejected(call, error):
    try:
        call()
    except error as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
    raise RuntimeError("operation unexpectedly succeeded")


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
            elif flow == "migrate":
                output = migration_flow(folder)
            else:
                raise ValueError("unknown example workflow: " + flow)
            print(json.dumps({"workflow": flow, "result": output}, ensure_ascii=False, sort_keys=True, default=str))


def migration_flow(folder):
    """Identify a format-1 database, migrate it with an interruption, and compare identities."""
    legacy = folder / "legacy.db"
    first = seed_legacy(legacy)
    before = inspect_format(legacy, counts=True)
    if before["format"] != 1 or before["migration"] != "required" or before["representations"]["versions"]["pack_rows"]:
        raise RuntimeError("legacy database was not identified as format 1")
    refused = rejected(lambda: Database(legacy), FormatMigrationRequired)
    # Read-only facts that must not drift across the rewrite.  ``fingerprint``
    # works on either representation without opening a live database handle.
    old_fingerprint = fingerprint(legacy)
    with _CursorStore(legacy) as old:
        old_cursor = encode_cursor(old, {"database": old.get("database_id"),
                                         "query": digest({"collection": "segments",
                                                          "predicate": {"field": "sequence", "op": "ge", "value": 0}}),
                                         "revision": first, "after": "seg_01"})
    dry = migrate(legacy, dry_run=True)
    paused = rejected(lambda: migrate(legacy, chunk_size=2, snapshot=folder / "before.db", interrupt_after=3),
                      MigrationInterrupted)
    interrupted = inspect_format(legacy, counts=True)
    resumed = migrate(legacy, chunk_size=2)
    after = inspect_format(legacy, counts=True)
    with Database(legacy) as db:
        db.indexes.create("segments", "by_code", "code")
        indexed = db.query("segments", {"field": "code", "op": "eq", "value": "c0"})
        scanned = db.query("segments", {"field": "code", "op": "eq", "value": "c0"}, force_scan=True)
        predicate = {"field": "sequence", "op": "ge", "value": 0}
        tail = db.query("segments", predicate, limit=2, cursor=old_cursor)
        tokens = index_tokens(legacy)
        migrated = {"format": db.format, "format_name": db.format_name, "revision": db.revision,
                    "inspection": db.diagnostics.inspect(deep=True), "fingerprint": db.diagnostics.fingerprint(),
                    "indexed_equals_scan": indexed.records == scanned.records,
                    "index_tokens": tokens,
                    "old_cursor_tail": [item.key for item in tail.records]}
        if not migrated["inspection"]["ok"] or db.get("segments", "seg_00").document["large"] != 2 ** 70:
            raise RuntimeError("migrated database is not logically intact")
        if fingerprint(legacy) != old_fingerprint or not migrated["indexed_equals_scan"]:
            raise RuntimeError("logical identity drifted across the payload format change")
        if [item.key for item in db.query("segments", predicate, limit=2).records] != ["seg_00", "seg_01"]:
            raise RuntimeError("first page changed across the payload format change")
    if migrated["old_cursor_tail"] != [f"seg_{index:02}" for index in range(2, 4)]:
        raise RuntimeError("a cursor minted before migration did not continue the same read view")
    if migrated["index_tokens"] != ['v:"c' + str(index) + '"' for index in range(6)]:
        raise RuntimeError("index scalar tokens drifted across the payload format change")
    package = replication_round_trip(folder / "legacy.db")
    if not package["matched"] or package["package_format"] != 2:
        raise RuntimeError("replica package interpretation changed after migration")
    old_backup = inspect_archive(folder / "before.db")
    if old_backup["format"] != 1 or migrated["revision"] != first:
        raise RuntimeError("legacy snapshot is not an intact format-1 archive")
    return {"identified": {k: before[k] for k in ("format", "format_name", "migration", "representations")},
            "refused_before_migration": refused, "dry_run": {k: dry[k] for k in ("state", "pending", "manifest")},
            "paused": paused, "interrupted": {k: interrupted[k] for k in ("format_name", "migration", "representations")},
            "resumed": {k: resumed[k] for k in ("state", "converted", "manifest", "receipts", "indexes", "snapshot")},
            "published": {k: after[k] for k in ("format", "format_name", "migration")},
            "legacy_snapshot": old_backup, "migrated": migrated, "replication": package}


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "commit")
