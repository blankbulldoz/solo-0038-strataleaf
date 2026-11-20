"""Verify the format-1 compatibility sample against the current source.

Checks are executable and read-only: they materialize the committed SQL sample
into temporary databases, drive it through the public API, and compare the
observations recorded in ``manifest.json``.  One additional check regenerates
the sample through the public API and requires byte-identical fixture files, so
a write-path change that alters the persisted format fails loudly.

Run from the repository root::

    python3 -m compat.verify
    python3 -m compat.verify --skip-regenerate

No production code is modified.  Exit status is 0 only when every check passes.
"""
import argparse
import base64
import hashlib
import hmac
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from strataleaf import Database, Archives, InvalidCursor, SnapshotTooOld, ReadOnly

from . import sample
from .dump import load_database

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "format1"


class Checker:
    def __init__(self):
        self.results = []

    def check(self, name, function):
        try:
            detail = function()
        except Exception as exc:  # noqa: BLE001 - a check failure is data, not a crash
            self.results.append({"check": name, "ok": False,
                                 "detail": type(exc).__name__ + ": " + str(exc)})
        else:
            self.results.append({"check": name, "ok": True, "detail": detail})

    def require(self, condition, message):
        if not condition:
            raise AssertionError(message)

    @property
    def failed(self):
        return [result for result in self.results if not result["ok"]]


def _manifest():
    return json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))


def _counts(connection):
    return {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in sample.TABLES}


def _meta(connection):
    return {row[0]: row[1] for row in connection.execute("SELECT key,value FROM meta")}


def _rows(connection, query):
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(query)]


