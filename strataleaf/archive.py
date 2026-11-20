import hashlib
import os
import sqlite3
import tempfile
import secrets
import uuid
from pathlib import Path
from .codec import FORMAT_NAMES, JSON_FORMAT, PACK_FORMAT
from .errors import Conflict, DatabaseError


def file_digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def inspect_archive(path):
    """Verify and describe an archive without opening it as a live database.

    Returns the payload ``format`` and its ``format_name`` so an operator can
    tell a legacy JSON-text archive from a MessagePack one before choosing a
    restore path.  Legacy archives restored as a primary database keep format 1
    and are migrated on the next open through :func:`strataleaf.migrate`.
    """
    connection = _connect(path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        values = dict(connection.execute("SELECT key,value FROM meta"))
        required = {"head", "floor", "format", "database_id", "cursor_secret", "mode", "upstream"}
        if result != "ok" or not required.issubset(values):
            raise DatabaseError("archive is not a supported intact database")
        if values["format"] not in {str(JSON_FORMAT), str(PACK_FORMAT)}:
            raise DatabaseError("archive carries an unsupported payload format")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DatabaseError("archive has broken references")
        return {"revision": int(values["head"]), "floor": int(values["floor"]),
                "database_id": values["database_id"], "format": int(values["format"]),
                "format_name": FORMAT_NAMES[int(values["format"])], "sha256": file_digest(path)}
    except (sqlite3.DatabaseError, KeyError, ValueError) as exc:
        raise DatabaseError("invalid database archive") from exc
    finally:
        connection.close()


class Archives:
    def __init__(self, store):
        self.store = store

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination.exists():
            raise Conflict("backup destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".strataleaf-", dir=destination.parent)
        os.close(fd)
        try:
            with self.store.lock:
                if self.store.closed:
                    raise DatabaseError("database handle is closed")
                target = sqlite3.connect(temporary)
                try:
                    self.store.connection.backup(target)
                finally:
                    target.close()
            receipt = inspect_archive(temporary)
            if receipt["format"] != self.store.payload_format:
                raise DatabaseError("backup payload format does not match the live database")
            os.link(temporary, destination)
            return {**receipt, "path": str(destination)}
        except FileExistsError as exc:
            raise Conflict("backup destination appeared concurrently") from exc
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def restore(source, destination, expected_sha256=None, follower=False):
        source, destination = Path(source).resolve(), Path(destination).resolve()
        receipt = inspect_archive(source)
        if expected_sha256 is not None and expected_sha256 != receipt["sha256"]:
            raise DatabaseError("archive checksum mismatch")
        if destination.exists():
            raise Conflict("restore requires an unused destination")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".strataleaf-restore-", dir=destination.parent)
        os.close(fd)
        original = None
        target = None
        try:
            original = _connect(source)
            target = sqlite3.connect(temporary)
            original.backup(target)
            target.execute("UPDATE meta SET value=? WHERE key='mode'", ("replica" if follower else "primary",))
            target.execute("UPDATE meta SET value=? WHERE key='upstream'", (receipt["database_id"] if follower else "",))
            if not follower:
                target.execute("UPDATE meta SET value=? WHERE key='database_id'", (str(uuid.uuid4()),))
                target.execute("UPDATE meta SET value=? WHERE key='cursor_secret'", (secrets.token_hex(32),))
            target.execute("DELETE FROM pins")
            target.execute("DELETE FROM consumers")
            target.commit()
            target.close()
            target = None
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise Conflict("restore destination appeared concurrently") from exc
        finally:
            if original:
                original.close()
            if target:
                target.close()
            Path(temporary).unlink(missing_ok=True)
        return {**receipt, "path": str(destination), "mode": "replica" if follower else "primary",
                "migration": "required" if receipt["format"] == JSON_FORMAT and not follower else
                             "legacy-archive-follows-upstream" if receipt["format"] == JSON_FORMAT else "none"}
