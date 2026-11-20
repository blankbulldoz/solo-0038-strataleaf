"""Reproduce takeover decisions that look alike but need different handling.

Every scenario uses only the public Database API against independent temporary
files.  Read-only SQLite queries and an external snapshot holder are used to
observe physical size and checkpoint pressure; none of this changes the
production semantics under test.  Each case returns the caller-visible result
of every probe, including the exact error type and message.
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from strataleaf import Archives, Database

SCHEMA = {"fields": {"region": "string", "sequence": "integer"},
          "required": ["region", "sequence"]}
DOCUMENTS = [{"region": "east", "sequence": 0}, {"region": "west", "sequence": 1},
             {"region": "east", "sequence": 2}, {"region": "west", "sequence": 3}]


def outcome(call):
    """Record a returned value or the exact caller-visible failure."""
    try:
        value = call()
    except Exception as exc:  # the exception type is itself evidence
        return {"error": type(exc).__name__, "message": str(exc)}
    return value.__dict__ if hasattr(value, "__dict__") else value


def sizes(folder, stem):
    return {suffix: (folder / (stem + suffix)).stat().st_size
            if (folder / (stem + suffix)).exists() else None
            for suffix in (".db", ".db-wal", ".db-shm")}


def pages(path):
    """Read-only physical layout; never used to decide logical retention."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        free = connection.execute("PRAGMA freelist_count").fetchone()[0]
        return {"page_size": page_size, "page_count": page_count,
                "allocated_bytes": page_size * page_count, "freelist_pages": free}
    finally:
        connection.close()


def protections(path):
    """Observation only: the API deliberately has no "list all holds" call.

    A maintainer needs the names, not just counts, before releasing anything.
    This opens the live file read-only and never writes.
    """
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        meta = dict(connection.execute("SELECT key,value FROM meta"))
        return {"pins": [list(row) for row in connection.execute("SELECT name,rev FROM pins ORDER BY name")],
                "consumers": [list(row) for row in connection.execute("SELECT name,ack FROM consumers ORDER BY name")],
                "catalog_epoch": int(meta["catalog_epoch"]),
                "metadata_head": int(meta["head"]), "metadata_floor": int(meta["floor"])}
    finally:
        connection.close()


def state(db, folder=None, stem=None):
    inspection = db.diagnostics.inspect()
    view = {"head": inspection["head"], "floor": inspection["floor"],
            "readable_range": [inspection["floor"], inspection["head"]],
            "counts": inspection["counts"], "issues": inspection["issues"],
            "mode": inspection["mode"], "database_id": inspection["database_id"]}
    if folder is not None:
        view["files"] = sizes(folder, stem)
        view["pages"] = pages(folder / (stem + ".db"))
        view["protections"] = protections(folder / (stem + ".db"))
    return view


def seed(db, request_key="takeover_seed"):
    db.catalog.create("segments", SCHEMA)
    batch = db.batch(request_key)
    for index, document in enumerate(DOCUMENTS):
        batch.put("segments", f"seg_{index:02}", document)
    return batch.commit().revision


def replay_seed(db, request_key="takeover_seed"):
    batch = db.batch(request_key)
    for index, document in enumerate(DOCUMENTS):
        batch.put("segments", f"seg_{index:02}", document)
    return batch.commit()


def write_versions(db, count, key="seg_00"):
    for value in range(count):
        db.batch().put("segments", key, {"region": "north", "sequence": value}).commit()


def wal(folder):
    """WAL growth while nobody triggered compaction, and checkpoint pressure."""
    stem = "wal"
    db = Database(folder / (stem + ".db"))
    seed(db)
    holder = sqlite3.connect(folder / (stem + ".db"), isolation_level=None)
    holder.execute("BEGIN")
    snapshot = holder.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    write_versions(db, 60)
    held = {"checkpoint": db.checkpoint(), "state": state(db, folder, stem)}
    holder.rollback()
    holder.close()
    released = {"checkpoint": db.checkpoint(), "state": state(db, folder, stem)}
    db.close()
    return {"holder_snapshot_versions": snapshot,
            "while_external_reader": held, "after_reader_closed": released,
            "after_database_closed": {"files": sizes(folder, stem),
                                      "pages": pages(folder / (stem + ".db"))}}


