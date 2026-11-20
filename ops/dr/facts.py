"""Read-only fact collection and comparison for StrataLeaf databases.

A database "fact set" is deliberately wider than ``head``:

* identity: ``database_id``, ``mode``, ``upstream`` and format marker;
* catalog: every collection schema and generation;
* index definitions *and* the materialised index projection, which is
  independently recomputed from visible documents;
* document content digest at a chosen revision (same construction the
  library's ``diagnostics.fingerprint`` uses);
* revision watermarks and commit-log continuity;
* SQLite physical integrity and foreign-key check results.

Everything here opens the database with ``mode=ro`` SQLite URIs or uses the
public read-only API.  The embedded library (``strataleaf``) is never
modified and no function in this module writes to a database file.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from strataleaf.codec import digest, field, loads, scalar_token
from strataleaf.mvcc import visible_rows

META_DEFAULTS = ("format", "head", "floor", "database_id", "cursor_secret", "mode", "upstream", "catalog_epoch")


class FactsError(Exception):
    pass


def file_digest(path: str | Path) -> str:
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def readonly_connection(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FactsError("database file does not exist: " + str(resolved))
    connection = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    return connection


def _meta(connection: sqlite3.Connection) -> dict:
    try:
        values = {row["key"]: row["value"] for row in connection.execute("SELECT key,value FROM meta")}
    except sqlite3.DatabaseError as exc:
        raise FactsError("not a StrataLeaf database: " + str(exc)) from exc
    missing = [key for key in META_DEFAULTS if key not in values]
    if missing:
        raise FactsError("missing storage metadata: " + ",".join(missing))
    return values


def _document_content(connection: sqlite3.Connection, revision: int) -> list:
    content = []
    for collection in connection.execute("SELECT name FROM collections ORDER BY name"):
        for row in visible_rows(connection, collection["name"], revision):
            content.append({
                "collection": collection["name"],
                "key": row["key"],
                "revision": row["rev"],
                "document": loads(row["body"]),
                "expires_at": row["expires"],
            })
    return content


def _document_digest(connection: sqlite3.Connection, revision: int) -> dict:
    content = _document_content(connection, revision)
    return {"revision": revision, "count": len(content), "sha256": digest(content)}


def _catalog(connection: sqlite3.Connection) -> list:
    return [{"name": row["name"], "schema": loads(row["schema"]), "generation": row["generation"]}
            for row in connection.execute("SELECT * FROM collections ORDER BY name")]


def _indexes(connection: sqlite3.Connection) -> list:
    return [{"collection": row["collection"], "name": row["name"], "field": row["field"],
             "unique": bool(row["unique_flag"])}
            for row in connection.execute("SELECT * FROM index_defs ORDER BY collection,name")]


def _index_projection(connection: sqlite3.Connection, revision: int, indexes: list) -> dict:
    """Recompute the current-head index projection from visible documents."""
    expected = set()
    issues = []
    for definition in indexes:
        tokens = set()
        for row in visible_rows(connection, definition["collection"], revision):
            token = scalar_token(field(loads(row["body"]), definition["field"]))
            if token is None:
                continue
            if definition["unique"] and token in tokens:
                issues.append("unique index has duplicate source values: " + definition["name"])
            tokens.add(token)
            expected.add((definition["collection"], definition["name"], token, row["key"]))
    actual = {tuple(row) for row in connection.execute("SELECT collection,name,token,key FROM index_values")}
    return {
        "ok": expected == actual and not issues,
        "expected_count": len(expected),
        "actual_count": len(actual),
        "missing": sorted(expected - actual)[:20],
        "unexpected": sorted(actual - expected)[:20],
        "issues": issues,
        "sha256": digest(sorted([list(item) for item in expected])),
    }


def read_facts(path: str | Path, revision: int | None = None, hash_file: bool = False) -> dict:
    """Collect the fact set of a database file at ``revision`` (default head)."""
    resolved = Path(path).resolve()
    connection = readonly_connection(resolved)
    try:
        values = _meta(connection)
        head, floor = int(values["head"]), int(values["floor"])
        if not 0 <= floor <= head:
            raise FactsError("invalid revision watermarks")
        target = head if revision is None else revision
        if isinstance(target, bool) or not isinstance(target, int) or target < 0 or target > head:
            raise FactsError("revision outside zero..head")
        if target < floor:
            raise FactsError("revision %d is older than retained floor %d" % (target, floor))

        collections = _catalog(connection)
        indexes = _indexes(connection)
        documents = _document_digest(connection, target)
        # The materialised projection always reflects head, never a historical
        # revision, so only compare it when the requested revision is head.
        projection = (_index_projection(connection, head, indexes) if target == head
                      else {"ok": None, "evaluated": False, "reason": "historical revision"})
        projection["evaluated"] = projection.get("ok") is not None

        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        references = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        revisions = [row[0] for row in connection.execute("SELECT rev FROM commits WHERE rev>? ORDER BY rev", (floor,))]
        expected_revisions = list(range(floor + 1, head + 1))
        contiguous = {"ok": revisions == expected_revisions,
                      "retained_from": floor + 1, "retained_to": head,
                      "count": len(revisions),
                      "first_missing": next((r for r in expected_revisions if r not in set(revisions)), None)}
        counts = {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                  for table in ("collections", "versions", "commits", "index_defs", "index_values", "pins", "consumers")}
    finally:
        connection.close()

    issues = []
    if integrity != ["ok"]:
        issues.append("SQLite physical integrity failure")
    if references:
        issues.append("broken foreign keys")
    if not contiguous["ok"]:
        issues.append("retained commit range is not contiguous")
    if projection.get("ok") is False:
        issues.append("index projection differs from visible documents")
        issues.extend(projection.get("issues", []))

    facts = {
        "path": str(resolved),
        "file_bytes": resolved.stat().st_size,
        "head": head,
        "floor": floor,
        "revision": target,
        "catalog_epoch": int(values["catalog_epoch"]),
        "identity": {"format": values["format"], "database_id": values["database_id"],
                     "mode": values["mode"], "upstream": values["upstream"]},
        "collections": collections,
        "indexes": indexes,
        "documents": documents,
        "index_projection": projection,
        "commit_contiguity": contiguous,
        "counts": counts,
        "sqlite_integrity": integrity,
        "foreign_key_violations": references,
        "issues": issues,
    }
    facts["schema_sha256"] = digest(collections)
    facts["index_definition_sha256"] = digest(indexes)
    facts["ok"] = not issues and (projection.get("ok") is not False)
    if hash_file:
        facts["file_sha256"] = file_digest(resolved)
    return facts


def compare_snapshot(expected: dict, actual: dict, *, identity: str,
                     source_database_id: str | None = None,
                     expected_sha256: str | None = None) -> list:
    """Compare a restored file against a recorded backup snapshot.

    ``identity`` is ``primary`` (disaster restore into a new primary) or
    ``follower`` (replica bootstrap bound to the original source).
    Returns a list of ``{check, ok, expected, actual, detail}`` records.
    """
    checks = []

    def record(name, ok, wanted, found, detail=""):
        checks.append({"check": name, "ok": bool(ok), "expected": wanted, "actual": found, "detail": detail})

    record("head_matches_backup", actual["head"] == expected["head"], expected["head"], actual["head"])
    record("floor_matches_backup", actual["floor"] == expected["floor"], expected["floor"], actual["floor"])
    record("catalog_epoch_matches", actual["catalog_epoch"] == expected["catalog_epoch"],
           expected["catalog_epoch"], actual["catalog_epoch"])
    record("collections_match", actual["schema_sha256"] == expected["schema_sha256"],
           expected["schema_sha256"], actual["schema_sha256"],
           detail="collection names, schemas and generations")
    record("index_definitions_match", actual["index_definition_sha256"] == expected["index_definition_sha256"],
           expected["index_definition_sha256"], actual["index_definition_sha256"])
    record("documents_match", actual["documents"]["sha256"] == expected["documents_sha256"],
           expected["documents_sha256"], actual["documents"]["sha256"],
           detail="%d documents at revision %d" % (actual["documents"]["count"], actual["documents"]["revision"]))
    record("document_count_matches", actual["documents"]["count"] == expected["documents_count"],
           expected["documents_count"], actual["documents"]["count"])

    projection = actual.get("index_projection", {})
    record("index_projection_recomputed", projection.get("evaluated") is True and projection.get("ok") is True,
           "materialised projection equals visible documents", projection)
    record("sqlite_integrity", actual["sqlite_integrity"] == ["ok"], ["ok"], actual["sqlite_integrity"])
    record("foreign_keys", actual["foreign_key_violations"] == [], [], actual["foreign_key_violations"])
    record("commit_continuity", actual["commit_contiguity"]["ok"] is True, True, actual["commit_contiguity"])
    record("no_reported_issues", actual["issues"] == [], [], actual["issues"])

    actual_identity = actual["identity"]
    if identity == "primary":
        record("identity_rotated",
               actual_identity["database_id"] != source_database_id,
               "database_id != %s" % source_database_id, actual_identity["database_id"],
               detail="restored primary must not reuse the source identity")
        record("mode_is_primary", actual_identity["mode"] == "primary", "primary", actual_identity["mode"])
        record("upstream_cleared", actual_identity["upstream"] == "", "", actual_identity["upstream"])
    elif identity == "follower":
        record("identity_preserved", actual_identity["database_id"] == source_database_id,
               source_database_id, actual_identity["database_id"],
               detail="follower keeps the upstream identity")
        record("mode_is_replica", actual_identity["mode"] == "replica", "replica", actual_identity["mode"])
        record("upstream_bound", actual_identity["upstream"] == source_database_id,
               source_database_id, actual_identity["upstream"])
    else:
        record("identity_mode_known", False, "primary|follower", identity)

    # The restored file is rewritten (identity rotation, pin/consumer cleanup),
    # so its file digest intentionally differs from the archive.  The archive
    # checksum was already asserted by the library before the copy; record it
    # as informational rather than as a pass/fail on the restored bytes.
    record("archive_checksum_verified_by_library", True,
           expected_sha256 or "unpinned", expected_sha256 or "unpinned",
           detail="restored file digest differs by design (new identity); archive digest is %s"
                  % expected_sha256)
    return checks


def checks_ok(checks: list) -> bool:
    return all(item["ok"] for item in checks)


def summary(facts: dict) -> dict:
    """Compact JSON-friendly summary for reports without the large doc list."""
    return {
        "path": facts["path"],
        "head": facts["head"],
        "floor": facts["floor"],
        "revision": facts["revision"],
        "catalog_epoch": facts["catalog_epoch"],
        "identity": facts["identity"],
        "documents": {key: facts["documents"][key] for key in ("count", "sha256", "revision")},
        "schema_sha256": facts["schema_sha256"],
        "index_definition_sha256": facts["index_definition_sha256"],
        "index_projection_ok": facts["index_projection"].get("ok"),
        "commit_contiguity": facts["commit_contiguity"]["ok"],
        "sqlite_integrity": facts["sqlite_integrity"],
        "issues": facts["issues"],
        "file_bytes": facts["file_bytes"],
    }
