import math
from dataclasses import dataclass
from .adjudication import Replay, decide
from .codec import digest, dumps, key as checked_key, loads, name
from .errors import Conflict, InvalidDocument
from .journal import apply_plan, load_facts


@dataclass(frozen=True)
class CommitResult:
    revision: int
    count: int
    replayed: bool = False


class WriteBatch:
    def __init__(self, store, request_key=None):
        self.store = store
        self.request_key = checked_key(request_key) if request_key is not None else None
        self.operations = []
        self.state = "open"

    def _append(self, operation):
        if self.state != "open":
            raise Conflict("batch is already " + self.state)
        identity = (operation["collection"], operation["key"])
        if any((op["collection"], op["key"]) == identity for op in self.operations):
            raise InvalidDocument("one operation per document per batch")
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
        fingerprint = digest(self.operations)
        try:
            with self.store.atomic(writable=True) as connection:
                facts = load_facts(connection, self.operations, self.request_key)
                ruling = decide(facts, self.operations, self.request_key, fingerprint)
                if isinstance(ruling, Replay):
                    self.state = "committed"
                    return CommitResult(ruling.revision, ruling.count, True)
                apply_plan(connection, ruling)
        except BaseException:
            self.state = "aborted"
            raise
        self.state = "committed"
        return CommitResult(ruling.revision, ruling.count)