def reclaim(folder):
    """Logical history removal versus physical file size."""
    stem = "reclaim"
    db = Database(folder / (stem + ".db"))
    seed(db)
    write_versions(db, 60)
    before = state(db, folder, stem)
    compacted = db.retention.compact(db.revision)
    after_compact = state(db, folder, stem)
    checkpoint = db.checkpoint()
    after_checkpoint = state(db, folder, stem)
    for index in range(20):
        db.batch().put("segments", f"new_{index:02}", {"region": "south", "sequence": index}).commit()
    after_reuse = state(db, folder, stem)
    additions = outcome(lambda: db.get("segments", "seg_04"))
    db.close()
    return {"before": before, "compact": compacted, "after_compact": after_compact,
            "checkpoint": checkpoint, "after_checkpoint": after_checkpoint,
            "after_reuse": after_reuse, "never_existing_key": additions,
            "after_close": sizes(folder, stem)}


def watermarks(folder):
    """Failures and empty results whose cause is a different watermark fact."""
    stem = "watermarks"
    db = Database(folder / (stem + ".db"))
    first = seed(db)
    db.batch().delete("segments", "seg_03").commit()
    db.batch().put("segments", "seg_00", {"region": "north", "sequence": 10}).commit()
    db.retention.pin("audit", first)
    db.batch().put("segments", "seg_01", {"region": "south", "sequence": 11}).commit()
    cursor_page = db.query("segments", None, limit=1)
    second_page = db.query("segments", None, limit=1, cursor=cursor_page.cursor)
    db.batch().put("segments", "seg_02", {"region": "east", "sequence": 12}).put(
        "segments", "seg_late", {"region": "north", "sequence": 13}).commit()
    while_readable = {"head": db.revision, "floor": state(db)["floor"],
                      "tombstone_now": outcome(lambda: db.get("segments", "seg_03")),
                      "tombstone_at_old_revision": outcome(
                          lambda: db.get("segments", "seg_03", first).__dict__),
                      "never_existed_now": outcome(lambda: db.get("segments", "never")),
                      "never_existed_at_old_revision": outcome(
                          lambda: db.get("segments", "never", first)),
                      "key_created_after_old_revision": outcome(
                          lambda: db.get("segments", "seg_late", first)),
                      "key_created_after_old_revision_now": outcome(
                          lambda: db.get("segments", "seg_late").__dict__),
                      "unknown_collection": outcome(lambda: db.get("absent", "seg_00")),
                      "read_beyond_head": outcome(lambda: db.get("segments", "seg_00", db.revision + 1)),
                      "read_at_head": outcome(lambda: db.get("segments", "seg_00").__dict__),
                      "feed_at_head": db.feed.read(db.revision),
                      "feed_from_first": [commit["revision"] for commit in db.feed.read(first)]}
    blocked = db.retention.compact(db.revision)
    after_blocked = {"feed_from_zero": outcome(lambda: db.feed.read(0)),
                     "read_at_zero": outcome(lambda: db.get("segments", "seg_00", 0)),
                     "read_at_protected_revision": outcome(
                         lambda: db.get("segments", "seg_00", first).__dict__),
                     "feed_from_protected_revision": [
                         commit["revision"] for commit in db.feed.read(first)],
                     "protections": protections(folder / (stem + ".db"))}
    db.retention.unpin("audit")
    advanced = db.retention.compact(db.revision)
    repeat = db.retention.compact(db.revision)
    floor = advanced["floor"]
    after_floor = {"head": db.revision, "floor": floor,
                   "tombstone_at_old_revision": outcome(lambda: db.get("segments", "seg_03", first)),
                   "never_existed_at_old_revision": outcome(lambda: db.get("segments", "never", first)),
                   "tombstone_at_floor": outcome(lambda: db.get("segments", "seg_03", floor)),
                   "never_existed_at_floor": outcome(lambda: db.get("segments", "never", floor)),
                   "read_at_floor": outcome(lambda: db.get("segments", "seg_00", floor).__dict__),
                   "read_at_floor_reports_version": int(db.get("segments", "seg_00", floor).revision),
                   "read_below_floor": outcome(lambda: db.get("segments", "seg_00", floor - 1)),
                   "feed_from_first": outcome(lambda: db.feed.read(first)),
                   "feed_at_floor": db.feed.read(floor),
                   "cursor_page_before_floor": outcome(
                       lambda: db.query("segments", None, limit=1, cursor=cursor_page.cursor)),
                   "cursor_second_page_before_floor": outcome(
                       lambda: db.query("segments", None, limit=1, cursor=second_page.cursor)),
                   "receipt_replay_after_floor": replay_seed(db),
                   "final": state(db, folder, stem)}
    return {"cursor_revisions": [cursor_page.revision, second_page.revision],
            "blocked_compact": blocked, "after_blocked_compact": after_blocked,
            "advanced_compact": advanced, "repeat_compact": repeat,
            "while_history_readable": while_readable, "after_floor_advanced": after_floor}


