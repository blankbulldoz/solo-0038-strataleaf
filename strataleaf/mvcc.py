from dataclasses import dataclass
from .codec import digest, key as checked_key, loads
from .cursors import decode_cursor, encode_cursor
from .errors import InvalidCursor, InvalidDocument, NotFound, SnapshotTooOld


@dataclass(frozen=True)
class Record:
    collection: str
    key: str
    revision: int
    document: dict
    expires_at: float | None = None


# States of a key across a run of read revisions.
PRESENT = "present"
ABSENT = "absent"
UNKNOWN = "unknown"

# How the state at the start of a span came to be.  ``overwrite`` covers a
# rewrite that restores an earlier body: bodies are never compared, so equal
# endpoint content cannot collapse the changes between them.
CREATE = "create"
OVERWRITE = "overwrite"
DELETE = "delete"
RECREATE = "recreate"
CARRIED_IN = "carried_in"
FLOOR_BASELINE = "floor_baseline"
UNPROVEN_ORIGIN = "unproven_origin"
NO_PRIOR_VERSION = "no_prior_version"
UNRETAINED = "unretained"

HISTORY_LIMIT = 1000


@dataclass(frozen=True)
class HistorySpan:
    """One contiguous run of read revisions with the same provable state.

    ``start`` and ``end`` are inclusive read revisions.  ``revision`` names the
    version row that defines the state; for ``floor_baseline`` it lies before
    the floor (and therefore before ``start``), and for ``unknown`` spans it is
    ``None``.  Every retained version row inside the interval starts a new
    span, including a rewrite whose body equals an earlier one.
    """
    start: int
    end: int
    state: str
    action: str
    revision: int | None
    document: dict | None = None
    expires_at: float | None = None


@dataclass(frozen=True)
class HistoryPage:
    collection: str
    key: str
    since: int
    until: int
    floor: int
    spans: tuple
    complete: bool
    cursor: str | None
    invalidated: tuple = ()


def checked_revision(store, revision=None):
    head, floor = int(store.get("head")), int(store.get("floor"))
    revision = head if revision is None else revision
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0 or revision > head:
        raise InvalidDocument("read revision must be between zero and current head")
    if revision < floor:
        raise SnapshotTooOld(f"revision {revision} is older than retained floor {floor}")
    return revision


def latest_row(connection, collection, key, revision):
    return connection.execute("SELECT * FROM versions WHERE collection=? AND key=? AND rev<=? ORDER BY rev DESC LIMIT 1",
                              (collection, key, revision)).fetchone()


def visible_rows(connection, collection, revision):
    return connection.execute("""SELECT v.* FROM versions v JOIN
      (SELECT key,MAX(rev) AS r FROM versions WHERE collection=? AND rev<=? GROUP BY key) latest
      ON v.collection=? AND v.key=latest.key AND v.rev=latest.r
      WHERE v.body IS NOT NULL ORDER BY v.key""", (collection, revision, collection)).fetchall()


def record(row):
    if row is None or row["body"] is None:
        return None
    return Record(row["collection"], row["key"], row["rev"], loads(row["body"]), row["expires"])


def read_record(store, collection, key, revision=None):
    with store.read() as connection:
        if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
            raise NotFound("unknown collection: " + collection)
        revision = checked_revision(store, revision)
        return record(latest_row(connection, collection, key, revision))


def _bound(value, name, head):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > head:
        raise InvalidDocument(f"{name} revision must be between zero and current head")
    return value


def _span_state(row):
    if row is None or row["body"] is None:
        return ABSENT, None, None
    return PRESENT, loads(row["body"]), row["expires"]


def _row_action(row, prior, floor):
    if row["body"] is None:
        return DELETE
    if prior is None:
        if row["rev"] == floor and floor >= 2:
            # Compaction keeps the newest version at or below the floor, so this
            # row is that kept baseline; any older row of the key was removed
            # with it.  Whether the key existed at ``floor - 1`` is no longer
            # stored, so this write cannot be called a creation.
            return UNPROVEN_ORIGIN
        # No row below ``low`` and this row is not the floor's kept baseline:
        # the key provably never had a version before this write.
        return CREATE
    return RECREATE if prior["body"] is None else OVERWRITE


