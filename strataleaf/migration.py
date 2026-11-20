"""Format identification and the JSON-text -> MessagePack payload migration.

The migration is an offline, restartable rewrite of the three columns that
store logical JSON values (``collections.schema``, ``versions.body`` and
``commits.payload``).  It never invents or drops documents, versions,
tombstones, commits or idempotent receipts: each row is decoded from its own
representation, validated, and re-encoded into the target format.

Crash-safety boundary
---------------------
* Before anything is written, a read snapshot is validated and a logical
  manifest digest is computed.  A database that fails validation is left in
  format 1, byte for byte.
* The first write transaction records a journal row and changes
  ``meta.format`` to ``migrating:1->2``.  From that moment neither the old
  build nor the new build treats the file as a normal database: an interrupted
  file is *identifiable* and can only be resumed.
* Payloads are rewritten in bounded chunks, each chunk committed together with
  the journal counters.  Every rewritten payload carries its own marker
  (SQLITE TEXT vs. a BLOB beginning with ``00 02``), so a partially converted
  database is unambiguous row by row as well.
* Resuming re-validates the manifest digest of what is currently stored, then
  continues with the remaining legacy rows.  Publishing (``meta.format=2``
  and journal deletion) is a single transaction that only runs after the
  complete manifest, idempotent receipts and materialised indexes match.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from .codec import (FORMAT_NAMES, JSON_FORMAT, PACK_FORMAT, digest, encode, field, loads,
                    msgpack_available, payload_kind, render, same_value, scalar_token, validate_json)
from .errors import Conflict, DatabaseError, MigrationError
from .schema import normalize_schema

MIGRATING = "migrating:1->2"
_TABLES = (
    ("collections", "schema"),
    ("versions", "body"),
    ("commits", "payload"),
)
_BLOB_TEST = "typeof({column})='blob' AND substr({column},1,2)=X'0002'"
_JOURNAL_DDL = """
CREATE TABLE IF NOT EXISTS migration (
 id INTEGER PRIMARY KEY CHECK (id=1), source_format TEXT NOT NULL, target_format TEXT NOT NULL,
 state TEXT NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 converted INTEGER NOT NULL, remaining INTEGER NOT NULL, detail TEXT NOT NULL)"""


class MigrationInterrupted(DatabaseError):
    """Raised by the deterministic interruption seam (``interrupt_after``)."""


# --------------------------------------------------------------------------
# Raw connection helpers (the live Database handle must not be used)
# --------------------------------------------------------------------------

def _connect(path, writable=False):
    path = Path(path).resolve()
    if not path.exists():
        raise DatabaseError("database does not exist: " + str(path))
    uri = path.as_uri() + ("" if writable else "?mode=ro")
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    except sqlite3.OperationalError as exc:
        raise DatabaseError("cannot open database: " + str(exc)) from exc
    connection.row_factory = sqlite3.Row
    return connection


def _read_meta(connection):
    try:
        return dict(connection.execute("SELECT key,value FROM meta"))
    except sqlite3.DatabaseError as exc:
        raise DatabaseError("path is not a StrataLeaf database") from exc


def _journal(connection):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration'").fetchone():
        return None
    row = connection.execute("SELECT * FROM migration WHERE id=1").fetchone()
    if row is None:
        return None
    try:
        detail = json.loads(row["detail"])
    except ValueError:
        detail = {}
    return {"source_format": row["source_format"], "target_format": row["target_format"],
            "state": row["state"], "started_at": row["started_at"], "updated_at": row["updated_at"],
            "converted": row["converted"], "remaining": row["remaining"], "detail": detail}


# --------------------------------------------------------------------------
# Identification
# --------------------------------------------------------------------------

def _representations(connection):
    output = {}
    for table, column in _TABLES:
        row = connection.execute(
            f"SELECT SUM(CASE WHEN typeof({column})='text' THEN 1 ELSE 0 END) AS text_rows,"
            f" SUM(CASE WHEN {_BLOB_TEST.format(column=column)} THEN 1 ELSE 0 END) AS pack_rows,"
            f" SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) AS null_rows,"
            f" COUNT(*) AS total FROM {table}").fetchone()
        output[table] = {key: int(row[key] or 0) for key in ("text_rows", "pack_rows", "null_rows", "total")}
    return output


def fingerprint(path):
    """Logical content fingerprint of a database on disk, usable before or after migration.

    Opens the file read-only and computes the same digest as
    ``Database.diagnostics.fingerprint`` at the current head, so an operator can
    compare identities without opening a live handle of the other format.
    """
    connection = _connect(path)
    try:
        values = _read_meta(connection)
        if values.get("format") not in {str(JSON_FORMAT), str(PACK_FORMAT)}:
            raise DatabaseError("database payload format is not a completed format")
        head = int(values["head"])
        _begin_read(connection)
        try:
            content = []
            for collection in connection.execute("SELECT name FROM collections ORDER BY name"):
                for row in connection.execute(
                        """SELECT v.key AS key, v.rev AS rev, v.body AS body, v.expires AS expires FROM versions v JOIN
                           (SELECT key,MAX(rev) AS r FROM versions WHERE collection=? AND rev<=? GROUP BY key) latest
                           ON v.collection=? AND v.key=latest.key AND v.rev=latest.r
                           WHERE v.body IS NOT NULL ORDER BY v.key""",
                        (collection[0], head, collection[0])):
                    document = _load(row["body"], f"fingerprint {collection[0]}/{row['key']}")
                    _validate_version(document, f"fingerprint {collection[0]}/{row['key']}")
                    content.append({"collection": collection[0], "key": row["key"], "revision": row["rev"],
                                    "document": document, "expires_at": row["expires"]})
        finally:
            connection.rollback()
        return {"revision": head, "documents": len(content), "sha256": digest(content)}
    finally:
        connection.close()


def inspect_format(path, counts=False):
    """Describe the payload format of a database or archive on disk.

    Never opens the database as a live handle and never writes.  ``migration``
    is one of ``none``, ``required``, ``in-progress``, ``inconsistent`` or
    ``unsupported``.
    """
    connection = _connect(path)
    try:
        values = _read_meta(connection)
        if "format" not in values:
            raise DatabaseError("path has no format marker")
        raw, journal = values["format"], _journal(connection)
        result = {"path": str(Path(path).resolve()), "raw_format": raw, "format": None,
                  "format_name": "unknown", "migration": "unsupported", "journal": journal,
                  "head": int(values["head"]), "floor": int(values["floor"]),
                  "database_id": values["database_id"], "mode": values["mode"]}
        if raw == str(JSON_FORMAT):
            result.update(format=JSON_FORMAT, format_name=FORMAT_NAMES[JSON_FORMAT], migration="required")
            if journal is not None:
                result["migration"] = "inconsistent"
        elif raw == str(PACK_FORMAT):
            result.update(format=PACK_FORMAT, format_name=FORMAT_NAMES[PACK_FORMAT], migration="none")
            if journal is not None:
                result["migration"] = "inconsistent"
            elif _count_pending(connection):
                result["migration"] = "inconsistent"
        elif raw == MIGRATING:
            result.update(format_name="migrating", migration="in-progress")
        if counts:
            result["representations"] = _representations(connection)
        return result
    except (sqlite3.DatabaseError, KeyError, ValueError) as exc:
        raise DatabaseError("cannot inspect database format") from exc
    finally:
        connection.close()


# --------------------------------------------------------------------------
# Loading and structural validation of individual stored payloads
# --------------------------------------------------------------------------

def _load(payload, where):
    kind = payload_kind(payload)
    try:
        if kind == "json-text":
            value = json.loads(payload)
        elif kind == "msgpack":
            value = loads(payload)
        else:
            raise MigrationError(f"{where}: payload is neither JSON text nor a format-2 MessagePack blob")
        validate_json(value)
        return value
    except MigrationError:
        raise
    except DatabaseError as exc:
        raise MigrationError(f"{where}: cannot decode stored payload: {exc}") from exc
    except (ValueError, TypeError) as exc:
        raise MigrationError(f"{where}: cannot decode stored payload: {exc}") from exc


def _validate_collection(value, where):
    try:
        normalize_schema(value)
    except DatabaseError as exc:
        raise MigrationError(f"{where}: stored schema is not valid: {exc}") from exc


def _validate_version(value, where, tombstone=False):
    if tombstone:
        if value is not None:
            raise MigrationError(f"{where}: tombstone row carries a document")
    elif not isinstance(value, dict):
        raise MigrationError(f"{where}: a live document must be a JSON object")


def _validate_commit(value, revision, where):
    if not isinstance(value, dict) or set(value) != {"revision", "operations"}:
        raise MigrationError(f"{where}: commit payload must contain exactly revision and operations")
    if isinstance(value["revision"], bool) or value["revision"] != revision:
        raise MigrationError(f"{where}: commit revision does not match its row")
    operations = value["operations"]
    if not isinstance(operations, list) or not operations:
        raise MigrationError(f"{where}: commit payload has no operations")
    for operation in operations:
        if (not isinstance(operation, dict)
                or set(operation) != {"collection", "key", "document", "expected", "expires_at"}
                or not isinstance(operation["collection"], str) or not isinstance(operation["key"], str)):
            raise MigrationError(f"{where}: malformed commit operation")
        if operation["document"] is not None:
            _validate_version(operation["document"], where)


def _decode_row(table, row, prefix):
    where = f"{prefix} {table} rowid {row['rid']}"
    if row["payload"] is None:
        _validate_version(None, where, tombstone=True)
        return None
    value = _load(row["payload"], where)
    if table == "collections":
        _validate_collection(value, where)
    elif table == "versions":
        _validate_version(value, where)
    else:
        _validate_commit(value, row["rev"], where)
    return value


def _pending_sql(column):
    return f"{column} IS NOT NULL AND NOT ({_BLOB_TEST.format(column=column)})"


def _count_pending(connection):
    return sum(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {_pending_sql(column)}").fetchone()[0]
               for table, column in _TABLES)


# --------------------------------------------------------------------------
# Logical manifest: identical before and after conversion
# --------------------------------------------------------------------------

def _manifest(connection):
    """Digest of every logical value, tombstone and expiry, independent of codec."""
    hasher = hashlib.sha256()

    def feed(tag, value):
        hasher.update(tag.encode("utf-8", "surrogatepass"))
        hasher.update(b"\x00")
        hasher.update(render(value).encode())
        hasher.update(b"\x1e")

    counts = {"collections": 0, "versions": 0, "tombstones": 0, "commits": 0}
    for row in connection.execute("SELECT name,schema,generation FROM collections ORDER BY name"):
        where = "collection " + row["name"]
        value = _load(row["schema"], where)
        _validate_collection(value, where)
        feed(f"collection:{row['name']}:{row['generation']}", value)
        counts["collections"] += 1
    for row in connection.execute("SELECT collection,key,rev,body,expires FROM versions ORDER BY collection,key,rev"):
        where = f"version {row['collection']}/{row['key']}@{row['rev']}"
        document = _load(row["body"], where) if row["body"] is not None else None
        _validate_version(document, where, tombstone=row["body"] is None)
        counts["versions" if row["body"] is not None else "tombstones"] += 1
        feed(f"version:{row['collection']}:{row['key']}:{row['rev']}:{row['body'] is None}",
             [document, row["expires"]])
    for row in connection.execute("SELECT rev,payload FROM commits ORDER BY rev"):
        payload = _load(row["payload"], f"commit {row['rev']}")
        _validate_commit(payload, row["rev"], f"commit {row['rev']}")
        feed(f"commit:{row['rev']}", payload)
        counts["commits"] += 1
    return hasher.hexdigest(), counts


# --------------------------------------------------------------------------
# Semantic verification performed before publishing
# --------------------------------------------------------------------------

def _verify_receipts(connection):
    verified = compacted = 0
    for row in connection.execute("""SELECT r.request_key,r.request_hash,r.rev,c.payload FROM receipts r
                                     LEFT JOIN commits c ON c.rev=r.rev ORDER BY r.request_key"""):
        if row["payload"] is None:
            compacted += 1   # the commit may legitimately have been reclaimed
            continue
        payload = _load(row["payload"], "receipt commit " + row["request_key"])
        _validate_commit(payload, row["rev"], "receipt commit " + row["request_key"])
        if digest(payload["operations"]) != row["request_hash"]:
            raise MigrationError("idempotent receipt hash no longer matches its commit: " + row["request_key"])
        verified += 1
    return {"verified": verified, "compacted": compacted}


def _verify_indexes(connection, head):
    from .mvcc import visible_rows
    expected = set()
    for definition in connection.execute("SELECT * FROM index_defs ORDER BY collection,name"):
        tokens = set()
        for row in visible_rows(connection, definition["collection"], head):
            document = _load(row["body"], f"index source {row['collection']}/{row['key']}")
            token = scalar_token(field(document, definition["field"]))
            if token is None:
                continue
            if definition["unique_flag"] and token in tokens:
                raise MigrationError("unique index has duplicate source values: " + definition["name"])
            tokens.add(token)
            expected.add((definition["collection"], definition["name"], token, row["key"]))
    actual = {tuple(row) for row in connection.execute("SELECT collection,name,token,key FROM index_values")}
    if expected != actual:
        # Tokens derive from canonical JSON text, so an equality change (int 1 vs
        # float 1.0, true vs 1, or a numeric token drift) surfaces here.
        raise MigrationError("materialised index values differ from the current visible documents")
    return {"definitions": connection.execute("SELECT COUNT(*) FROM index_defs").fetchone()[0],
            "entries": connection.execute("SELECT COUNT(*) FROM index_values").fetchone()[0]}


def _verify_structure(connection):
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MigrationError("payload conversion found broken foreign keys")
    values = _read_meta(connection)
    head, floor = int(values["head"]), int(values["floor"])
    if not 0 <= floor <= head:
        raise MigrationError("invalid revision watermarks")
    revisions = [row[0] for row in connection.execute("SELECT rev FROM commits WHERE rev>? ORDER BY rev", (floor,))]
    if revisions != list(range(floor + 1, head + 1)):
        raise MigrationError("retained commit range is not contiguous")
    latest = connection.execute("SELECT MAX(rev) FROM versions").fetchone()[0]
    if latest is not None and latest > head:
        raise MigrationError("a stored version is newer than the database head")
    for name, revision in connection.execute("SELECT name,rev FROM pins"):
        if not floor <= revision <= head:
            raise MigrationError("read pin outside retained history: " + name)
    for name, ack in connection.execute("SELECT name,ack FROM consumers"):
        if not floor <= ack <= head:
            raise MigrationError("consumer position outside retained history: " + name)
    return {"head": head, "floor": floor}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def migrate(path, chunk_size=250, snapshot=None, interrupt_after=None, dry_run=False):
    """Convert a database from JSON text to MessagePack payloads.

    ``chunk_size`` bounds how many payload rows each committed transaction
    rewrites.  ``interrupt_after`` commits a chunk and then raises
    :class:`MigrationInterrupted`, a deterministic seam for observing that an
    interrupted migration is identifiable and resumable.  ``snapshot`` takes a
    verified pre-migration SQLite copy at that path before the first write, so
    an abandoned migration has an explicit, intact way back.  ``dry_run``
    performs the full read-only validation (manifest, receipts, indexes and
    structure) and reports what *would* be converted without changing a byte.

    Returns a JSON-ready receipt describing what was converted and verified.
    """
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or not 1 <= chunk_size <= 10000:
        raise MigrationError("chunk_size must be between 1 and 10000")
    if interrupt_after is not None and (isinstance(interrupt_after, bool)
                                        or not isinstance(interrupt_after, int) or interrupt_after < 0):
        raise MigrationError("interrupt_after must be a nonnegative integer")
    path = Path(path).resolve()
    if not path.exists():
        raise DatabaseError("database does not exist: " + str(path))
    if not msgpack_available():
        raise MigrationError("payload migration requires the msgpack 1.2.4 runtime dependency")

    connection = _connect(path, writable=not dry_run)
    try:
        if dry_run:
            return _dry_run(connection, path)
        raw, journal = _read_meta(connection).get("format"), _journal(connection)
        if raw == str(PACK_FORMAT):
            if journal is not None:
                raise MigrationError("database claims format 2 but still has a migration journal")
            if _count_pending(connection):
                raise MigrationError("database claims format 2 but contains legacy JSON payloads")
            return {"path": str(path), "state": "current", "format": PACK_FORMAT,
                    "format_name": FORMAT_NAMES[PACK_FORMAT], "converted": 0, "snapshot": None}
        if raw == str(JSON_FORMAT):
            if journal is not None:
                raise MigrationError("format 1 database has a migration journal; inspect it before retrying")
            saved = _snapshot(connection, path, snapshot) if snapshot is not None else None
            return _start(connection, path, chunk_size, interrupt_after, saved)
        if raw == MIGRATING:
            if journal is None:
                raise MigrationError("database is marked as migrating but has no journal; "
                                     "restore a pre-migration snapshot or archive")
            if snapshot is not None:
                raise Conflict("migration already started; a fresh pre-migration snapshot is no longer possible")
            return _resume(connection, path, chunk_size, interrupt_after, journal)
        raise DatabaseError("unsupported on-disk format: " + str(raw))
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise MigrationError("another connection holds a write lock; close open handles before migrating") from exc
        raise
    finally:
        connection.close()


def _dry_run(connection, path):
    raw = _read_meta(connection).get("format")
    report = {"path": str(path), "dry_run": True, "raw_format": raw, "changed": False}
    if raw not in {str(JSON_FORMAT), MIGRATING}:
        report.update(state="current" if raw == str(PACK_FORMAT) else "unsupported", format=raw)
        return report
    _begin_read(connection)
    try:
        digest_before, counts = _manifest(connection)
        structure = _verify_structure(connection)
        receipts = _verify_receipts(connection)
        indexes = _verify_indexes(connection, structure["head"])
        pending = _count_pending(connection)
    finally:
        connection.rollback()
    report.update(state="ready" if raw == str(JSON_FORMAT) else "resumable",
                  manifest=counts, receipts=receipts, indexes=indexes, structure=structure,
                  pending=pending, manifest_sha256=digest_before,
                  representations=_representations(connection))
    return report


def _begin_read(connection):
    try:
        connection.execute("BEGIN")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise MigrationError("another connection holds a write lock; retry the inspection") from exc
        raise


def _snapshot(connection, path, destination):
    destination = Path(destination).resolve()
    if destination == path:
        raise Conflict("snapshot must use a different path than the database")
    if destination.exists():
        raise Conflict("snapshot destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".strataleaf-snapshot-", dir=destination.parent)
    os.close(fd)
    try:
        target = sqlite3.connect(temporary)
        try:
            connection.backup(target)
        finally:
            target.close()
        from .archive import inspect_archive
        receipt = inspect_archive(temporary)
        if receipt["format"] != JSON_FORMAT:
            raise MigrationError("pre-migration snapshot is not a legacy archive")
        check = _connect(temporary)
        try:
            _begin_read(check)
            try:
                snapshot_manifest, _ = _manifest(check)
            finally:
                check.rollback()
        finally:
            check.close()
        os.link(temporary, destination)
        return {"path": str(destination), "sha256": receipt["sha256"],
                "revision": receipt["revision"], "format": receipt["format"],
                "manifest_sha256": snapshot_manifest}
    except FileExistsError as exc:
        raise Conflict("snapshot destination appeared concurrently") from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


def _begin(connection):
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise MigrationError("another connection holds a write lock; close open handles before migrating") from exc
        raise


def _start(connection, path, chunk_size, interrupt_after, saved_snapshot):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _begin(connection)
    try:
        # Preflight inside the write lock: no writer can slip in between the
        # manifest and the marker, and no bytes change if validation fails.
        digest_before, counts = _manifest(connection)
        structure = _verify_structure(connection)
        receipts = _verify_receipts(connection)
        indexes = _verify_indexes(connection, structure["head"])
        pending = _count_pending(connection)
        if saved_snapshot is not None and saved_snapshot["manifest_sha256"] != digest_before:
            raise MigrationError("pre-migration snapshot does not match the live database; "
                                 "it was not taken from this state")
        detail = {"source_digest": digest_before, "counts": counts, "receipts": receipts,
                  "indexes": indexes, "structure": structure, "snapshot": saved_snapshot}
        connection.execute(_JOURNAL_DDL)
        connection.execute(
            "INSERT INTO migration VALUES (1,?,?,?,?,?,?,?,?)",
            (str(JSON_FORMAT), str(PACK_FORMAT), "converting", now, now, 0, pending,
             json.dumps(detail, ensure_ascii=False, sort_keys=True)))
        connection.execute("UPDATE meta SET value=? WHERE key='format'", (MIGRATING,))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    result = _convert(connection, path, chunk_size, interrupt_after)
    result.update({"manifest": counts, "receipts": receipts, "indexes": indexes,
                   "structure": structure, "snapshot": saved_snapshot})
    return result


def _resume(connection, path, chunk_size, interrupt_after, journal):
    if journal["source_format"] != str(JSON_FORMAT) or journal["target_format"] != str(PACK_FORMAT):
        raise MigrationError("migration journal points at an unsupported conversion")
    if journal["state"] not in {"converting", "verified"}:
        raise MigrationError("migration journal state is not resumable: " + journal["state"])
    expected = journal["detail"].get("source_digest")
    if not isinstance(expected, str):
        raise MigrationError("migration journal has no source manifest; manual inspection required")
    result = _convert(connection, path, chunk_size, interrupt_after)
    detail = journal["detail"]
    result.update({"manifest": detail.get("counts"), "receipts": detail.get("receipts"),
                   "indexes": detail.get("indexes"), "structure": detail.get("structure"),
                   "snapshot": detail.get("snapshot")})
    return result


def _convert(connection, path, chunk_size, interrupt_after):
    converted_this_run = 0
    while True:
        _begin(connection)
        try:
            journal = _journal(connection)
            if journal is None:
                # Another process may have completed and published this migration.
                published = _read_meta(connection).get("format") == str(PACK_FORMAT)
                connection.rollback()
                if published and _count_pending(connection) == 0:
                    return {"path": str(path), "state": "migrated", "format": PACK_FORMAT,
                            "format_name": FORMAT_NAMES[PACK_FORMAT], "converted": converted_this_run,
                            "concurrent": True}
                raise MigrationError("migration journal disappeared during conversion")
            if journal["state"] == "verified":
                connection.rollback()
                break
            if journal["state"] != "converting":
                connection.rollback()
                raise MigrationError("migration journal state is not resumable: " + journal["state"])
            total_before = journal["converted"] + journal["remaining"]
            rows_done = 0
            for table, column in _TABLES:
                if rows_done >= chunk_size:
                    break
                limit = chunk_size - rows_done
                if table == "commits":
                    rows = connection.execute(
                        f"SELECT rowid AS rid,rev,{column} AS payload FROM {table} "
                        f"WHERE {_pending_sql(column)} ORDER BY rowid LIMIT ?", (limit,)).fetchall()
                else:
                    rows = connection.execute(
                        f"SELECT rowid AS rid,{column} AS payload FROM {table} "
                        f"WHERE {_pending_sql(column)} ORDER BY rowid LIMIT ?", (limit,)).fetchall()
                for row in rows:
                    value = _decode_row(table, row, "converting")
                    encoded = encode(value)
                    # Round-trip the new payload before committing it: a value that
                    # fails to decode back to an identical logical value is never stored.
                    if not same_value(loads(encoded), value):
                        raise MigrationError(f"converting {table} rowid {row['rid']}: "
                                             "MessagePack round trip changed the logical value")
                    connection.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?",
                                       (encoded, row["rid"]))
                    rows_done += 1
            remaining = _count_pending(connection)
            if journal["converted"] + rows_done + remaining != total_before:
                raise MigrationError("legacy payload count changed during migration")
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            connection.execute("UPDATE migration SET converted=converted+?,remaining=?,updated_at=? WHERE id=1",
                               (rows_done, remaining, now))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        converted_this_run += rows_done
        if interrupt_after is not None and converted_this_run >= interrupt_after:
            raise MigrationInterrupted(
                f"migration paused after {converted_this_run} converted payloads; "
                "resume with strataleaf.migrate(path)")
        if rows_done == 0:
            break
    return _publish(connection, path, converted_this_run)


def _publish(connection, path, converted_this_run):
    _begin(connection)
    try:
        journal = _journal(connection)
        if journal is None:
            if _read_meta(connection).get("format") == str(PACK_FORMAT):
                connection.rollback()
                return {"path": str(path), "state": "migrated", "format": PACK_FORMAT,
                        "format_name": FORMAT_NAMES[PACK_FORMAT], "converted": converted_this_run,
                        "concurrent": True}
            raise MigrationError("migration journal disappeared before publishing")
        pending = _count_pending(connection)
        if pending:
            raise MigrationError(f"{pending} legacy payloads remain; refusing to publish")
        digest_after, counts = _manifest(connection)
        if digest_after != journal["detail"].get("source_digest"):
            raise MigrationError("logical manifest changed during payload conversion")
        structure = _verify_structure(connection)
        receipts = _verify_receipts(connection)
        indexes = _verify_indexes(connection, structure["head"])
        connection.execute("UPDATE meta SET value=? WHERE key='format'", (str(PACK_FORMAT),))
        connection.execute("DELETE FROM migration")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    try:
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.DatabaseError:
        pass
    return {"path": str(path), "state": "migrated", "format": PACK_FORMAT,
            "format_name": FORMAT_NAMES[PACK_FORMAT], "converted": converted_this_run,
            "manifest": counts, "receipts": receipts, "indexes": indexes, "structure": structure}