def consumer(folder):
    """Advancing an acknowledgement, releasing a consumer, and abandoning it."""
    stem = "consumer"
    db = Database(folder / (stem + ".db"))
    first = seed(db)
    db.retention.pin("audit", first)
    db.feed.register("slow", first)
    write_versions(db, 10)
    hold_both = db.retention.compact(db.revision)
    db.retention.unpin("audit")
    hold_consumer = db.retention.compact(db.revision)
    middle = first + 5
    db.feed.acknowledge("slow", middle)
    backward = outcome(lambda: db.feed.acknowledge("slow", first))
    partway = {"compact": db.retention.compact(db.revision),
               "position": db.feed.position("slow"),
               "backward_while_history_retained": backward,
               "read_from_old_position": outcome(lambda: db.feed.read(first)),
               "read_from_position": db.feed.read(middle)}
    db.feed.acknowledge("slow", db.revision)
    at_head = {"compact": db.retention.compact(db.revision),
               "position": db.feed.position("slow"),
               "read_from_previous_position": outcome(lambda: db.feed.read(middle)),
               "register_second_consumer_at_old_position": outcome(
                   lambda: db.feed.register("late", first))}
    db.feed.release("slow")
    after_release = {
        "inspection": db.diagnostics.inspect(),
        "register_at_old_position": outcome(lambda: db.feed.register("slow", first)),
        "register_at_floor": outcome(lambda: db.feed.register("slow", db.diagnostics.inspect()["floor"])),
        "receipt_replay": replay_seed(db),
        "backward_acknowledge_unknown": outcome(lambda: db.feed.acknowledge("slow", first))}
    db.close()

    stem = "abandoned"
    other = Database(folder / (stem + ".db"))
    second = seed(other, "abandoned_seed")
    other.feed.register("gone", second)
    write_versions(other, 8)
    before = {"position": other.feed.position("gone"),
              "compact": other.retention.compact(other.revision)}
    other.feed.release("gone")
    after = {"compact": other.retention.compact(other.revision),
             "read_from_old_position": outcome(lambda: other.feed.read(second)),
             "reregister_old": outcome(lambda: other.feed.register("gone", second)),
             "state": state(other)}
    other.close()
    return {"hold_by_pin_and_consumer": hold_both, "hold_by_consumer_only": hold_consumer,
            "acknowledged_partway": partway, "acknowledged_head": at_head,
            "released_after_ack": after_release,
            "released_without_ack": {"before": before, "after": after}}


