import json
import sys
import tempfile
from pathlib import Path
from strataleaf import Database, Archives, Conflict, InvalidDocument, InvalidCursor, ReadOnly, SnapshotTooOld, ReplicationError

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


def span_view(span):
    return {"start": span.start, "end": span.end, "state": span.state, "action": span.action,
            "revision": span.revision, "document": span.document}


def conflicts_with_traversal(db, collection, key, since, until):
    """Independent oracle: read every revision with the unchanged single-revision API."""
    answers = {}
    for revision in range(since, until + 1):
        observed = db.get(collection, key, revision)
        answers[revision] = ("present", observed.document) if observed is not None else ("absent", None)
    return answers


def expand(spans, since, until):
    """Flatten spans into one proven answer per revision, checking they tile the interval."""
    answers = {}
    expected = since
    for span in spans:
        if span.start != expected or span.end < span.start:
            raise RuntimeError("history spans do not tile the interval")
        for revision in range(span.start, span.end + 1):
            if revision in answers:
                raise RuntimeError("history spans overlap")
            answers[revision] = (span.state, span.document)
        expected = span.end + 1
    if expected != until + 1:
        raise RuntimeError("history spans leave a gap at the interval end")
    return answers


def page_history(db, collection, key, since, until, limit):
    spans, pages, cursor = [], 0, None
    while True:
        page = db.history(collection, key, since=since, until=until, limit=limit) if cursor is None \
            else db.history(collection, key, limit=limit, cursor=cursor)
        if page.since != since or page.until != until:
            raise RuntimeError("history endpoint drifted while paging")
        pages += 1
        if page.invalidated:
            raise RuntimeError("unexpectedly invalidated spans during simple paging")
        spans.extend(page.spans)
        cursor = page.cursor
        if cursor is None:
            return spans, pages


