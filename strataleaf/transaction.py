import math
from dataclasses import dataclass
from .codec import digest, dumps, field, key as checked_key, loads, name, scalar_token
from .errors import Conflict, InvalidDocument, NotFound
from .indexes import refresh_document
from .mvcc import latest_row
from .schema import document_problems, validate_document


@dataclass(frozen=True)
class CommitResult:
    revision: int
    count: int
    replayed: bool = False


@dataclass(frozen=True)
class OperationVerdict:
    """Adjudication outcome for one operation of a batch."""
    index: int
    collection: str
    key: str
    status: str
    problems: tuple


@dataclass(frozen=True)
class BatchAdjudication:
    """Whole-batch verdicts judged together against one snapshot of head.

    ``basis_revision`` is the read revision the adjudication ran against, not a
    commit: a "clear" report reserves nothing and commit() remains the only
    write path. Idempotency receipts are not consulted here either.
    """
    status: str
    basis_revision: int
    fingerprint: str
    verdicts: tuple

    def as_dict(self):
        return {"status": self.status, "basis_revision": self.basis_revision, "fingerprint": self.fingerprint,
                "verdicts": [{"index": verdict.index, "collection": verdict.collection, "key": verdict.key,
                              "status": verdict.status, "problems": [dict(problem) for problem in verdict.problems]}
                             for verdict in self.verdicts]}


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

    def adjudicate(self):
        """Judge the whole batch against one snapshot without committing anything.

        Every operation is checked on the same read revision: revision
        preconditions, schema conformance and unique occupation are reported
        per operation, separating proven conflicts from relations that stay
        undetermined because a prerequisite operation is invalid. Occupants
        outside the batch are identified by constraint and key only, never by
        their document. The report is read-only — no versions, commits,
        receipts or index rows are written — and a "clear" report is not a
        commit: concurrent writes can still make commit() raise, in which case
        the batch must be adjudicated again.
        """
        if self.state != "open":
            raise Conflict("batch is already " + self.state)
        if not self.operations or len(self.operations) > 10000:
            raise InvalidDocument("batch must contain between 1 and 10000 operations")
        with self.store.read() as connection:
            head = int(self.store.get("head"))
            schemas = {}
            problems = [[] for _ in self.operations]
            for position, op in enumerate(self.operations):
                if op["collection"] not in schemas:
                    row = connection.execute("SELECT schema FROM collections WHERE name=?", (op["collection"],)).fetchone()
                    schemas[op["collection"]] = None if row is None else loads(row[0])
                schema = schemas[op["collection"]]
                if schema is None:
                    problems[position].append({"kind": "unknown_collection", "collection": op["collection"]})
                    continue
                old = latest_row(connection, op["collection"], op["key"], head)
                current = old["rev"] if old is not None else 0
                if op["expected"] is not None and op["expected"] != current:
                    problems[position].append({"kind": "revision_conflict",
                                               "expected": op["expected"], "current": current})
                if op["document"] is not None:
                    for message in document_problems(op["document"], schema):
                        problems[position].append({"kind": "schema_violation", "message": message})
            invalid = {position for position, found in enumerate(problems) if found}
            self._adjudicate_unique(connection, problems, invalid)
            verdicts = []
            for position, op in enumerate(self.operations):
                found = problems[position]
                if not found:
                    status = "clear"
                elif any(problem["kind"] != "unique_occupation" for problem in found):
                    status = "invalid"
                elif any(problem["certainty"] == "proven" for problem in found):
                    status = "conflict"
                else:
                    status = "undetermined"
                verdicts.append(OperationVerdict(position, op["collection"], op["key"], status, tuple(found)))
            status = "clear" if all(verdict.status == "clear" for verdict in verdicts) else "rejected"
            return BatchAdjudication(status, head, digest(self.operations), tuple(verdicts))

    def _adjudicate_unique(self, connection, problems, invalid):
        definitions = {}
        for op in self.operations:
            if op["collection"] not in definitions:
                rows = connection.execute("SELECT name, field FROM index_defs WHERE collection=? AND unique_flag=1",
                                          (op["collection"],)).fetchall()
                definitions[op["collection"]] = [(row["name"], row["field"]) for row in rows]
        target_of = {(op["collection"], op["key"]): position for position, op in enumerate(self.operations)}
        held, claimed, interest = {}, {}, set()
        for position, op in enumerate(self.operations):
            unique = {index_name for index_name, _ in definitions[op["collection"]]}
            if not unique:
                continue
            identity = (op["collection"], op["key"])
            rows = connection.execute("SELECT name, token FROM index_values WHERE collection=? AND key=?",
                                      identity).fetchall()
            held[identity] = [(row["name"], row["token"]) for row in rows if row["name"] in unique]
            interest.update((op["collection"], index_name, token) for index_name, token in held[identity])
            if op["document"] is not None:
                for index_name, path in definitions[op["collection"]]:
                    token = scalar_token(field(op["document"], path))
                    if token is not None:
                        claimed.setdefault((op["collection"], index_name, token), []).append(position)
                        interest.add((op["collection"], index_name, token))
        occupancy = {}
        for collection, index_name, token in interest:
            row = connection.execute("SELECT key FROM index_values WHERE collection=? AND name=? AND token=? LIMIT 1",
                                     (collection, index_name, token)).fetchone()
            if row is not None:
                occupancy[(collection, index_name, token)] = row[0]
        for position, op in enumerate(self.operations):
            if position in invalid:
                continue
            identity = (op["collection"], op["key"])
            for index_name, token in held.get(identity, []):
                if occupancy.get((op["collection"], index_name, token)) == op["key"]:
                    del occupancy[(op["collection"], index_name, token)]
            if op["document"] is None:
                continue
            for index_name, path in definitions[op["collection"]]:
                token = scalar_token(field(op["document"], path))
                if token is None:
                    continue
                slot = (op["collection"], index_name, token)
                holder = occupancy.get(slot)
                if holder is None or holder == op["key"]:
                    rivals = [other for other in claimed.get(slot, []) if other in invalid]
                    if rivals:
                        problems[position].append({"kind": "unique_occupation", "certainty": "undetermined",
                                                   "index": index_name, "field": path, "depends_on": rivals,
                                                   "reason": "invalid operations may claim the same unique value"})
                    occupancy[slot] = op["key"]
                    continue
                holder_ref = {"collection": op["collection"], "key": holder}
                other = target_of.get((op["collection"], holder))
                if other is None:
                    problems[position].append({"kind": "unique_occupation", "certainty": "proven",
                                               "index": index_name, "field": path, "holder": holder_ref})
                elif other in invalid:
                    problems[position].append({"kind": "unique_occupation", "certainty": "undetermined",
                                               "index": index_name, "field": path, "holder": holder_ref,
                                               "depends_on": [other],
                                               "reason": "the holder release depends on an invalid operation"})
                else:
                    problems[position].append({"kind": "unique_occupation", "certainty": "proven",
                                               "index": index_name, "field": path, "holder": holder_ref,
                                               "operation": other})

    def commit(self):
        if self.state != "open":
            raise Conflict("batch is already " + self.state)
        if not self.operations or len(self.operations) > 10000:
            raise InvalidDocument("batch must contain between 1 and 10000 operations")
        fingerprint = digest(self.operations)
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
                for op in self.operations:
                    schema = connection.execute("SELECT schema FROM collections WHERE name=?", (op["collection"],)).fetchone()
                    if schema is None:
                        raise NotFound("unknown collection: " + op["collection"])
                    old = latest_row(connection, op["collection"], op["key"], head)
                    current = old["rev"] if old is not None else 0
                    if op["expected"] is not None and op["expected"] != current:
                        raise Conflict(f"revision conflict for {op['collection']}/{op['key']}: expected {op['expected']}, found {current}")
                    if op["document"] is not None:
                        validate_document(op["document"], loads(schema[0]))
                revision = head + 1
                for op in self.operations:
                    body = dumps(op["document"]) if op["document"] is not None else None
                    connection.execute("INSERT INTO versions VALUES (?,?,?,?,?)",
                                       (op["collection"], op["key"], revision, body, op["expires_at"]))
                    refresh_document(connection, op["collection"], op["key"], op["document"])
                payload = {"revision": revision, "operations": self.operations}
                connection.execute("INSERT INTO commits VALUES (?,?,?,?)",
                                   (revision, dumps(payload), self.request_key, fingerprint))
                if self.request_key is not None:
                    connection.execute("INSERT INTO receipts VALUES (?,?,?,?)", (self.request_key, revision, fingerprint, len(self.operations)))
                self.store.set("head", revision)
        except BaseException:
            self.state = "aborted"
            raise
        self.state = "committed"
        return CommitResult(revision, len(self.operations))