def restore(folder):
    """What a backup carries, and what restore deliberately drops or rekeys."""
    stem = "source"
    db = Database(folder / (stem + ".db"))
    first = seed(db)
    db.retention.pin("audit", first)
    db.feed.register("slow", first)
    write_versions(db, 5)
    cursor = db.query("segments", None, limit=2).cursor
    db.retention.compact(db.revision)
    held = db.archives.backup(folder / "held.db")
    db.retention.unpin("audit")
    db.feed.acknowledge("slow", db.revision)
    db.retention.compact(db.revision)
    compacted = db.archives.backup(folder / "compacted.db")
    original = state(db)
    Archives.restore(folder / "held.db", folder / "restored-held.db", held["sha256"])
    Archives.restore(folder / "compacted.db", folder / "restored-late.db", compacted["sha256"])
    Archives.restore(folder / "compacted.db", folder / "follower.db", compacted["sha256"], follower=True)
    restored = {}
    for label, name in (("held", "restored-held.db"), ("compacted", "restored-late.db"),
                        ("follower", "follower.db")):
        with Database(folder / name) as copy:
            view = state(copy)
            view["read_seed_revision"] = outcome(lambda: copy.get("segments", "seg_00", first).__dict__)
            view["feed_from_seed_revision"] = outcome(lambda: copy.feed.read(first))
            view["old_cursor_from_source"] = outcome(
                lambda: copy.query("segments", None, limit=2, cursor=cursor).records)
            view["receipt_replay"] = outcome(lambda: replay_seed(copy))
            view["register_at_restored_floor"] = outcome(
                lambda: copy.feed.register("probe_floor", copy.diagnostics.inspect()["floor"]))
            view["register_at_seed_revision"] = outcome(
                lambda: copy.feed.register("probe_seed", first))
            view["pin_at_restored_floor"] = outcome(
                lambda: copy.retention.pin("probe_pin", copy.diagnostics.inspect()["floor"]))
            restored[label] = view
    with Database(folder / "follower.db") as follower:
        restored["follower"]["write_attempt"] = outcome(
            lambda: follower.batch().delete("segments", "seg_00").commit())
        restored["follower"]["local_pin_and_consumer"] = {
            "pin": outcome(lambda: follower.retention.pin("local", follower.revision)),
            "consumer": outcome(lambda: follower.feed.register("local", follower.revision)),
            "compact_attempt": outcome(lambda: follower.retention.compact(follower.revision)),
            "sweep_without_candidates": outcome(lambda: follower.retention.sweep(10 ** 9))}
    db.close()

    Archives.restore(folder / "held.db", folder / "old-follower.db", held["sha256"], follower=True)
    with Database(folder / "restored-late.db") as new_primary, \
            Database(folder / "old-follower.db") as old_follower:
        new_primary.batch().put("segments", "seg_01", {"region": "north", "sequence": 50}).commit()
        source_packet = new_primary.replication.export(old_follower.revision)
        restored["new_primary_vs_old_follower"] = {
            "old_follower_head": old_follower.revision,
            "new_primary_id": new_primary.diagnostics.inspect()["database_id"],
            "packet_source": source_packet["body"]["source"],
            "old_follower_upstream": "the pre-restore database_id",
            "apply_new_primary_packet": outcome(
                lambda: old_follower.replication.apply(source_packet)),
            "old_follower_still_reads_its_own_head": outcome(
                lambda: old_follower.get("segments", "seg_00").__dict__)}
    return {"backup_while_protected": {key: held[key] for key in ("revision", "floor", "database_id", "sha256")},
            "backup_after_release": {key: compacted[key] for key in ("revision", "floor", "database_id", "sha256")},
            "backup_file_bytes": {name: (folder / name).stat().st_size
                                  for name in ("held.db", "compacted.db")},
            "original": original, "restored": restored}


