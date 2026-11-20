import json
import sys
import tempfile
from pathlib import Path
from strataleaf import (Database, Archives, AggregateError, Conflict, InvalidDocument,
                        InvalidCursor, NotFound, ReadOnly, SnapshotTooOld, ReplicationError)

SCHEMA = {"fields": {"region": "string", "sequence": "integer", "code": "string"}, "required": ["region", "sequence"]}
AGGREGATE_SCHEMA = {"fields": {"bucket": "string", "active": "boolean"}}


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


def aggregate_workflow(db, first):
    """Exercise every numeric mode on one historical revision.

    The ``ledger`` collection deliberately contains large integers, binary64
    fractions, a boolean measure, a string measure, a missing measure, a JSON
    null, and a group with no usable measure.  All historical facts come from
    one pinned revision, including after later writes and retention movement.
    """
    db.catalog.create("ledger", AGGREGATE_SCHEMA)
    rows = [
        ("c1", {"bucket": "cancel", "active": True, "amount": 10**30}),
        ("c2", {"bucket": "cancel", "active": True, "amount": -(10**30)}),
        ("c3", {"bucket": "cancel", "active": True, "amount": 0.5}),
        ("b1", {"bucket": "big", "active": True, "amount": 10**30}),
        ("b2", {"bucket": "big", "active": True, "amount": 1}),
        ("d1", {"bucket": "rounding", "active": True, "amount": 1e16}),
        ("d2", {"bucket": "rounding", "active": True, "amount": 1.0}),
        ("d3", {"bucket": "rounding", "active": True, "amount": -1e16}),
        ("m1", {"bucket": "mixed", "active": True, "amount": True}),
        ("m2", {"bucket": "mixed", "active": True, "amount": "n/a"}),
        ("m3", {"bucket": "mixed", "active": True}),
        ("m4", {"bucket": "mixed", "active": True, "amount": None}),
        ("m5", {"bucket": "mixed", "active": True, "amount": 2}),
        ("o1", {"bucket": "overflow", "active": True, "amount": 1.5e308}),
        ("o2", {"bucket": "overflow", "active": True, "amount": 1.5e308}),
        ("e1", {"bucket": "empty", "active": True}),
        ("u1", {"bucket": "later", "active": True, "amount": 7}),
    ]
    batch = db.batch("ledger_seed")
    for key, document in rows:
        batch.put("ledger", key, document)
    batch.commit()
    historical = db.revision

    legacy = db.aggregates.group("segments", "region", "sequence", revision=first)
    if sum(item["count"] for item in legacy["groups"]) != 6:
        raise RuntimeError("aggregation lost visible documents")
    if legacy["numeric"]["mode"] != "native" or any("operations" not in item for item in legacy["groups"]):
        raise RuntimeError("simple calling convention changed")

    native = db.aggregates.group("ledger", "bucket", "amount", revision=historical)
    float_mode = db.aggregates.group("ledger", "bucket", "amount", revision=historical, numeric="float")
    exact = db.aggregates.group("ledger", "bucket", "amount", revision=historical, numeric="exact")
    counts_only = db.aggregates.group("ledger", "bucket", revision=historical)
    nothing = db.aggregates.group("ledger", "bucket", "amount", revision=historical,
                                  predicate={"field": "bucket", "op": "eq", "value": "absent"})
    filtered = db.aggregates.group("ledger", "bucket", "amount", revision=historical, numeric="float",
                                   predicate={"field": "bucket", "op": "in", "value": ["cancel", "big", "rounding"]})
    raising = rejected(lambda: db.aggregates.group("ledger", "bucket", "amount", revision=historical, on_error="raise"), AggregateError)
    unknown_mode = rejected(lambda: db.aggregates.group("ledger", "bucket", "amount", numeric="decimal"), InvalidDocument)
    unknown_collection = rejected(lambda: db.aggregates.group("unknown", "region"), NotFound)

    db.retention.pin("ledger_reader", historical)
    db.batch().put("ledger", "c1", {"bucket": "cancel", "active": True, "amount": 10**30}).put(
        "ledger", "u1", {"bucket": "later", "active": True, "amount": 8}).commit()
    blocked = db.retention.compact(db.revision)
    after_writes = db.aggregates.group("ledger", "bucket", "amount", revision=historical, numeric="exact")
    if after_writes["summary"] != exact["summary"] or after_writes["groups"] != exact["groups"]:
        raise RuntimeError("historical aggregation changed after later writes or compaction")
    if after_writes["summary"]["document_count"] != len(rows):
        raise RuntimeError("historical aggregation saw rows written after its revision")
    db.retention.unpin("ledger_reader")
    advanced = db.retention.compact(db.revision)
    expired = rejected(lambda: db.aggregates.group("ledger", "bucket", "amount", revision=historical), SnapshotTooOld)

    rational = lambda numerator, denominator: {"numerator": numerator, "denominator": denominator}
    by_bucket = lambda result: {group["key"]: group for group in result["groups"]}
    native_groups, float_groups, exact_groups = by_bucket(native), by_bucket(float_mode), by_bucket(exact)
    # Cancellation: native/float cannot represent the inputs, exact yields 0.5.
    if exact_groups["cancel"]["sum"] != rational(1, 2) or exact_groups["cancel"]["average"] != rational(1, 6):
        raise RuntimeError("exact cancellation observation failed")
    if native_groups["cancel"]["sum"] is not None or float_groups["cancel"]["sum"] is not None:
        raise RuntimeError("large integers were silently rounded in a floating mode")
    if native_groups["cancel"]["operations"]["sum"]["status"] != "error":
        raise RuntimeError("native mode concealed an unrepresentable integer")
    # Integers alone stay exact in native/exact and are refused by float.
    if native_groups["big"]["sum"] != 10**30 + 1 or exact_groups["big"]["sum"] != 10**30 + 1:
        raise RuntimeError("integer accumulation lost precision")
    if float_groups["big"]["sum"] is not None:
        raise RuntimeError("float mode accepted an unrepresentable integer")
    # Binary64 rounding: 1e16 + 1.0 - 1e16 is 0.0 as binary64 but exactly 1.
    if native_groups["rounding"]["sum"] != 0.0 or float_groups["rounding"]["sum"] != 0.0:
        raise RuntimeError("binary64 rounding observation failed")
    if exact_groups["rounding"]["sum"] != 1 or exact_groups["rounding"]["average"] != rational(1, 3):
        raise RuntimeError("exact accumulation did not preserve the lost unit")
    # Inapplicable and missing measures are attributed, not averaged in.
    if native_groups["mixed"]["sum"] is not None or native_groups["mixed"]["numeric_count"] != 1:
        raise RuntimeError("inapplicable measure fabricated a numeric result")
    if native_groups["mixed"]["invalid_measure_count"] != 2 or native_groups["mixed"]["missing_measure_count"] != 1:
        raise RuntimeError("measure exclusions were not counted")
    if native_groups["mixed"]["null_measure_count"] != 1:
        raise RuntimeError("JSON null was confused with a missing measure")
    if (native_groups["overflow"]["sum"] is not None
            or native_groups["overflow"]["operations"]["sum"]["reason"] != "sum_not_finite"
            or float_groups["overflow"]["sum"] is not None
            or float_groups["overflow"]["operations"]["sum"]["reason"] != "sum_not_finite"):
        raise RuntimeError("non-finite sum was returned as a value")
    if float_groups["overflow"]["min"] != 1.5e308 or float_groups["overflow"]["max"] != 1.5e308:
        raise RuntimeError("extrema should remain available when only the sum overflows")
    if [error["scope"] for error in native_groups["overflow"]["errors"]] != ["aggregate"]:
        raise RuntimeError("aggregate error was not attributed to its group")
    if native_groups["empty"]["sum"] is not None or native_groups["empty"]["operations"]["sum"]["status"] != "empty":
        raise RuntimeError("empty group fabricated a numeric result")
    if nothing["summary"]["group_count"] != 0 or not nothing["summary"]["complete"]:
        raise RuntimeError("empty selection fabricated a group")
    filtered_groups = by_bucket(filtered)
    if sorted(filtered_groups) != ["big", "cancel", "rounding"]:
        raise RuntimeError("predicate did not restrict grouped input")
    if filtered["summary"]["document_count"] != 8 or filtered_groups["rounding"]["sum"] != 0.0:
        raise RuntimeError("filtered aggregation disagrees with unfiltered facts")
    if counts_only["summary"]["group_count"] != len(by_bucket(counts_only)) or counts_only["summary"]["document_count"] != len(rows):
        raise RuntimeError("count-only aggregation lost group facts")
    if any(group["errors"] for group in counts_only["groups"]):
        raise RuntimeError("count-only aggregation attributed measure errors")
    if raising["error"] != "AggregateError" or not raising["message"]:
        raise RuntimeError("on_error='raise' did not surface attributed errors")

    return {
        "legacy_call": {"revision": legacy["revision"], "groups": legacy["groups"], "summary": legacy["summary"]},
        "semantics": {mode: db.aggregates.semantics(mode) for mode in ("native", "float", "exact")},
        "historical_revision": historical,
        "native": {"summary": native["summary"], "groups": native["groups"], "errors": native["errors"]},
        "float": {"summary": float_mode["summary"], "groups": float_mode["groups"], "errors": float_mode["errors"]},
        "exact": {"summary": exact["summary"], "groups": exact["groups"], "errors": exact["errors"]},
        "count_only": {"summary": counts_only["summary"], "groups": counts_only["groups"]},
        "empty_selection": {"summary": nothing["summary"], "groups": nothing["groups"]},
        "filtered_float": {"summary": filtered["summary"], "groups": filtered["groups"], "errors": filtered["errors"]},
        "stable_after_writes": by_bucket(after_writes) == exact_groups,
        "retention": {"blocked": blocked, "advanced": advanced},
        "raised": raising,
        "rejected_inputs": {"unknown_mode": unknown_mode, "unknown_collection": unknown_collection, "expired_revision": expired},
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
                output = aggregate_workflow(db, first)
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
            else:
                raise ValueError("unknown example workflow: " + flow)
            print(json.dumps({"workflow": flow, "result": output}, ensure_ascii=False, sort_keys=True, default=str))


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "commit")
