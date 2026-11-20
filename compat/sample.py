"""Build the committed format-1 compatibility sample through the public API.

Every state transition below goes through ``strataleaf.Database`` public
methods.  Only two opaque identity values (``database_id`` and
``cursor_secret``) are pinned with a scoped patch so that the generated fixture,
the captured cursor and the replication packets are reproducible byte for byte.
No production code is imported privately or modified.

Run ``python3 -m compat.sample --write`` to regenerate the fixture files.
"""
import argparse
import contextlib
import json
import platform
import sqlite3
import sys
import tempfile
import unittest.mock
import uuid
from pathlib import Path

from strataleaf import Database, Archives
import strataleaf

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "format1"

# Sample-only identity.  These are deliberately fixed so that cursors and
# packet checksums in the fixture are reproducible.  They are not secrets.
SAMPLE_DATABASE_ID = "00000000-0000-4000-8000-000000000001"
SAMPLE_CURSOR_SECRET = "5a" * 32

SEGMENTS_V1 = {
    "fields": {"region": "string", "sequence": "integer", "code": "string"},
    "required": ["region", "sequence"],
}
SEGMENTS_V2 = {
    "fields": {"region": "string", "sequence": "integer", "code": "string", "note": "string"},
    "required": ["region", "sequence"],
}
MARKS = {"fields": {"segment": "string"}, "required": ["segment"]}
METRICS = {}

SEED_OPERATIONS = [
    ("segments", "a", {"region": "east", "sequence": 0, "code": "c0"}),
    ("segments", "b", {"region": "west", "sequence": 1, "code": "c1"}),
    ("segments", "c", {"region": "east", "sequence": 2, "code": "c2"}),
    ("segments", "u", {"region": "东", "sequence": 50, "code": "c50"}),
    ("marks", "m0", {"segment": "a"}),
]
# One document per scalar/token edge: bool, int, float, non-integral float,
# explicit null, string, missing field, array, object.
METRIC_OPERATIONS = [
    ("m1", {"rank": True}),
    ("m2", {"rank": 1}),
    ("m3", {"rank": 1.0}),
    ("m4", {"rank": 1.5}),
    ("m5", {"rank": None}),
    ("m6", {"rank": "x"}),
    ("m7", {}),
    ("m8", {"rank": []}),
    ("m9", {"rank": {}}),
]

CURSOR_PREDICATE = {"field": "region", "op": "eq", "value": "east"}

TABLES = ("collections", "versions", "commits", "receipts",
          "index_defs", "index_values", "pins", "consumers")


@contextlib.contextmanager
def _fixed_identity():
    def fixed_uuid():
        return uuid.UUID(SAMPLE_DATABASE_ID)

    def fixed_hex(length):
        if length != 32:
            raise AssertionError("unexpected token length")
        return SAMPLE_CURSOR_SECRET

    with unittest.mock.patch("strataleaf.storage.uuid.uuid4", fixed_uuid), \
            unittest.mock.patch("strataleaf.storage.secrets.token_hex", fixed_hex):
        yield


def _seed(db, request_key="seed"):
    batch = db.batch(request_key)
    for collection, key, document in SEED_OPERATIONS:
        batch.put(collection, key, document)
    for key, document in METRIC_OPERATIONS:
        batch.put("metrics", key, document)
    return batch.commit()