def receipts(folder):
    """A settled request is remembered in the live file, not in an older archive."""
    history = Database(folder / "history.db")
    history.catalog.create("segments", SCHEMA)
    history.batch("bootstrap").put("segments", "seg_00", DOCUMENTS[0]).commit()
    archive = history.archives.backup(folder / "before-charge.db")
    live = {"archive_head": archive["revision"]}
    live["first_retire"] = outcome(
        lambda: history.batch("retire_charge").delete("segments", "seg_00").commit())
    live["immediate_retry"] = outcome(
        lambda: history.batch("retire_charge").delete("segments", "seg_00").commit())
    live["later_unrelated_write"] = outcome(
        lambda: history.batch().put("segments", "seg_01", {"region": "south", "sequence": 8}).commit())
    live["same_key_different_operations"] = outcome(
        lambda: history.batch("retire_charge").put("segments", "seg_00", DOCUMENTS[1]).commit())
    live["compaction"] = outcome(lambda: history.retention.compact(history.revision))
    live["retry_after_compaction"] = outcome(
        lambda: history.batch("retire_charge").delete("segments", "seg_00").commit())
    live["state_after_compaction"] = state(history)
    Archives.restore(folder / "before-charge.db", folder / "restored-before-charge.db",
                     archive["sha256"])
    with Database(folder / "restored-before-charge.db") as copy:
        restored = {"head": copy.revision,
                    "document_before_retry": copy.get("segments", "seg_00").document,
                    "retry_after_restore": outcome(lambda: copy.batch("retire_charge")
                                                   .delete("segments", "seg_00").commit()),
                    "document_after_retry": copy.get("segments", "seg_00"),
                    "feed_revisions": [commit["revision"] for commit in copy.feed.read(0)],
                    "state": state(copy)}
    history.close()
    return {"archive": {key: archive[key] for key in ("revision", "floor")},
            "live": live, "restored_older_archive": restored}