def verify_structure(checker, directory, manifest):
    """Schema, metadata and row-level facts of the committed sample."""
    primary = load_database(FIXTURE / "primary.sql", directory / "primary.db")
    replica = load_database(FIXTURE / "replica.sql", directory / "replica.db")
    baseline = load_database(FIXTURE / "backup-head8.sql", directory / "baseline.db")

    def schema_matches_ddl():
        expected = sqlite3.connect(":memory:")
        expected.executescript(__import__("strataleaf.storage", fromlist=["DDL"]).DDL)
        wanted = {row[0]: " ".join(row[1].split())
                  for row in expected.execute(
                      "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                      "AND name NOT LIKE 'sqlite_%' AND type IN ('table','index')")}
        expected.close()
        actual = sqlite3.connect(primary.resolve().as_uri() + "?mode=ro", uri=True)
        got = {row[0]: " ".join(row[1].split())
               for row in actual.execute(
                   "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                   "AND name NOT LIKE 'sqlite_%' AND type IN ('table','index')")}
        actual.close()
        missing = sorted(set(wanted) - set(got))
        extra = sorted(set(got) - set(wanted))
        changed = sorted(name for name in set(wanted) & set(got) if wanted[name] != got[name])
        checker.require(not missing and not extra and not changed,
                        "DDL differs: missing=" + repr(missing) + " extra=" + repr(extra)
                        + " changed=" + repr(changed))
        return {"objects": len(got), "all_equal": True}

    def primary_meta_and_counts():
        connection = sqlite3.connect(primary.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            meta, counts = _meta(connection), _counts(connection)
        finally:
            connection.close()
        checker.require(meta == manifest["primary"]["meta"], meta)
        checker.require(counts == manifest["primary"]["table_counts"], counts)
        checker.require(meta["format"] == "1", meta)
        checker.require(meta["head"] == "11" and meta["floor"] == "7"
                        and meta["catalog_epoch"] == "12", meta)
        return {"meta": meta, "counts": counts}

    def replica_meta_and_counts():
        connection = sqlite3.connect(replica.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            meta, counts = _meta(connection), _counts(connection)
        finally:
            connection.close()
        checker.require(meta == manifest["replica"]["meta"], meta)
        checker.require(counts == manifest["replica"]["table_counts"], counts)
        checker.require(meta["mode"] == "replica", meta)
        checker.require(meta["upstream"] == meta["database_id"], meta)
        connection = sqlite3.connect(replica.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = _rows(connection, "SELECT rev,request_key,request_hash FROM commits ORDER BY rev")
        finally:
            connection.close()
        restored = [row for row in rows if row["rev"] <= 8]
        replicated = [row for row in rows if row["rev"] > 8]
        checker.require(all(row["request_key"] is not None for row in restored), rows)
        checker.require(all(row["request_key"] is None and row["request_hash"] is None
                            for row in replicated), rows)
        return {"meta": meta, "counts": counts,
                "restored_commits_keep_request_key": [row["rev"] for row in restored],
                "replicated_commits_drop_request_key": [row["rev"] for row in replicated]}

    def chains_and_tombstones():
        connection = sqlite3.connect(primary.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = [dict(row) for row in connection.execute(
                "SELECT collection,key,rev,body,expires FROM versions ORDER BY rowid")]
        finally:
            connection.close()
        chains = {}
        for row in rows:
            body = None if row["body"] is None else json.loads(row["body"])
            chains.setdefault(row["collection"] + "/" + row["key"], []).append(
                {"rev": row["rev"], "body": body, "expires": row["expires"]})
        checker.require(chains == manifest["primary"]["chains"],
                        "version chains differ from manifest")
        tombstones = [name for name, chain in chains.items()
                      if any(item["body"] is None for item in chain)]
        checker.require(tombstones == ["segments/b", "segments/t", "segments/ttl"], tombstones)
        # A JSON null field must not be confused with a tombstone.
        null_field = [name for name, chain in chains.items()
                      if any(item["body"] is not None and isinstance(item["body"], dict)
                             and item["body"].get("rank", 0) is None for item in chain)]
        checker.require(null_field == ["metrics/m5"], null_field)
        return {"chains": len(chains), "tombstone_keys": tombstones,
                "json_null_keys": null_field}

    def receipts_survive_compaction():
        connection = sqlite3.connect(primary.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            receipts = _rows(connection, "SELECT * FROM receipts ORDER BY request_key")
            commits = _rows(connection, "SELECT * FROM commits ORDER BY rev")
        finally:
            connection.close()
        checker.require([item["rev"] for item in commits] == [8, 9, 10, 11], commits)
        checker.require(any(item["request_key"] == "seed" and item["rev"] == 1
                            for item in receipts), receipts)
        checker.require([item["request_key"] for item in receipts] ==
                        [item["request_key"] for item in manifest["primary"]["receipts"]],
                        receipts)
        return {"commits": [item["rev"] for item in commits],
                "receipts": len(receipts), "receipt_at_or_below_floor": True}

    def index_tokens():
        connection = sqlite3.connect(primary.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = _rows(connection, "SELECT * FROM index_values "
                                      "ORDER BY collection,name,token,key")
        finally:
            connection.close()
        checker.require(rows == manifest["primary"]["index_values"],
                        "index projection differs from manifest")
        tokens = {(row["collection"], row["name"], row["token"], row["key"]) for row in rows}
        for expected in [("metrics", "by_rank", "b:1", "m1"),
                         ("metrics", "by_rank", "n:1", "m2"),
                         ("metrics", "by_rank", "n:1", "m3"),
                         ("metrics", "by_rank", "n:1.5", "m4"),
                         ("metrics", "by_rank", "v:null", "m5"),
                         ("metrics", "by_rank", "v:\"x\"", "m6"),
                         ("segments", "by_region", "v:\"东\"", "u")]:
            checker.require(expected in tokens, "missing index token " + repr(expected))
        for absent in ("m7", "m8", "m9"):
            checker.require(not any(key == absent for _, _, _, key in tokens),
                            "unexpected indexed value for " + absent)
        return {"tokens": len(tokens), "numeric_merge": ["n:1 -> m2,m3"],
                "bool_not_merged": ["b:1 -> m1"], "skipped": ["m7", "m8", "m9"]}

    checker.check("structure/schema-matches-source-ddl", schema_matches_ddl)
    checker.check("structure/primary-meta-and-counts", primary_meta_and_counts)
    checker.check("structure/replica-meta-and-counts", replica_meta_and_counts)
    checker.check("structure/version-chains-and-tombstones", chains_and_tombstones)
    checker.check("structure/receipts-outlive-floor", receipts_survive_compaction)
    checker.check("structure/index-token-encoding", index_tokens)
    return primary, replica, baseline


def verify_reads(checker, primary, manifest):
    observed = manifest["observed"]

    def history_reads():
        with Database(primary) as db:
            checker.require(db.get("segments", "a", 7).revision == 2, "a@7")
            checker.require(db.get("segments", "b", 7) is None, "b@7 tombstone")
            checker.require(db.get("segments", "c", 7).revision == 5, "c@7 recreate")
            checker.require(db.get("segments", "t", 7) is None, "t@7 expiry tombstone")
            checker.require(db.get("marks", "m0", 7).revision == 1, "m0@7")
            checker.require(db.get("segments", "d", 7) is None, "d@7 created after floor")
            # Expiry is a delete commit: the old value is still readable at its own revision.
            old = db.get("segments", "ttl", 10)
            checker.require(old is not None and old.expires_at == 200.0, old)
            checker.require(db.get("segments", "ttl", 11) is None, "ttl@11")
            for revision in (6,):
                try:
                    db.get("segments", "a", revision)
                except SnapshotTooOld:
                    pass
                else:
                    raise AssertionError("read below floor was accepted")
            return {"a@7": 2, "b@7": None, "c@7": 5, "t@7": None,
                    "below_floor": "SnapshotTooOld"}

    def query_and_cursor():
        with Database(primary) as db:
            page = db.query("segments", observed["cursor"]["predicate"], limit=1)
            checker.require(page.revision == 11, page.revision)
            checker.require(page.cursor == observed["cursor"]["token"], page.cursor)
            checker.require([record.key for record in page.records] == ["c"],
                            [record.key for record in page.records])
            follow = db.query("segments", observed["cursor"]["predicate"], limit=1,
                              cursor=page.cursor)
            checker.require([record.key for record in follow.records] == ["d"],
                            [record.key for record in follow.records])
            try:
                db.query("segments", {"field": "region", "op": "eq", "value": "west"},
                         limit=1, cursor=page.cursor)
            except InvalidCursor:
                pass
            else:
                raise AssertionError("cursor was accepted for a different predicate")
        return {"token_sha256_prefix": hashlib.sha256(
                    observed["cursor"]["token"].encode()).hexdigest()[:16],
                "pages": [["c"], ["d"]]}

    def cursor_payload_is_signed_and_bound():
        token = observed["cursor"]["token"]
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        signature, payload_bytes = raw[:32], raw[32:]
        expected = hmac.new(bytes.fromhex(sample.SAMPLE_CURSOR_SECRET), payload_bytes,
                            hashlib.sha256).digest()
        checker.require(hmac.compare_digest(signature, expected), "HMAC mismatch")
        payload = json.loads(payload_bytes)
        checker.require(set(payload) == {"after", "database", "query", "revision"}, payload)
        checker.require(payload["database"] == sample.SAMPLE_DATABASE_ID, payload)
        checker.require(payload["revision"] == 11 and payload["after"] == "c", payload)
        tampered = bytearray(raw)
        tampered[-1] = tampered[-1] ^ 0xFF
        with Database(primary) as db:
            try:
                db.query("segments", observed["cursor"]["predicate"], limit=1,
                         cursor=base64.urlsafe_b64encode(bytes(tampered)).decode().rstrip("="))
            except InvalidCursor:
                pass
            else:
                raise AssertionError("tampered cursor was accepted")
        return {"payload_keys": sorted(payload), "database": payload["database"],
                "tamper": "InvalidCursor"}

    def feed_and_floor():
        with Database(primary) as db:
            commits = db.feed.read(7)
            checker.require([commit["revision"] for commit in commits] == [8, 9, 10, 11],
                            commits)
            checker.require(db.feed.position("mirror") == 7, "consumer ack")
            try:
                db.feed.read(6)
            except SnapshotTooOld:
                pass
            else:
                raise AssertionError("feed read below floor was accepted")
        return {"commits_above_floor": [8, 9, 10, 11], "ack": 7,
                "below_floor": "SnapshotTooOld"}

    def diagnostics():
        with Database(primary) as db:
            report = db.diagnostics.inspect(deep=True)
            checker.require(report["ok"], report)
            checker.require(report["head"] == 11 and report["floor"] == 7, report)
            for revision, expected in observed["fingerprints"].items():
                actual = db.diagnostics.fingerprint(int(revision))
                checker.require(actual == expected, (revision, actual, expected))
        return {"inspect_ok": True, "fingerprints": sorted(observed["fingerprints"])}

    checker.check("reads/history-and-floor", history_reads)
    checker.check("reads/query-cursor", query_and_cursor)
    checker.check("reads/cursor-signature", cursor_payload_is_signed_and_bound)
    checker.check("reads/feed-and-floor", feed_and_floor)
    checker.check("reads/diagnostics-and-fingerprints", diagnostics)


def verify_backup_and_replica(checker, directory, primary, manifest):
    observed = manifest["observed"]

    def restore_primary():
        baseline = load_database(FIXTURE / "backup-head8.sql", directory / "restore-base.db")
        receipt = Archives.restore(baseline, directory / "restored.db")
        checker.require(receipt["revision"] == 8 and receipt["floor"] == 7, receipt)
        with Database(directory / "restored.db") as db:
            checker.require(db.diagnostics.fingerprint(8) ==
                            observed["fingerprints"]["8"], "fingerprint after restore")
            checker.require(db._store.get("database_id") != sample.SAMPLE_DATABASE_ID,
                            "restored primary kept identity")
            checker.require(db._store.get("mode") == "primary", "restored mode")
            checker.require(db._store.connection.execute(
                "SELECT COUNT(*) FROM pins").fetchone()[0] == 0, "pins restored")
            checker.require(db._store.connection.execute(
                "SELECT COUNT(*) FROM consumers").fetchone()[0] == 0, "consumers restored")
            # Cursors signed by the old database are rejected after re-identity.
            try:
                db.query("segments", observed["cursor"]["predicate"], limit=1,
                         cursor=observed["cursor"]["token"])
            except InvalidCursor:
                pass
            else:
                raise AssertionError("old cursor accepted by re-identified database")
        return {"revision": 8, "floor": 7, "identity_replaced": True,
                "pins": 0, "consumers": 0}

    def replication_replay():
        baseline = load_database(FIXTURE / "backup-head8.sql", directory / "fw-base.db")
        follower_path = directory / "follower.db"
        Archives.restore(baseline, follower_path, follower=True)
        with Database(follower_path) as replica:
            checker.require(replica._store.get("mode") == "replica", "follower mode")
            checker.require(replica._store.get("upstream") == sample.SAMPLE_DATABASE_ID,
                            "follower upstream")
            for name in ("document-and-catalog", "catalog-only", "document", "expiry-delete"):
                envelope = json.loads((FIXTURE / (name + "-packet.json"))
                                      .read_text(encoding="utf-8"))
                result = replica.replication.apply(envelope)
                expected = observed["packets"][name]
                checker.require(result["revision"] == expected["through"],
                                (name, result, expected))
                repeat = replica.replication.apply(envelope)
                checker.require(repeat["applied"] == 0, (name, repeat))
            checker.require(replica.diagnostics.fingerprint() ==
                            observed["fingerprints"]["11"], "replayed fingerprint")
            checker.require(replica._store.get("catalog_epoch") == "12",
                            replica._store.get("catalog_epoch"))
            checker.require(replica._store.get("floor") == "7",
                            replica._store.get("floor"))
            replica_page = replica.query("segments", observed["cursor"]["predicate"],
                                         limit=1, cursor=observed["cursor"]["token"])
            checker.require([record.key for record in replica_page.records] == ["d"],
                            [record.key for record in replica_page.records])
            try:
                replica.batch().put("segments", "nope", {"region": "x", "sequence": 1}).commit()
            except ReadOnly:
                pass
            else:
                raise AssertionError("replica accepted a primary write")
        # The replayed state must equal the committed replica sample row for row.
        expected_db = load_database(FIXTURE / "replica.sql", directory / "replica-expected.db")
        tables = ("collections", "versions", "commits", "index_defs", "index_values",
                  "pins", "consumers")
        actual_connection = sqlite3.connect(follower_path)
        expected_connection = sqlite3.connect(expected_db)
        try:
            for table in tables:
                actual = [tuple(row) for row in actual_connection.execute(
                    "SELECT * FROM " + table + " ORDER BY rowid")]
                wanted = [tuple(row) for row in expected_connection.execute(
                    "SELECT * FROM " + table + " ORDER BY rowid")]
                checker.require(actual == wanted, "replayed " + table + " differs")
        finally:
            actual_connection.close()
            expected_connection.close()
        return {"packets_applied": 4, "repeats_applied": 0, "matches_replica_sql": True}

    def backup_api_receipt():
        # The committed baseline is the SQL form of a real backup.  Taking a fresh
        # backup through the API must preserve the same meta and fingerprint facts.
        baseline = load_database(FIXTURE / "backup-head8.sql", directory / "bk-base.db")
        with Database(baseline) as db:
            target = directory / "bk-fresh.db"
            receipt = db.archives.backup(target)
            checker.require(receipt["revision"] == 8 and receipt["floor"] == 7, receipt)
            checker.require(receipt["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest(),
                            "receipt digest is not the file digest")
            restored = Archives.restore(target, directory / "bk-fresh-restored.db",
                                        expected_sha256=receipt["sha256"])
            checker.require(restored["revision"] == 8 and restored["floor"] == 7, restored)
            with Database(directory / "bk-fresh-restored.db") as restored_db:
                checker.require(restored_db._store.get("database_id") != receipt["database_id"],
                                "primary restore kept identity")
            try:
                Archives.restore(target, directory / "bk-fresh-restored.db")
            except Exception as exc:
                checker.require(type(exc).__name__ == "Conflict", type(exc).__name__)
            else:
                raise AssertionError("restore overwrote an existing destination")
        return {"receipt": {"revision": 8, "floor": 7}, "digest_is_file_digest": True,
                "no_overwrite": True}

    checker.check("backup/restore-reidentifies-primary", restore_primary)
    checker.check("replica/replay-committed-packets", replication_replay)
    checker.check("backup/api-receipt-and-no-overwrite", backup_api_receipt)


def verify_format_guards(checker, directory, primary):
    def future_format_refused():
        target = directory / "format2.db"
        connection = sqlite3.connect(target)
        connection.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta VALUES ('format','2');"
            "INSERT INTO meta VALUES ('head','9');")
        connection.commit()
        before = connection.execute("SELECT * FROM meta ORDER BY key").fetchall()
        connection.close()
        try:
            Database(target).close()
        except Exception as exc:
            checker.require(type(exc).__name__ == "DatabaseError", type(exc).__name__)
        else:
            raise AssertionError("a format-2 file was opened as format 1")
        connection = sqlite3.connect(target)
        after = connection.execute("SELECT * FROM meta ORDER BY key").fetchall()
        tables = sorted(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        connection.close()
        # Observed caveat: the guard raises, but the DDL script runs before the
        # check, so empty tables may already be present in the rejected file.
        return {"meta_unchanged": before == after, "tables_present": tables}

    def plain_sqlite_file_is_adopted():
        target = directory / "foreign.db"
        connection = sqlite3.connect(target)
        connection.executescript(
            "CREATE TABLE business (id INTEGER PRIMARY KEY, note TEXT NOT NULL);"
            "INSERT INTO business VALUES (1,'x');")
        connection.commit()
        connection.close()
        with Database(target) as db:
            meta = _meta(db._store.connection)
            business = db._store.connection.execute(
                "SELECT COUNT(*) FROM business").fetchone()[0]
            checker.require(meta["head"] == "0" and meta["floor"] == "0", meta)
            checker.require(business == 1, business)
        return {"meta_initialized": True, "foreign_rows_untouched": business,
                "caveat": "a non-StrataLeaf SQLite file is adopted, not rejected"}

    checker.check("guards/future-format-refused", future_format_refused)
    checker.check("guards/plain-sqlite-adopted", plain_sqlite_file_is_adopted)


def verify_dump_fidelity(checker, primary, directory):
    """Dumping a loaded sample again must reproduce the committed text exactly."""
    def dump_round_trip():
        from .dump import dump_database, load_database
        loaded = directory / "round-trip.db"
        load_database(FIXTURE / "primary.sql", loaded)
        rewritten = directory / "primary-rewritten.sql"
        dump_database(loaded, rewritten,
                      note="Format-1 primary sample: head=11 floor=7 catalog_epoch=12.")
        checker.require(rewritten.read_bytes() == (FIXTURE / "primary.sql").read_bytes(),
                        "SQL round-trip is not stable")
        return {"stable": True, "bytes": len(rewritten.read_bytes())}

    checker.check("dump/sql-round-trip-stable", dump_round_trip)


def verify_regeneration(checker, directory):
    """The committed fixture must be exactly what the current public API writes."""
    def regenerate_byte_identical():
        from .dump import dump_database
        (directory / "regen").mkdir(exist_ok=True)
        built = sample.build(directory / "regen")
        target = directory / "regen-out"
        target.mkdir()
        dump_database(built["primary_path"], target / "primary.sql",
                      note="Format-1 primary sample: head=11 floor=7 catalog_epoch=12.")
        dump_database(built["replica_path"], target / "replica.sql",
                      note="Format-1 replica sample after catch-up: same identity, mode=replica.")
        dump_database(built["backup_path"], target / "backup-head8.sql",
                      note="Snapshot behind backup.db: head=8 floor=7 catalog_epoch=7, "
                           "pins and consumers still present as at backup time.")
        for name, envelope in built["packets"].items():
            (target / (name + "-packet.json")).write_text(
                json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8")
        (target / "cursor.json").write_text(
            json.dumps(built["observed"]["cursor"], ensure_ascii=False,
                       sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (target / "manifest.json").write_text(
            json.dumps(sample.manifest(built, directory / "regen"), ensure_ascii=False,
                       sort_keys=True, indent=2) + "\n", encoding="utf-8")
        comparisons = {}
        for name in ("primary.sql", "replica.sql", "backup-head8.sql", "cursor.json",
                     "manifest.json", "document-and-catalog-packet.json",
                     "catalog-only-packet.json", "document-packet.json",
                     "expiry-delete-packet.json"):
            committed = (FIXTURE / name).read_bytes()
            fresh = (target / name).read_bytes()
            comparisons[name] = hashlib.sha256(fresh).hexdigest()[:16]
            checker.require(committed == fresh, "fixture changed: " + name)
        return comparisons

    checker.check("regeneration/byte-identical", regenerate_byte_identical)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-regenerate", action="store_true",
                        help="skip rebuilding the sample through the public API")
    arguments = parser.parse_args(argv)
    if not FIXTURE.exists():
        print("missing fixture directory: " + str(FIXTURE), file=sys.stderr)
        return 2
    manifest = _manifest()
    checker = Checker()
    with tempfile.TemporaryDirectory(prefix="strataleaf-verify-") as directory:
        directory = Path(directory)
        primary, replica, baseline = verify_structure(checker, directory, manifest)
        verify_reads(checker, primary, manifest)
        verify_backup_and_replica(checker, directory, primary, manifest)
        verify_format_guards(checker, directory, primary)
        verify_dump_fidelity(checker, primary, directory)
        if not arguments.skip_regenerate:
            verify_regeneration(checker, directory)
    print(json.dumps({"ok": not checker.failed, "checks": checker.results,
                      "failed": len(checker.failed)}, ensure_ascii=False, indent=2))
    return 0 if not checker.failed else 1


if __name__ == "__main__":
    sys.exit(main())
