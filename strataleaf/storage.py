import contextlib
import secrets
import sqlite3
import threading
import uuid
from .codec import CURRENT_FORMAT, FORMAT_NAMES, JSON_FORMAT, PACK_FORMAT, encode, msgpack_available, render
from .errors import DatabaseError, FormatMigrationRequired, MigrationError, ReadOnly

#: ``meta.format`` values.  The legacy JSON-text database and every archive it
#: produced carry ``"1"``; MessagePack payloads carry ``"2"``.  A migration in
#: progress deliberately uses a value that neither the old build nor the new
#: build accepts as a normal database.
MIGRATING = "migrating:1->2"
SUPPORTED_FORMATS = {str(JSON_FORMAT), str(PACK_FORMAT)}

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collections (name TEXT PRIMARY KEY, schema, generation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS commits (rev INTEGER PRIMARY KEY, payload, request_key TEXT UNIQUE, request_hash TEXT);
CREATE TABLE IF NOT EXISTS receipts (request_key TEXT PRIMARY KEY, rev INTEGER NOT NULL, request_hash TEXT NOT NULL, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS versions (
 collection TEXT NOT NULL, key TEXT NOT NULL, rev INTEGER NOT NULL, body,
 expires REAL, PRIMARY KEY(collection,key,rev), FOREIGN KEY(collection) REFERENCES collections(name));
CREATE INDEX IF NOT EXISTS version_revision ON versions(rev);
CREATE TABLE IF NOT EXISTS index_defs (
 collection TEXT NOT NULL, name TEXT NOT NULL, field TEXT NOT NULL, unique_flag INTEGER NOT NULL,
 PRIMARY KEY(collection,name), FOREIGN KEY(collection) REFERENCES collections(name));
CREATE TABLE IF NOT EXISTS index_values (
 collection TEXT NOT NULL, name TEXT NOT NULL, token TEXT NOT NULL, key TEXT NOT NULL,
 PRIMARY KEY(collection,name,token,key), FOREIGN KEY(collection,name) REFERENCES index_defs(collection,name) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS index_document ON index_values(collection,key);
CREATE TABLE IF NOT EXISTS pins (name TEXT PRIMARY KEY, rev INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS consumers (name TEXT PRIMARY KEY, ack INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS migration (
 id INTEGER PRIMARY KEY CHECK (id=1), source_format TEXT NOT NULL, target_format TEXT NOT NULL,
 state TEXT NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 converted INTEGER NOT NULL, remaining INTEGER NOT NULL, detail TEXT NOT NULL);
"""

class Storage:
    def __init__(self, path, timeout=5.0, create_format=None):
        self.path = str(path)
        self.lock = threading.RLock()
        self.closed = False
        if create_format is not None and create_format not in {JSON_FORMAT, PACK_FORMAT}:
            raise DatabaseError("create_format must be 1 (JSON text) or 2 (MessagePack)")
        self.connection = sqlite3.connect(self.path, timeout=timeout, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        try:
            existing = self._existing_format(allow_format=create_format)
        except BaseException:
            self.connection.close()
            self.closed = True
            raise
        # Refuse legacy or half-migrated files *before* any DDL, pragma or
        # metadata insert: an unopened database must stay byte-for-byte intact.
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(DDL)
        with self.atomic():
            initial = {"format": str(CURRENT_FORMAT if create_format is None else create_format),
                       "head": "0", "floor": "0", "database_id": str(uuid.uuid4()),
                       "cursor_secret": secrets.token_hex(32), "mode": "primary", "upstream": "", "catalog_epoch": "0"}
            for key, value in initial.items():
                self.connection.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, value))
        self.payload_format = existing if existing is not None else int(create_format or CURRENT_FORMAT)
        if self.payload_format == PACK_FORMAT and not msgpack_available():
            self.connection.close()
            self.closed = True
            raise DatabaseError("database uses MessagePack payloads but the msgpack 1.2.4 dependency is missing")

    # -- format ----------------------------------------------------------
    def _existing_format(self, allow_format=None):
        """Detect the declared on-disk format without writing to the file.

        ``allow_format`` lets an explicit ``create_format`` reopen a database
        that already declares that same format.  This is the fixture/tooling
        path used to build format-1 databases; ordinary ``Database`` handles
        pass ``None`` and therefore never touch legacy files.
        """
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() is None:
            # A brand-new empty file is ours to initialise; any other schema is not.
            if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                raise DatabaseError("file is a SQLite database but not a StrataLeaf database")
            return None
        row = self.connection.execute("SELECT value FROM meta WHERE key='format'").fetchone()
        if row is None:
            raise DatabaseError("database has no payload format marker")
        value = row[0]
        if value == str(JSON_FORMAT):
            if allow_format == JSON_FORMAT:
                return JSON_FORMAT
            raise FormatMigrationRequired(
                "database stores JSON text payloads (format 1); run strataleaf.migrate(path) "
                "to convert it to MessagePack before opening it")
        if value == MIGRATING:
            raise FormatMigrationRequired(
                "database payload format migration was interrupted or is in progress; "
                "resume it with strataleaf.migrate(path) before using the database")
        if value != str(PACK_FORMAT):
            raise DatabaseError("unsupported on-disk format: " + value)
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration'").fetchone():
            if self.connection.execute("SELECT 1 FROM migration LIMIT 1").fetchone():
                raise DatabaseError("database claims format 2 but still carries a migration journal; "
                                    "run strataleaf.migrate(path) to resolve it")
        legacy = self._legacy_payloads()
        if legacy:
            raise DatabaseError("database claims format 2 but still contains legacy JSON payloads; "
                                "restore it from an archive or re-run strataleaf.migrate(path)")
        return PACK_FORMAT

    def _legacy_payloads(self):
        """True when a table declared as format 2 still holds a legacy TEXT payload."""
        for table, column in (("collections", "schema"), ("versions", "body"), ("commits", "payload")):
            if self.connection.execute(
                    f"SELECT 1 FROM {table} WHERE typeof({column})='text' LIMIT 1").fetchone():
                return True
        return False

    @property
    def format_name(self):
        return FORMAT_NAMES[self.payload_format]

    def encode(self, value):
        """Represent a logical value in this database's declared format."""
        if self.payload_format == JSON_FORMAT:
            return render(value)
        if self.payload_format == PACK_FORMAT:
            return encode(value)
        raise MigrationError("database payload format is being migrated")

    def get(self, key):
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            raise DatabaseError("missing storage metadata: " + key)
        return row[0]

    def set(self, key, value):
        self.connection.execute("UPDATE meta SET value=? WHERE key=?", (str(value), key))

    @contextlib.contextmanager
    def atomic(self, writable=False):
        with self.lock:
            if self.closed:
                raise DatabaseError("database handle is closed")
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                if writable:
                    if self.get("format") != str(self.payload_format):
                        raise MigrationError("database payload format changed; reopen the database before writing")
                    if self.get("mode") != "primary":
                        raise ReadOnly("replica rejects primary writes")
                yield self.connection
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    @contextlib.contextmanager
    def read(self):
        with self.lock:
            if self.closed:
                raise DatabaseError("database handle is closed")
            self.connection.execute("BEGIN")
            try:
                yield self.connection
            finally:
                self.connection.rollback()

    def checkpoint(self):
        with self.lock:
            if self.closed:
                raise DatabaseError("database handle is closed")
            return tuple(self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())

    def close(self):
        with self.lock:
            if not self.closed:
                self.connection.close()
                self.closed = True