def layout(folder):
    """Which files hold the live database, and what a raw copy captures."""
    stem = "live"
    db = Database(folder / (stem + ".db"))
    seed(db)
    write_versions(db, 20)
    uncheckpointed = {"files": sizes(folder, stem), "source": state(db)}
    main_only = folder / "main-file-only.db"
    main_only.write_bytes((folder / (stem + ".db")).read_bytes())
    with Database(main_only) as copy:
        from_main = {"files": sizes(folder, "main-file-only"), "quiet_restore": state(copy),
                     "fingerprint_matches_source":
                         copy.diagnostics.fingerprint() == db.diagnostics.fingerprint()}
    checkpoint = db.checkpoint()
    after_checkpoint = {"checkpoint": checkpoint, "files": sizes(folder, stem)}
    main_checked = folder / "main-after-checkpoint.db"
    main_checked.write_bytes((folder / (stem + ".db")).read_bytes())
    with Database(main_checked) as copy:
        after_copy = {"files": sizes(folder, "main-after-checkpoint"), "state": state(copy),
                      "fingerprint_matches_source":
                          copy.diagnostics.fingerprint() == db.diagnostics.fingerprint()}
    official = db.archives.backup(folder / "official.db")
    connection = sqlite3.connect((folder / "official.db").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = sorted(row[0] for row in
                        connection.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        connection.close()
    db.close()
    return {"uncheckpointed": uncheckpointed, "copy_of_main_file_only": from_main,
            "after_checkpoint": after_checkpoint, "copy_after_checkpoint": after_copy,
            "official_backup": {key: official[key] for key in ("revision", "floor", "path")},
            "official_backup_bytes": (folder / "official.db").stat().st_size,
            "official_backup_tables": tables,
            "closed_files": sizes(folder, stem)}


def entangled(folder):
    """The four reported symptoms in one timeline, to separate cause from noise."""
    stem = "entangled"
    db = Database(folder / (stem + ".db"))
    first = seed(db)
    db.feed.register("downstream", first)
    db.retention.pin("reporting", first)
    write_versions(db, 40)
    observed = {"growth": state(db, folder, stem),
                "consumer_position": db.feed.position("downstream")}
    blocked = db.retention.compact(db.revision)
    db.retention.unpin("reporting")
    after_release = {"compact": db.retention.compact(db.revision),
                     "old_read": outcome(lambda: db.get("segments", "seg_00", first)),
                     "consumer_position": db.feed.position("downstream")}
    db.feed.acknowledge("downstream", db.revision)
    after_ack = {"compact": db.retention.compact(db.revision),
                 "old_read": outcome(lambda: db.get("segments", "seg_00", first)),
                 "feed_from_first": outcome(lambda: db.feed.read(first)),
                 "state": state(db, folder, stem)}
    replacement = db.archives.backup(folder / "replacement.db")
    Archives.restore(folder / "replacement.db", folder / "replacement-copy.db", replacement["sha256"])
    with Database(folder / "replacement-copy.db") as copy:
        after_restore = state(copy)
        after_restore["read_before_restore_revision"] = outcome(
            lambda: copy.get("segments", "seg_00", first))
        after_restore["replay_old_request"] = outcome(lambda: replay_seed(copy))
    db.close()
    return {"first_symptom": observed, "blocked": blocked, "after_releasing_pin": after_release,
            "after_consumer_ack": after_ack,
            "replacement": {key: replacement[key] for key in ("revision", "floor", "database_id")},
            "after_restore": after_restore}


def replica(folder):
    """Catch-up failures that need retransmit, re-bootstrap, or a new follower."""
    source = Database(folder / "source.db")
    seed(source)
    source.indexes.create("segments", "by_region", "region")
    source.batch().put("segments", "timed", {"region": "east", "sequence": 40},
                       expires_at=50).commit()
    base = source.archives.backup(folder / "base.db")
    for name in ("ahead", "behind", "stale"):
        Archives.restore(folder / "base.db", folder / (name + ".db"), follower=True)
    write_versions(source, 4)
    ahead = Database(folder / "ahead.db")
    ahead.retention.pin("local_reader", ahead.revision)
    ahead.feed.register("local_consumer", ahead.revision)
    full = source.replication.export(ahead.revision)
    applied = ahead.replication.apply(full)
    repeated = ahead.replication.apply(full)
    local_protections = protections(folder / "ahead.db")
    replica_sweep = {"no_candidate": outcome(lambda: ahead.retention.sweep(1)),
                     "expired_candidate": outcome(lambda: ahead.retention.sweep(100)),
                     "head_after": ahead.revision,
                     "protections_after": protections(folder / "ahead.db")}
    behind = Database(folder / "behind.db")
    gap = outcome(lambda: behind.replication.apply(source.replication.export(3)))
    partial = behind.replication.apply(source.replication.export(behind.revision, limit=2))
    during_catchup = {"head": behind.revision,
                      "state": state(behind, folder, "behind"),
                      "indexes": behind.indexes.list("segments"),
                      "plan": behind.query("segments", {"field": "region", "op": "eq", "value": "east"}).plan,
                      "deep_inspection": behind.diagnostics.inspect(True)}
    finished = behind.replication.apply(source.replication.export(behind.revision))
    after_catchup = {"head": behind.revision,
                     "state": state(behind, folder, "behind"),
                     "indexes": behind.indexes.list("segments"),
                     "plan": behind.query("segments", {"field": "region", "op": "eq", "value": "east"}).plan,
                     "deep_inspection": behind.diagnostics.inspect(True)}
    checksum = json.loads(json.dumps(full))
    checksum["sha256"] = "0" * 64
    other = Database(folder / "other.db")
    seed(other, "other_seed")
    wrong_upstream = other.replication.export(0)
    rejected = {"checksum": outcome(lambda: ahead.replication.apply(checksum)),
                "wrong_upstream": outcome(lambda: ahead.replication.apply(wrong_upstream))}
    stale = Database(folder / "stale.db")
    stale_view = {"head": stale.revision, "floor": state(stale)["floor"],
                  "indexes": stale.indexes.list("segments"),
                  "inspection_issues": stale.diagnostics.inspect()["issues"]}
    source.retention.compact(source.revision)
    source_floor = source.diagnostics.inspect()["floor"]
    at_floor = source.replication.export(source_floor)
    stale_view.update({
        "read_from_stale_head": outcome(lambda: stale.get("segments", "seg_00").__dict__),
        "export_from_stale_head": outcome(lambda: source.replication.export(stale.revision)),
        "export_at_source_floor": {"since": at_floor["body"]["since"],
                                   "through": at_floor["body"]["through"],
                                   "commits": len(at_floor["body"]["commits"])},
        "apply_export_at_source_floor": outcome(lambda: stale.replication.apply(at_floor)),
        "still_reads_old_head": outcome(lambda: stale.get("segments", "seg_00").__dict__)})
    source.batch().put("segments", "seg_00", {"region": "north", "sequence": 90}).commit()
    fresh_backup = source.archives.backup(folder / "fresh.db")
    Archives.restore(folder / "fresh.db", folder / "fresh.follower.db", follower=True)
    fresh_view = {}
    with Database(folder / "fresh.follower.db") as fresh:
        fresh_view["before"] = state(fresh)
        fresh_view["export_at_replica_head"] = outcome(
            lambda: fresh.replication.apply(source.replication.export(fresh.revision)))
        fresh_view["retry_at_same_head"] = outcome(
            lambda: fresh.replication.apply(source.replication.export(fresh.revision)))
        fresh_view["after"] = state(fresh)
        source.catalog.evolve("segments",
                              {**SCHEMA, "fields": {**SCHEMA["fields"], "note": "string"}}, 1)
        heartbeat = source.replication.export(fresh.revision)
        fresh_view["heartbeat"] = {
            "since": heartbeat["body"]["since"], "through": heartbeat["body"]["through"],
            "head": heartbeat["body"]["head"],
            "catalog_epoch": heartbeat["body"]["catalog_epoch"],
            "result": outcome(lambda: fresh.replication.apply(heartbeat)),
            "catalog_after": fresh.catalog.describe("segments"),
            "repeat": outcome(lambda: fresh.replication.apply(heartbeat))}
        source.batch().put("segments", "seg_00", {"region": "north", "sequence": 91}).commit()
        fresh_view["catch_up_after_heartbeat"] = outcome(
            lambda: fresh.replication.apply(source.replication.export(fresh.revision)))
        fresh_view["final"] = state(fresh)
    for handle in (source, ahead, behind, stale, other):
        handle.close()
    return {"base": {key: base[key] for key in ("revision", "floor", "database_id")},
            "full_packet": {"revision": full["body"]["through"], "commits": len(full["body"]["commits"])},
            "applied": applied, "repeated": repeated,
            "local_protections_after_apply": local_protections,
            "sweep_on_replica": replica_sweep,
            "gap": gap, "partial": partial, "during_catchup": during_catchup,
            "finished": finished, "after_catchup": after_catchup,
            "rejected": rejected, "stale_follower": stale_view,
            "source_floor_after_compaction": source_floor,
            "fresh_backup": {key: fresh_backup[key] for key in ("revision", "floor", "database_id")},
            "fresh_follower": fresh_view}


CASES = {"wal": wal, "reclaim": reclaim, "watermarks": watermarks,
         "consumer": consumer, "restore": restore, "receipts": receipts,
         "layout": layout, "entangled": entangled, "replica": replica}

if __name__ == "__main__":
    selected = sys.argv[1] if len(sys.argv) > 1 else "watermarks"
    names = list(CASES) if selected == "all" else [selected]
    with tempfile.TemporaryDirectory(prefix="strataleaf-takeover-") as directory:
        root = Path(directory)
        for name in names:
            folder = root / name
            folder.mkdir()
            print(json.dumps({"case": name, "result": CASES[name](folder)},
                             ensure_ascii=False, sort_keys=True, default=str))
