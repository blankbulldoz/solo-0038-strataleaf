import math
from dataclasses import dataclass
from .codec import digest, dumps, key as checked_key, loads, name
from .errors import Conflict, InvalidDocument, NotFound
from .indexes import refresh_document
from .mvcc import latest_row
from .schema import validate_document


@dataclass(frozen=True)
class CommitResult:
    revision: int
    count: int
    replayed: bool = False


def effective_operations(operations):
    """Return the last staged operation for every document, in staged order.

    All operations in a batch share one commit revision, so only this reduced
    sequence describes the documents that the commit actually publishes.
    Earlier operations for the same collection/key are intermediate steps: they
    are kept in the commit log for audit and replay, but never become a visible
    document version of their own.
    """
    last = {}
    for position, operation in enumerate(operations):
        last[(operation["collection"], operation["key"])] = position
    return [operation for position, operation in enumerate(operations)
            if last[(operation["collection"], operation["key"])] == position]


class WriteBatch:
    def __init__(self, store, request_key=None):
        self.store = store
        self.request_key = checked_key(request_key) if request_key is not None else None
        self.operations = []
        self.state = "open"

    def _append(self, operation):
        if self.state != "open":
            raise Conflict("batch is already " + self.state)
        expected = operation["expected"]
        if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 0):
            raise InvalidDocument("expected revision must be nonnegative or None")
        self.operations.append(operation)
        return self

    def put(self, collection, key, document, expected=None, expires_at=None):
        name(collection)
        checked_key(key)
        if not isinstance(document, dict):
            raise InvalidDocument("document must be an object")
        if expires_at is not None and (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at)):
            raise InvalidDocument("expires_at must be a finite timestamp")
        return self._append({"collection": collection, "key": key, "document": loads(dumps(document)),
                             "expected": expected, "expires_at": expires_at})

    def delete(self, collection, key, expected=None):
        return self._append({"collection": name(collection), "key": checked_key(key), "document": None,
                             "expected": expected, "expires_at": None})

    def abort(self):
        if self.state != "open":
            raise Conflict("batch is already " + self.state)
        self.operations.clear()
        self.state = "aborted"

    def commit(self):
        if self.state != "open":
            raise Conflict("batch is already " + self.state)
        if not self.operations or len(self.operations) > 10000:
            raise InvalidDocument("batch must contain between 1 and 10000 operations")
        steps = list(self.operations)
        fingerprint = digest(steps)
        try:
            with self.store.atomic(writable=True) as connection:
                if self.request_key is not None:
                    prior = connection.execute("SELECT * FROM receipts WHERE request_key=?", (self.request_key,)).fetchone()
                    if prior:
                        if prior["request_hash"] != fingerprint:
                            raise Conflict("request key was used with different operations")
                        self.state = "committed"
                        return CommitResult(prior["rev"], prior["count"], True)
                head = int(self.store.get("head"))
                base = {}
                schemas = {}
                for op in steps:
                    identity = (op["collection"], op["key"])
                    if identity not in base:
                        old = latest_row(connection, op["collection"], op["key"], head)
                        base[identity] = old["rev"] if old is not None else 0
                    schema = schemas.get(op["collection"])
                    if schema is None:
                        row = connection.execute("SELECT schema FROM collections WHERE name=?", (op["collection"],)).fetchone()
                        if row is None:
                            raise NotFound("unknown collection: " + op["collection"])
                        schema = schemas[op["collection"]] = loads(row[0])
                    if op["expected"] is not None and op["expected"] != base[identity]:
                        raise Conflict(f"revision conflict for {op['collection']}/{op['key']}: expected {op['expected']}, found {base[identity]}")
                    if op["document"] is not None:
                        validate_document(op["document"], schema)
                revision = head + 1
                # Steps run in staged order and each sees the logical document and
                # index projection left by the steps before it. Only the last step
                # for a key is published as a version at this revision.
                for op in steps:
                    refresh_document(connection, op["collection"], op["key"], op["document"])
                final = effective_operations(steps)
                for op in final:
                    body = dumps(op["document"]) if op["document"] is not None else None
                    connection.execute("INSERT INTO versions VALUES (?,?,?,?,?)",
                                       (op["collection"], op["key"], revision, body, op["expires_at"]))
                payload = {"revision": revision, "operations": final}
                if len(final) != len(steps):
                    payload["steps"] = steps
                connection.execute("INSERT INTO commits VALUES (?,?,?,?)",
                                   (revision, dumps(payload), self.request_key, fingerprint))
                if self.request_key is not None:
                    connection.execute("INSERT INTO receipts VALUES (?,?,?,?)", (self.request_key, revision, fingerprint, len(final)))
                self.store.set("head", revision)
        except BaseException:
            self.state = "aborted"
            raise
        self.state = "committed"
        # count is the number of document versions this revision publishes;
        # repeated steps for one key collapse into their last step.
        return CommitResult(revision, len(final))