def _snapshot_rows(path):
    """Read committed rows from a closed database read-only (observation only)."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        meta = {row["key"]: row["value"] for row in connection.execute("SELECT * FROM meta")}
        counts = {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                  for table in TABLES}
        chains = {}
        for row in connection.execute("SELECT * FROM versions ORDER BY collection,key,rev"):
            body = None if row["body"] is None else json.loads(row["body"])
            chains.setdefault(row["collection"] + "/" + row["key"], []).append(
                {"rev": row["rev"], "body": body, "expires": row["expires"]})
        connections = {}
        for row in connection.execute("SELECT * FROM collections ORDER BY name"):
            connections[row["name"]] = {"schema": json.loads(row["schema"]),
                                        "generation": row["generation"]}
        index_defs = [dict(row) for row in connection.execute(
            "SELECT * FROM index_defs ORDER BY collection,name")]
        index_values = [dict(row) for row in connection.execute(
            "SELECT * FROM index_values ORDER BY collection,name,token,key")]
        commits = [dict(row) for row in connection.execute("SELECT * FROM commits ORDER BY rev")]
        receipts = [dict(row) for row in connection.execute("SELECT * FROM receipts ORDER BY request_key")]
        pins = [dict(row) for row in connection.execute("SELECT * FROM pins ORDER BY name")]
        consumers = [dict(row) for row in connection.execute("SELECT * FROM consumers ORDER BY name")]
        return {"meta": meta, "counts": counts, "chains": chains, "collections": connections,
                "index_defs": index_defs, "index_values": index_values,
                "commits": commits, "receipts": receipts, "pins": pins, "consumers": consumers}
    finally:
        connection.close()


def _packet_summary(envelope):
    body = envelope["body"]
    return {
        "sha256": envelope["sha256"],
        "format": body["format"],
        "since": body["since"],
        "through": body["through"],
        "head": body["head"],
        "floor": body["floor"],
        "catalog_epoch": body["catalog_epoch"],
        "commit_revisions": [commit["revision"] for commit in body["commits"]],
        "collection_names": [definition["name"] for definition in body["collections"]],
        "index_names": [definition["collection"] + "/" + definition["name"]
                        for definition in body["indexes"]],
    }


def build(root):
    """Create the primary, backup and replica files under *root*.

    Returns a dictionary of observed facts and captured packets.  The sequence
    and its asserted revisions are the sample's contract; changing the write
    path in a way that moves any of these values fails generation on purpose.
    """
    root = Path(root)
    primary_path = root / "primary.db"
    backup_path = root / "backup.db"
    replica_path = root / "replica.db"
    packets = {}

    with _fixed_identity():
        with Database(primary_path) as db:
            db.catalog.create("segments", SEGMENTS_V1)
            db.catalog.create("marks", MARKS)
            db.catalog.create("metrics", METRICS)
            seeded = _seed(db)
            assert seeded.revision == 1 and seeded.count == 14, seeded

            # Schema evolution is catalog-only: head stays at 1.
            assert db.catalog.evolve("segments", SEGMENTS_V2, 1) == 2
            db.indexes.create("segments", "by_region", "region")
            db.indexes.create("segments", "unique_code", "code", unique=True)
            db.indexes.create("metrics", "by_rank", "rank")
            assert db.revision == 1
            assert db._store.get("catalog_epoch") == "7"

            assert db.batch("update_a").put(
                "segments", "a", {"region": "north", "sequence": 10, "code": "c0"},
                expected=1).commit().revision == 2
            assert db.batch("delete_c").delete(
                "segments", "c", expected=1).commit().revision == 3
            assert db.batch("delete_b").delete(
                "segments", "b", expected=1).commit().revision == 4
            assert db.batch("recreate_c").put(
                "segments", "c", {"region": "east", "sequence": 20, "code": "c9"},
                expected=3).commit().revision == 5
            assert db.batch("timed").put(
                "segments", "t", {"region": "south", "sequence": 30, "code": "c30"},
                expected=0, expires_at=100).commit().revision == 6
            assert db.retention.sweep(100) == {"expired": 1, "revision": 7}

            # Two retention passes: the first is blocked by a pin and a consumer,
            # the second advances the floor to the requested head.
            db.retention.pin("reader_a", 4)
            db.feed.register("mirror", 2)
            db.feed.acknowledge("mirror", 4)
            first_compact = db.retention.compact(7)
            second_compact = db.retention.compact(7)
            db.retention.unpin("reader_a")
            db.feed.acknowledge("mirror", 7)
            db.retention.pin("reader_b", 7)
            final_compact = db.retention.compact(7)
            assert first_compact == {"requested": 7, "floor": 4, "removed_versions": 3,
                                     "removed_commits": 4, "blocked": True}, first_compact
            assert second_compact == {"requested": 7, "floor": 4, "removed_versions": 0,
                                      "removed_commits": 0, "blocked": True}, second_compact
            assert final_compact == {"requested": 7, "floor": 7, "removed_versions": 2,
                                     "removed_commits": 3, "blocked": False}, final_compact

            assert db.batch("after_floor").put(
                "segments", "d", {"region": "east", "sequence": 40, "code": "c40"},
                expected=0).put(
                "segments", "ttl", {"region": "south", "sequence": 70, "code": "c70"},
                expected=0, expires_at=200).commit().revision == 8
            assert db.revision == 8 and db._store.get("floor") == "7"
            assert db._store.get("catalog_epoch") == "7"

            fingerprint_at_7 = db.diagnostics.fingerprint(7)
            fingerprint_at_8 = db.diagnostics.fingerprint(8)
            assert fingerprint_at_7["documents"] == 13, fingerprint_at_7
            assert fingerprint_at_8["documents"] == 15, fingerprint_at_8

            backup = db.archives.backup(backup_path)
            assert backup["revision"] == 8 and backup["floor"] == 7, backup
            assert backup["database_id"] == SAMPLE_DATABASE_ID, backup

            assert db.batch("post_e").put(
                "segments", "e", {"region": "west", "sequence": 60, "code": "c60"},
                expected=0).commit().revision == 9
            # Catalog-only changes after the backup: head stays at 9, epoch moves.
            db.catalog.create("regions", {"fields": {"code": "string"}})
            db.indexes.create("segments", "by_code", "code")
            db.catalog.evolve("regions", {"fields": {"code": "string", "note": "string"}}, 1)
            db.catalog.create("audit", {})
            assert db.revision == 9 and db._store.get("catalog_epoch") == "11"
            assert db._store.get("floor") == "7"

        # A follower restored from the baseline backup keeps the source identity
        # and loses pins/consumers.
        Archives.restore(backup_path, replica_path, follower=True)

        with Database(primary_path) as db, Database(replica_path) as replica:
            assert replica.revision == 8
            assert replica._store.get("floor") == "7"
            assert replica._store.get("catalog_epoch") == "7"
            assert replica._store.get("mode") == "replica"
            assert replica._store.get("upstream") == SAMPLE_DATABASE_ID
            assert replica._store.get("database_id") == SAMPLE_DATABASE_ID
            assert replica._store.get("cursor_secret") == SAMPLE_CURSOR_SECRET

            packet = db.replication.export(replica.revision)
            packets["document-and-catalog"] = packet
            applied = replica.replication.apply(packet)
            assert applied == {"revision": 9, "applied": 1}, applied
            assert replica._store.get("catalog_epoch") == "11"

            # Catalog-only advance at the same document head.
            db.indexes.create("segments", "by_seq", "sequence")
            assert db.revision == 9 and db._store.get("catalog_epoch") == "12"
            packet = db.replication.export(replica.revision)
            packets["catalog-only"] = packet
            applied = replica.replication.apply(packet)
            assert applied == {"revision": 9, "applied": 0}, applied
            assert replica._store.get("catalog_epoch") == "12"

            assert db.batch("post_audit").put(
                "segments", "f", {"region": "north", "sequence": 80, "code": "c80"},
                expected=0).commit().revision == 10
            packet = db.replication.export(replica.revision)
            packets["document"] = packet
            assert replica.replication.apply(packet) == {"revision": 10, "applied": 1}

            assert db.retention.sweep(250) == {"expired": 1, "revision": 11}
            packet = db.replication.export(replica.revision)
            packets["expiry-delete"] = packet
            assert replica.replication.apply(packet) == {"revision": 11, "applied": 1}
            assert replica.replication.apply(packet) == {"revision": 11, "applied": 0}

            fingerprint_at_11 = db.diagnostics.fingerprint()
            assert fingerprint_at_11["documents"] == 16, fingerprint_at_11
            assert replica.diagnostics.fingerprint() == fingerprint_at_11

            page = db.query("segments", CURSOR_PREDICATE, limit=1)
            assert page.revision == 11 and page.cursor is not None
            assert [record.key for record in page.records] == ["c"], page
            assert page.plan["access"] == "equality_index", page.plan
            follow = db.query("segments", CURSOR_PREDICATE, limit=1, cursor=page.cursor)
            assert [record.key for record in follow.records] == ["d"], follow
            assert follow.cursor is None
            replica_page = replica.query("segments", CURSOR_PREDICATE, limit=1,
                                         cursor=page.cursor)
            assert [record.key for record in replica_page.records] == ["d"], replica_page

            cursor_token = page.cursor
            observed = {
                "fingerprints": {"7": fingerprint_at_7, "8": fingerprint_at_8,
                                 "11": fingerprint_at_11},
                "compaction": {"blocked_by_pin_and_consumer": first_compact,
                               "repeated_blocked": second_compact,
                               "advanced": final_compact},
                "backup": backup,
                "packets": {name: _packet_summary(envelope)
                            for name, envelope in packets.items()},
                "cursor": {"predicate": CURSOR_PREDICATE, "token": cursor_token,
                           "first_page_keys": ["c"], "second_page_keys": ["d"],
                           "replica_second_page_keys": ["d"]},
                "primary_reads": {
                    "a@7": db.get("segments", "a", 7).__dict__,
                    "b@7": db.get("segments", "b", 7),
                    "c@7": db.get("segments", "c", 7).__dict__,
                    "t@7": db.get("segments", "t", 7),
                    "ttl@10": db.get("segments", "ttl", 10).__dict__,
                    "ttl@11": db.get("segments", "ttl", 11),
                },
            }

        primary = _snapshot_rows(primary_path)
        replica_rows = _snapshot_rows(replica_path)

    # Structural assertions that pin the committed sample.
    assert primary["meta"] == {
        "format": "1", "head": "11", "floor": "7",
        "database_id": SAMPLE_DATABASE_ID, "cursor_secret": SAMPLE_CURSOR_SECRET,
        "mode": "primary", "upstream": "", "catalog_epoch": "12",
    }, primary["meta"]
    assert replica_rows["meta"] == {
        "format": "1", "head": "11", "floor": "7",
        "database_id": SAMPLE_DATABASE_ID, "cursor_secret": SAMPLE_CURSOR_SECRET,
        "mode": "replica", "upstream": SAMPLE_DATABASE_ID, "catalog_epoch": "12",
    }, replica_rows["meta"]
    assert primary["counts"] == {"collections": 5, "versions": 20, "commits": 4,
                                 "receipts": 9, "index_defs": 5, "index_values": 30,
                                 "pins": 1, "consumers": 1}, primary["counts"]
    assert replica_rows["counts"] == {"collections": 5, "versions": 20, "commits": 4,
                                      "receipts": 7, "index_defs": 5, "index_values": 30,
                                      "pins": 0, "consumers": 0}, replica_rows["counts"]

    return {
        "primary_path": primary_path,
        "replica_path": replica_path,
        "backup_path": backup_path,
        "observed": observed,
        "primary": primary,
        "replica": replica_rows,
        "packets": packets,
    }


def _strip_paths(observed, root):
    """Make observed facts independent of the temporary directory name."""
    text = json.dumps(observed, ensure_ascii=False, sort_keys=True)
    return json.loads(text.replace(str(root), "<tmp>").replace(str(Path(root).resolve()), "<tmp>"))


def manifest(built, root):
    primary, replica = built["primary"], built["replica"]
    return {
        "fixture": "strataleaf-format-1",
        "purpose": "Executable format and compatibility sample; see docs/storage-format.md.",
        "generated_by": "python3 -m compat.sample --write",
        "environment": {
            "strataleaf_version": strataleaf.__version__,
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "identity": {
            "database_id": SAMPLE_DATABASE_ID,
            "cursor_secret": SAMPLE_CURSOR_SECRET,
            "note": "Sample-only fixed identity so the cursor and packet digests reproduce.",
        },
        "primary": {
            "meta": primary["meta"],
            "table_counts": primary["counts"],
            "collections": primary["collections"],
            "index_defs": primary["index_defs"],
            "chains": primary["chains"],
            "commits": primary["commits"],
            "receipts": primary["receipts"],
            "pins": primary["pins"],
            "consumers": primary["consumers"],
            "index_values": primary["index_values"],
        },
        "replica": {
            "meta": replica["meta"],
            "table_counts": replica["counts"],
            "collections": replica["collections"],
            "index_defs": replica["index_defs"],
            "chains": replica["chains"],
            "commits": replica["commits"],
            "receipts": replica["receipts"],
            "pins": replica["pins"],
            "consumers": replica["consumers"],
            "index_values": replica["index_values"],
        },
        "observed": _strip_paths(built["observed"], root),
        "packets": {
            "document-and-catalog": "document-and-catalog-packet.json",
            "catalog-only": "catalog-only-packet.json",
            "document": "document-packet.json",
            "expiry-delete": "expiry-delete-packet.json",
        },
        "observations": [
            {"id": "O1", "claim": "head is a document-commit revision, not a catalog counter",
             "evidence": "primary.meta.head=11 while catalog_epoch=12; catalog-only changes left commits at [8,9,10,11]"},
            {"id": "O2", "claim": "floor is independent of head and catalog_epoch",
             "evidence": "primary.meta.floor=7 with head=11 and catalog_epoch=12"},
            {"id": "O3", "claim": "tombstones are SQL NULL bodies, not JSON null",
             "evidence": "chains segments/b rev4 body=null and segments/t rev7 body=null; metrics/m5 body contains JSON null"},
            {"id": "O4", "claim": "compaction keeps the baseline version below the floor for every key",
             "evidence": "segments/a rev2 and segments/c rev5 remain with floor=7"},
            {"id": "O5", "claim": "receipts survive compaction while commits at or below the floor do not",
             "evidence": "receipts include seed rev1 while commits are only [8,9,10,11]"},
            {"id": "O6", "claim": "index tokens merge numeric 1 and 1.0 while keeping true separate",
             "evidence": "metrics by_rank has n:1 for m2 and m3, b:1 for m1, and no row for m7/m8/m9"},
            {"id": "O7", "claim": "replication packets carry head, floor and catalog_epoch separately",
             "evidence": "catalog-only packet has through=head=9, no commits, catalog_epoch=12"},
            {"id": "O8", "claim": "replica keeps source identity but cannot write",
             "evidence": "replica meta mode=replica/upstream=<source id>; Database.batch commit raises ReadOnly"},
            {"id": "O9", "claim": "replication does not copy receipts or request keys on applied commits",
             "evidence": "replica receipts=7 versus primary 9; replicated commits 9-11 carry NULL request_key/request_hash, while the restored baseline keeps its original rev8 row"},
            {"id": "O10", "claim": "a cursor is an HMAC-signed payload bound to database_id",
             "evidence": "cursor payload has database/query/revision/after; same token continues on the replica fixture"},
            {"id": "O11", "claim": "backup and restore are byte receipts, not logical fingerprints",
             "evidence": "verified dynamically: receipt sha equals the backup file, a restored file has a different sha"},
        ],
    }


def write_fixtures(destination=FIXTURE_DIR):
    """Regenerate every committed fixture file under *destination*."""
    from .dump import dump_database

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="strataleaf-sample-") as directory:
        root = Path(directory)
        built = build(root)
        dump_database(built["primary_path"], destination / "primary.sql",
                      note="Format-1 primary sample: head=11 floor=7 catalog_epoch=12.")
        dump_database(built["replica_path"], destination / "replica.sql",
                      note="Format-1 replica sample after catch-up: same identity, mode=replica.")
        dump_database(built["backup_path"], destination / "backup-head8.sql",
                      note="Snapshot behind backup.db: head=8 floor=7 catalog_epoch=7, "
                           "pins and consumers still present as at backup time.")
        for name, envelope in built["packets"].items():
            (destination / (name + "-packet.json")).write_text(
                json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8")
        (destination / "cursor.json").write_text(
            json.dumps(built["observed"]["cursor"], ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        (destination / "manifest.json").write_text(
            json.dumps(manifest(built, root), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="write the fixture files under " + str(FIXTURE_DIR))
    parser.add_argument("--destination", default=str(FIXTURE_DIR),
                        help="fixture directory to write")
    arguments = parser.parse_args(argv)
    if not arguments.write:
        parser.print_help()
        return 2
    destination = write_fixtures(arguments.destination)
    print(json.dumps({"written": str(destination), "files": sorted(
        path.name for path in destination.iterdir())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