def history_example(db, first):
    """Cross-check interval traversal against an input timeline and single-revision reads."""
    db.catalog.create("trail")
    timeline = []  # (revision, state, document) recorded from the writes we issued, not from traversal

    def record(revision, state, document):
        timeline.append((revision, state, document))

    record(0, "absent", None)  # revision 0 pins the never-created boundary
    record(first, "absent", None)  # the seed commit never touched "track"
    carried = ("absent", None)
    for operation in ["put_a", "unrelated", "put_a",
                      "delete", "unrelated", "put_b", "put_a"]:
        if operation == "unrelated":
            db.batch().put("trail", "unrelated", {"tick": db.revision}).commit()
        elif operation == "delete":
            db.batch().delete("trail", "track").commit()
            carried = ("absent", None)
        else:
            document = {"value": operation[-1]}
            db.batch().put("trail", "track", document).commit()
            carried = ("present", document)
        record(db.revision, *carried)  # the state readable *at* the commit revision
    endpoint = db.revision

    spans, pages = page_history(db, "trail", "track", 0, endpoint, limit=2)
    proven = expand(spans, 0, endpoint)
    if proven != conflicts_with_traversal(db, "trail", "track", 0, endpoint):
        raise RuntimeError("traversal disagrees with per-revision single reads")
    if {revision: answer for revision, answer in proven.items()} != {revision: (state, document) for revision, state, document in timeline}:
        raise RuntimeError("traversal disagrees with the independent input timeline")
    point_checks = 0
    for revision, state, document in timeline:
        observed = db.get("trail", "track", revision)
        expected = None if state == "absent" else document
        if (None if observed is None else observed.document) != expected:
            raise RuntimeError(f"single-revision read disagrees at revision {revision}")
        point_checks += 1
    bodies = [span.document for span in spans if span.state == "present"]
    if len({json.dumps(body, sort_keys=True) for body in bodies}) == 1 and len(bodies) > 1:
        raise RuntimeError("equal bodies were collapsed into one span")

    # A write after the fixed endpoint must not enter the traversal.
    first_page = db.history("trail", "track", limit=2)
    fixed_until = first_page.until
    db.batch().put("trail", "track", {"value": "late"}).commit()
    late = db.revision
    after_late = {"value": "late"}  # only unrelated commits follow, so this stays readable
    resumed = db.history("trail", "track", limit=2, cursor=first_page.cursor)
    resumed_spans = list(first_page.spans) + list(resumed.spans)
    while resumed.cursor is not None:
        resumed = db.history("trail", "track", limit=2, cursor=resumed.cursor)
        resumed_spans.extend(resumed.spans)
    resumed_answers = expand(resumed_spans, first_page.since, fixed_until)
    if max(resumed_answers) != fixed_until or resumed_answers.get(late) is not None:
        raise RuntimeError("commit after the fixed endpoint leaked into the traversal")
    if resumed.until != fixed_until:
        raise RuntimeError("continuation moved the fixed endpoint")

    # Pin so the interval start falls below a later floor, then reclaim history.
    db.retention.pin("history_reader", fixed_until)
    for tick in range(3):
        db.batch().put("trail", "unrelated", {"tick": "after" + str(tick)}).commit()
    db.retention.unpin("history_reader")
    reclaimed = db.retention.compact(db.revision)
    floor = int(db._store.get("floor"))
    after = db.history("trail", "track", since=0)
    after_spans = [span_view(span) for span in after.spans]
    unknown_prefix = [span for span in after.spans if span.state == "unknown"]
    baseline = [span for span in after.spans if span.action == "floor_baseline"]
    if floor <= fixed_until or not unknown_prefix or not baseline:
        raise RuntimeError("compaction did not surface an unknown prefix and a baseline")
    if baseline[0].revision is None or baseline[0].revision >= floor:
        raise RuntimeError("baseline version was reported as a write inside the interval")
    if unknown_prefix[0].start != 0 or unknown_prefix[0].end != floor - 1:
        raise RuntimeError("unknown prefix does not cover revisions below the floor")
    # The floor keeps every key's newest version at or below the boundary, so the baseline
    # span must agree with what a single-revision read returns at the floor itself.
    readable = db.get("trail", "track", floor)
    if readable is None or readable.document != after_late:
        raise RuntimeError("baseline does not describe the document readable at the floor")
    if baseline[0].document != readable.document or baseline[0].start != floor:
        raise RuntimeError("baseline span disagrees with the single-revision read at the floor")
    if baseline[0].revision >= floor:
        raise RuntimeError("baseline row must lie before the floor, not inside the interval")

    # A continuation page whose covered revisions were reclaimed mid-traversal is flagged.
    # Start inside the retained region with several distinct spans so a real cursor exists.
    for tick in range(3):
        db.batch().put("trail", "unrelated", {"tick": "span" + str(tick)}).commit()
    stale = db.history("trail", "unrelated", since=int(db._store.get("floor")), limit=1)
    if stale.spans[0].state == "unknown" or stale.cursor is None:
        raise RuntimeError("traversal from the floor must be provable and pageable")
    covered = (stale.spans[0].start, stale.spans[0].end)
    for tick in range(2):
        db.batch().put("trail", "unrelated", {"tick": "late" + str(tick)}).commit()
    db.retention.compact(db.revision)
    continued = db.history("trail", "unrelated", limit=1, cursor=stale.cursor)
    if not continued.invalidated or continued.invalidated[0]["start"] != covered[0]:
        raise RuntimeError("reclaimed coverage was not reported as invalidated")
    reclaimed_now = [span for span in continued.spans if span.state == "unknown"]
    if not reclaimed_now or reclaimed_now[0].end != continued.until:
        raise RuntimeError("reclaimed revisions were not replaced by an unknown span")

    return {"timeline": [{"revision": revision, "state": state, "document": document}
                         for revision, state, document in timeline],
            "spans": [span_view(span) for span in spans], "pages": pages,
            "point_checks": point_checks, "fixed_endpoint": fixed_until,
            "late_revision": late, "reclaimed": reclaimed, "floor": floor,
            "after_floor": after_spans,
            "stale_coverage": {"start": covered[0], "end": covered[1]},
            "invalidated": list(continued.invalidated),
            "recovered": [span_view(span) for span in continued.spans],
            "rejected_endpoint": rejected(lambda: db.history("trail", "track", until=late,
                                                              limit=1, cursor=first_page.cursor), InvalidCursor)}


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
            elif flow == "history":
                output = history_example(db, first)
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
            else:
                raise ValueError("unknown example workflow: " + flow)
            print(json.dumps({"workflow": flow, "result": output}, ensure_ascii=False, sort_keys=True, default=str))


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "commit")