def _interval_spans(connection, collection, key, low, until, floor):
    """Provable spans inside ``[low, until]`` with ``low >= floor``.

    Every retained row cuts a span, and the state before the first row is
    inherited from the newest row strictly below ``low``.  That inherited
    state is labelled ``floor_baseline`` when its row predates the floor: it
    answers "what was readable at the floor" without claiming a write happened
    inside the requested interval.  Compaction always keeps, for every key, its
    newest version at or below the boundary, so a missing row below ``low``
    means the key held nothing that compaction could have saved.  When ``low``
    is the floor itself the inherited state is therefore ``no_prior_version``;
    when ``low`` is above the floor, a row at exactly ``low`` may itself be
    that kept baseline and its origin is reported as ``unproven_origin``.
    """
    rows = connection.execute("""SELECT rev,body,expires FROM versions
      WHERE collection=? AND key=? AND rev>=? AND rev<=? ORDER BY rev""",
        (collection, key, low, until)).fetchall()
    prior = latest_row(connection, collection, key, low - 1) if low > 0 else None
    spans = []
    if not rows or rows[0]["rev"] > low:
        end = rows[0]["rev"] - 1 if rows else until
        state, document, expires = _span_state(prior)
        if prior is None:
            action = NO_PRIOR_VERSION
        elif prior["rev"] < floor:
            action = FLOOR_BASELINE
        else:
            action = CARRIED_IN
        spans.append(HistorySpan(low, end, state, action,
                                 None if prior is None else prior["rev"], document, expires))
    for index, row in enumerate(rows):
        end = rows[index + 1]["rev"] - 1 if index + 1 < len(rows) else until
        state, document, expires = _span_state(row)
        spans.append(HistorySpan(row["rev"], end, state, _row_action(row, prior, floor),
                                 row["rev"], document, expires))
        prior = row
    return spans


def _unknown_span(start, end):
    return HistorySpan(start, end, UNKNOWN, UNRETAINED, None)


def _check_limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= HISTORY_LIMIT:
        raise InvalidDocument(f"history limit must be between 1 and {HISTORY_LIMIT} spans")
    return limit


def _cursor_payload(store, collection, key, since, until, next_revision, floor):
    return {"database": store.get("database_id"), "kind": "history",
            "query": digest({"collection": collection, "key": key, "since": since, "until": until}),
            "collection": collection, "key": key, "since": since, "until": until,
            "next": next_revision, "floor": floor}


def _resume(store, connection, collection, key, since, until, cursor):
    payload = decode_cursor(store, cursor)
    if payload.get("kind") != "history" or payload.get("database") != store.get("database_id"):
        raise InvalidCursor("cursor does not belong to a history traversal")
    if payload.get("collection") != collection or payload.get("key") != key:
        raise InvalidCursor("cursor belongs to a different document")
    if since is not None and since != payload.get("since"):
        raise InvalidCursor("cursor belongs to a different interval start")
    if until is not None and until != payload.get("until"):
        raise InvalidCursor("history endpoint is fixed by the first page")
    expected = digest({"collection": collection, "key": key,
                       "since": payload.get("since"), "until": payload.get("until")})
    if payload.get("query") != expected:
        raise InvalidCursor("cursor query summary mismatch")
    since, until, next_revision, before_floor = (payload.get(key) for key in ("since", "until", "next", "floor"))
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (since, until, next_revision, before_floor)):
        raise InvalidCursor("malformed history cursor")
    if not 0 <= since <= until or next_revision < since or next_revision > until + 1:
        raise InvalidCursor("malformed history interval")
    floor = int(store.get("floor"))
    invalidated = []
    if floor > before_floor:
        # Everything already served that has since fallen below the floor can
        # no longer be proven; report exactly that slice rather than silently
        # re-labelling pages the caller already consumed.
        low, high = max(since, before_floor), min(next_revision - 1, floor - 1)
        if low <= high:
            invalidated.append({"start": low, "end": high})
    spans = []
    if next_revision < floor:
        spans.append(_unknown_span(next_revision, min(floor - 1, until)))
        next_revision = floor
    if next_revision <= until:
        spans.extend(_interval_spans(connection, collection, key, next_revision, until, floor))
    return since, until, floor, spans, invalidated


def read_history(store, collection, key, since=None, until=None, limit=100, cursor=None):
    """Traverse when ``collection``/``key`` was readable and when it was absent.

    ``since`` defaults to revision zero and ``until`` to the current head.  The
    first page fixes both endpoints; continuation pages reject a different
    endpoint and never include commits made after it.  Reads below the retained
    floor are reported as an explicit :data:`UNKNOWN` prefix ("unretained")
    rather than answered from a surviving baseline version, and a baseline is
    never reported as a write inside the interval.  Points inside any returned
    span remain verifiable through the unchanged single-revision
    :func:`read_record` / ``Database.get`` read.
    """
    checked_key(key)
    limit = _check_limit(limit)
    with store.read() as connection:
        if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
            raise NotFound("unknown collection: " + collection)
        head, floor = int(store.get("head")), int(store.get("floor"))
        if cursor is not None:
            since, until, floor, spans, invalidated = _resume(
                store, connection, collection, key, since, until, cursor)
        else:
            since = 0 if since is None else _bound(since, "since", head)
            until = head if until is None else _bound(until, "until", head)
            if until < since:
                raise InvalidDocument("history interval end precedes its start")
            invalidated = []
            spans = []
            if since < floor:
                spans.append(_unknown_span(since, min(floor - 1, until)))
            low = max(since, floor)
            if low <= until:
                spans.extend(_interval_spans(connection, collection, key, low, until, floor))
        page_spans = spans[:limit]
        remaining = spans[limit:]
        continuation = None
        if remaining:
            continuation = encode_cursor(store, _cursor_payload(
                store, collection, key, since, until, page_spans[-1].end + 1, floor))
        return HistoryPage(collection, key, since, until, floor, tuple(page_spans),
                           not remaining, continuation, tuple(invalidated))
