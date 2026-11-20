"""SQLite driver boundary around APSW 3.54.1.0.

The rest of the package only uses the small surface defined here so the move
off the standard library ``sqlite3`` module stays in one place:

* :class:`Row` keeps the dual index/by-name row access (``row[0]`` and
  ``row["body"]``, ``dict(row)``) that callers and internal modules rely on.
* :func:`connect` hides APSW connection flags, busy timeout units and the row
  tracer installation.
* APSW has no ``Cursor.rowcount``; :func:`changes` reads the affected-row
  count from the connection instead.
* APSW never opens implicit transactions, has no ``executescript`` and no
  ``Connection.commit/rollback``; the storage layer drives explicit
  ``BEGIN``/``COMMIT``/``ROLLBACK`` statements through the connection.
"""
import apsw

# Re-exported so callers that previously referenced sqlite3.OperationalError on
# lock contention can target the APSW equivalent explicitly.
BusyError = apsw.BusyError
LockedError = apsw.LockedError
ConstraintError = apsw.ConstraintError
ReadOnlyError = apsw.ReadOnlyError
DriverError = apsw.Error

READONLY_FLAGS = apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI


class Row:
    """A view compatible with the subset of ``sqlite3.Row`` used by the package.

    Columns are accessible by position (``row[0]``), by case-sensitive column
    name (``row["body"]``) and through :func:`dict`/iteration. Duplicate column
    names resolve to the first match, matching ``sqlite3.Row`` semantics.
    """

    __slots__ = ("_columns", "_values")

    def __init__(self, columns, values):
        self._columns = tuple(columns)
        self._values = tuple(values)

    def keys(self):
        return list(self._columns)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        for index, column in enumerate(self._columns):
            if column == key:
                return self._values[index]
        raise IndexError("unknown column: " + str(key))

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        if isinstance(other, Row):
            return self._values == other._values
        if isinstance(other, tuple):
            return self._values == other
        return NotImplemented

    def __hash__(self):
        return hash(self._values)

    def __repr__(self):
        return "<Row " + repr(tuple(zip(self._columns, self._values))) + ">"


def _row_tracer(cursor, values):
    return Row([description[0] for description in cursor.get_description()], values)


def connect(path, timeout=5.0, readonly=False):
    """Open an APSW connection with the package's standard configuration."""
    if readonly:
        location = str(path)
        if not location.startswith("file:"):
            location = "file:" + location
        if "?" not in location:
            location += "?mode=ro"
        connection = apsw.Connection(location, flags=READONLY_FLAGS)
    else:
        connection = apsw.Connection(str(path))
    # APSW takes milliseconds; the public Database/Storage timeout is seconds.
    connection.set_busy_timeout(max(0, int(round(float(timeout) * 1000))))
    connection.row_trace = _row_tracer
    return connection


def changes(connection):
    """Number of rows modified by the most recent INSERT/UPDATE/DELETE.

    Replaces ``sqlite3.Cursor.rowcount``, which APSW cursors do not provide.
    """
    return connection.changes()


def run_script(connection, script):
    """Execute a semicolon-separated DDL script (APSW has no executescript)."""
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def online_backup(target, source):
    """Copy ``source`` into ``target`` with SQLite's online backup API."""
    backup = target.backup("main", source, "main")
    try:
        while not backup.done:
            backup.step()
    finally:
        backup.finish()
