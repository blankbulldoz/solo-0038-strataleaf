import json
import os
import sqlite3
from pathlib import Path
from .archive import Archives, inspect_archive
from .codec import digest, dumps, loads
from .diagnostics import Diagnostics
from .errors import Conflict, DatabaseError, NotFound, ReplicationError
from .indexes import refresh_document
from .mvcc import checked_revision
from .storage import Storage

STAGED_SUFFIX = ".rebaseline"
JOURNAL_SUFFIX = ".rebaseline.json"


def staged_path(path):
    return Path(str(path) + STAGED_SUFFIX)


def journal_path(path):
    return Path(str(path) + JOURNAL_SUFFIX)


def _write_journal(path, payload):
    target = journal_path(path)
    temporary = target.with_name(target.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    try:
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _read_journal(path):
    target = journal_path(path)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReplicationError("rebaseline journal is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("format") != 1 or payload.get("state") not in ("preparing", "switching"):
        raise ReplicationError("rebaseline journal is not supported")
    return payload


def _remove_sidecars(base):
    for suffix in ("-wal", "-shm"):
        Path(str(base) + suffix).unlink(missing_ok=True)


def _checkpoint_file(path):
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def recover_rebaseline(path):
    """Finish a committed generation switch after a crash or reopen.

    A journal in state "switching" is the commit point: the staged baseline is
    promoted if the rename still has to happen, then the journal is cleared.
    State "preparing" never touches the serving file and is left resumable.
    """
    journal = _read_journal(path)
    if journal is None:
        return None
    if journal["state"] != "switching":
        return {"recovered": "preparing", "generation": journal["generation"]}
    staged = staged_path(path)
    if staged.exists():
        _checkpoint_file(staged)
        _remove_sidecars(staged)
        os.replace(staged, str(path))
    journal_path(path).unlink(missing_ok=True)
    return {"recovered": "switched", "generation": journal["generation"]}


def _canonical(rows):
    return sorted(dumps(row) for row in rows)


class Replication:
    def __init__(self, store):
        self.store = store

    def export(self, since, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ReplicationError("replication limit must be between 1 and 1000 commits")
        with self.store.read() as connection:
            since = checked_revision(self.store, since)
            commits = [loads(row[0]) for row in connection.execute("SELECT payload FROM commits WHERE rev>? ORDER BY rev LIMIT ?", (since, limit))]
            body = {"format": 1, "source": self.store.get("database_id"), "since": since,
                "through": commits[-1]["revision"] if commits else since,
                "catalog_epoch": int(self.store.get("catalog_epoch")), "head": int(self.store.get("head")), "floor": int(self.store.get("floor")),
                "collections": [dict(row) for row in connection.execute("SELECT * FROM collections ORDER BY name")],
                "indexes": [dict(row) for row in connection.execute("SELECT * FROM index_defs ORDER BY collection,name")],
                "commits": commits}
            return {"body": body, "sha256": digest(body)}

    def apply(self, envelope):
        try:
            body = envelope["body"]
            if envelope["sha256"] != digest(body) or body["format"] != 1:
                raise ReplicationError("replication envelope checksum/format mismatch")
            commits = body["commits"]
            expected = list(range(body["since"] + 1, body["through"] + 1))
            if [item["revision"] for item in commits] != expected or body["through"] > body["head"]:
                raise ReplicationError("replication envelope has a revision gap")
            with self.store.atomic() as connection:
                if self.store.get("mode") != "replica" or self.store.get("upstream") != body["source"]:
                    raise ReplicationError("envelope does not match configured upstream")
                head = int(self.store.get("head"))
                if body["since"] > head:
                    raise ReplicationError("missing preceding replication range")
                if body["through"] < head or (body["through"] == head and body["catalog_epoch"] <= int(self.store.get("catalog_epoch"))):
                    return {"revision": head, "applied": 0}
                for definition in body["collections"]:
                    connection.execute("INSERT INTO collections VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET schema=excluded.schema,generation=excluded.generation",
                                       (definition["name"], definition["schema"], definition["generation"]))
                connection.execute("DELETE FROM index_values")
                connection.execute("DELETE FROM index_defs")
                for definition in body["indexes"] if body["through"] == body["head"] else []:
                    connection.execute("INSERT INTO index_defs VALUES (?,?,?,?)", (definition["collection"], definition["name"], definition["field"], definition["unique_flag"]))
                applied = 0
                for commit in commits:
                    revision = commit["revision"]
                    if revision <= head:
                        continue
                    for operation in commit["operations"]:
                        document = operation["document"]
                        connection.execute("INSERT INTO versions VALUES (?,?,?,?,?)", (operation["collection"], operation["key"], revision,
                            dumps(document) if document is not None else None, operation["expires_at"]))
                    connection.execute("INSERT INTO commits(rev,payload) VALUES (?,?)", (revision, dumps(commit)))
                    applied += 1
                self.store.set("head", body["through"])
                self.store.set("catalog_epoch", body["catalog_epoch"] if body["through"] == body["head"] else -1)
                from .mvcc import visible_rows
                for collection in connection.execute("SELECT name FROM collections").fetchall():
                    for row in visible_rows(connection, collection[0], body["through"]):
                        refresh_document(connection, collection[0], row["key"], loads(row["body"]))
                return {"revision": body["through"], "applied": applied}
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplicationError("malformed replication envelope") from exc

    def verification(self, revision=None):
        """Describe this database at a revision so a staged replica can be checked before a switch."""
        with self.store.read() as connection:
            revision = checked_revision(self.store, revision)
            body = {"format": 1, "source": self.store.get("database_id"), "revision": revision,
                    "catalog_epoch": int(self.store.get("catalog_epoch")),
                    "collections": [dict(row) for row in connection.execute("SELECT * FROM collections ORDER BY name")],
                    "indexes": [dict(row) for row in connection.execute("SELECT * FROM index_defs ORDER BY collection,name")]}
        fingerprint = Diagnostics(self.store).fingerprint(revision)
        return {**body, "documents": fingerprint["documents"], "fingerprint": fingerprint["sha256"]}

    def rebaseline_prepare(self, source, expected_sha256=None):
        """Stage the next replica generation from an upstream backup while this replica keeps serving."""
        store = self.store
        with store.lock:
            if store.closed:
                raise DatabaseError("database handle is closed")
            if store.get("mode") != "replica":
                raise ReplicationError("only a replica can prepare a rebaseline")
            journal = self._steady_journal()
            staged = staged_path(store.path)
            if journal is not None and staged.exists():
                raise Conflict("rebaseline already in progress")
            if journal is None and staged.exists():
                raise Conflict("staged baseline exists without a journal; call rebaseline_abort")
            upstream = store.get("upstream")
            receipt = inspect_archive(source)
            if receipt["database_id"] != upstream:
                raise ReplicationError("backup does not come from the configured upstream")
            generation = int(store.get("replica_generation")) + 1
            if journal is None:
                _write_journal(store.path, {"format": 1, "state": "preparing", "generation": generation,
                                            "upstream": upstream, "base_revision": None})
            receipt = Archives.restore(source, staged, expected_sha256, follower=True)
            staged_store = Storage(staged)
            try:
                with staged_store.atomic():
                    staged_store.set("replica_generation", generation)
            finally:
                staged_store.close()
            _remove_sidecars(staged)
            _write_journal(store.path, {"format": 1, "state": "preparing", "generation": generation,
                                        "upstream": upstream, "base_revision": receipt["revision"]})
            return {"generation": generation, "base_revision": receipt["revision"],
                    "serving_head": int(store.get("head")), "staged": str(staged)}

    def rebaseline_apply(self, envelope):
        """Apply one upstream envelope to the staged generation; the serving generation is untouched."""
        store = self.store
        with store.lock:
            if store.closed:
                raise DatabaseError("database handle is closed")
            journal = self._steady_journal()
            if journal is None:
                raise NotFound("no rebaseline in progress")
            staged = staged_path(store.path)
            if not staged.exists():
                raise ReplicationError("staged baseline is missing; call rebaseline_prepare again")
            staged_store = Storage(staged)
            try:
                result = Replication(staged_store).apply(envelope)
            finally:
                staged_store.close()
            _remove_sidecars(staged)
            _write_journal(store.path, {**journal, "staged_head": result["revision"]})
            return {**result, "generation": journal["generation"], "staged_head": result["revision"]}

    def rebaseline_switch(self, verification):
        """Promote the staged generation once documents, catalog and upstream relations check out.

        The serving handle stays readable afterwards but is retired for writes;
        reopening the database path lands on the new generation.
        """
        store = self.store
        with store.lock:
            if store.closed:
                raise DatabaseError("database handle is closed")
            journal = self._steady_journal()
            if journal is None:
                raise NotFound("no prepared rebaseline to switch")
            if store.get("mode") != "replica":
                raise ReplicationError("only a replica can switch generations")
            staged = staged_path(store.path)
            if not staged.exists():
                raise ReplicationError("staged baseline is missing; call rebaseline_prepare again")
            try:
                expected = {"source": verification["source"], "revision": verification["revision"],
                            "catalog_epoch": verification["catalog_epoch"], "documents": verification["documents"],
                            "fingerprint": verification["fingerprint"], "collections": verification["collections"],
                            "indexes": verification["indexes"]}
            except (KeyError, TypeError) as exc:
                raise ReplicationError("malformed rebaseline verification") from exc
            upstream = store.get("upstream")
            serving_head = int(store.get("head"))
            serving_generation = int(store.get("replica_generation"))
            if expected["source"] != upstream:
                raise ReplicationError("verification does not come from the configured upstream")
            if journal["generation"] != serving_generation + 1:
                raise ReplicationError("staged baseline generation does not follow the serving generation")
            staged_store = Storage(staged)
            try:
                if (staged_store.get("mode") != "replica" or staged_store.get("upstream") != upstream
                        or staged_store.get("database_id") != upstream):
                    raise ReplicationError("staged baseline upstream relation mismatch")
                if int(staged_store.get("replica_generation")) != journal["generation"]:
                    raise ReplicationError("staged baseline generation mismatch")
                staged_head = int(staged_store.get("head"))
                if staged_head != expected["revision"]:
                    raise ReplicationError("staged baseline is not at the verified revision; catch up and verify again")
                if staged_head < serving_head:
                    raise ReplicationError("staged baseline would move the replica backwards")
                if int(staged_store.get("catalog_epoch")) != expected["catalog_epoch"]:
                    raise ReplicationError("staged catalog epoch does not match verification")
                collections = [dict(row) for row in staged_store.connection.execute("SELECT * FROM collections ORDER BY name")]
                indexes = [dict(row) for row in staged_store.connection.execute("SELECT * FROM index_defs ORDER BY collection,name")]
                if _canonical(collections) != _canonical(expected["collections"]) or _canonical(indexes) != _canonical(expected["indexes"]):
                    raise ReplicationError("staged catalog does not match verification")
                fingerprint = Diagnostics(staged_store).fingerprint(staged_head)
                if fingerprint["sha256"] != expected["fingerprint"] or fingerprint["documents"] != expected["documents"]:
                    raise ReplicationError("staged documents do not match verification")
                inspection = Diagnostics(staged_store).inspect(deep=True)
                if not inspection["ok"]:
                    raise ReplicationError("staged baseline failed inspection: " + "; ".join(inspection["issues"]))
                staged_floor = int(staged_store.get("floor"))
                carried, dropped = [], []
                with staged_store.atomic() as connection:
                    for row in store.connection.execute("SELECT name,ack FROM consumers ORDER BY name"):
                        if row["ack"] < staged_floor:
                            dropped.append(row["name"])
                            continue
                        connection.execute("INSERT OR IGNORE INTO consumers VALUES (?,?)", (row["name"], row["ack"]))
                        carried.append(row["name"])
                staged_store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                staged_store.close()
            _remove_sidecars(staged)
            busy = store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            if busy:
                raise Conflict("serving replica has active readers; retry the switch when quiescent")
            store.retired = True
            _write_journal(store.path, {"format": 1, "state": "switching", "generation": journal["generation"],
                                        "upstream": upstream})
            os.replace(staged, store.path)
            journal_path(store.path).unlink(missing_ok=True)
            return {"switched": True, "generation": journal["generation"], "previous_generation": serving_generation,
                    "revision": staged_head, "consumers_carried": carried, "consumers_dropped": dropped}

    def rebaseline_abort(self):
        """Discard a staged generation; the serving generation is never modified."""
        store = self.store
        with store.lock:
            if store.closed:
                raise DatabaseError("database handle is closed")
            journal = _read_journal(store.path)
            staged = staged_path(store.path)
            if journal is not None and journal["state"] == "switching":
                recover_rebaseline(store.path)
                raise Conflict("rebaseline switch was already committed; reopen the database")
            if journal is None and not staged.exists():
                raise NotFound("no rebaseline in progress")
            generation = journal["generation"] if journal else None
            _remove_sidecars(staged)
            staged.unlink(missing_ok=True)
            journal_path(store.path).unlink(missing_ok=True)
            return {"aborted": generation, "serving_head": int(store.get("head"))}

    def rebaseline_status(self):
        """Report the serving generation and any staged generation, recovering a crashed switch first."""
        store = self.store
        with store.lock:
            if store.closed:
                raise DatabaseError("database handle is closed")
            recover_rebaseline(store.path)
            journal = _read_journal(store.path)
            status = {"mode": store.get("mode"), "upstream": store.get("upstream"),
                      "generation": int(store.get("replica_generation")),
                      "head": int(store.get("head")), "floor": int(store.get("floor")),
                      "retired": store.retired, "rebaseline": None}
            staged = staged_path(store.path)
            if journal is not None:
                pending = {"state": journal["state"], "generation": journal["generation"],
                           "base_revision": journal.get("base_revision"), "staged_exists": staged.exists()}
                if journal["state"] == "preparing" and staged.exists():
                    staged_store = Storage(staged)
                    try:
                        pending["staged_head"] = int(staged_store.get("head"))
                    finally:
                        staged_store.close()
                    _remove_sidecars(staged)
                status["rebaseline"] = pending
            elif staged.exists():
                status["rebaseline"] = {"state": "orphaned", "staged_exists": True}
            return status

    def _steady_journal(self):
        journal = _read_journal(self.store.path)
        if journal is not None and journal["state"] == "switching":
            recover_rebaseline(self.store.path)
            raise Conflict("rebaseline switch was already committed; reopen the database")
        return journal
