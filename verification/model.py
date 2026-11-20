"""Independent expected-state model of strataleaf's documented semantics.

This module is the verification oracle. It re-implements the state semantics
fixed by PROJECT_SPEC.md / README.md / docs in plain standard-library Python
and shares NO code with the production ``strataleaf`` package. Every method
mirrors one production API call: it either returns a JSON-comparable value or
raises one of the model error types below. Model error classes intentionally
carry the same names as ``strataleaf.errors`` classes so the differential
runner can compare outcomes by name without importing production code.

Semantics modeled (mirroring production, including check ordering):

* one global revision counter; a committed batch advances it by exactly one
  and all of its operations become visible under that single revision;
* per-key version chains with delete tombstones; ``expected`` preconditions
  compare against the latest version including tombstones (0 = never written);
* batch validation split into append-time checks (name/key/document shape,
  duplicate identity, expected type) and commit-time checks (request-key
  replay, unknown collection, stale expected, schema), then an apply phase
  where unique-index conflicts abort the whole batch atomically;
* schema normalization/validation (fields/required/additional, integer and
  number exclude bool, declared fields reject null, additional=false only
  restricts top-level roots);
* MVCC reads at pinned revisions with floor/head validation;
* predicate validation and matching (missing != null, bool is not numeric);
* query planning (equality index only at current head), key-ordered pages,
  examined counts, and signed-cursor semantics modeled with model-issued
  opaque tokens bound to database identity, query fingerprint and revision;
* index create/drop with unique pre-scan and current-head projection
  maintenance (scalar tokens: 1 and 1.0 share, true and 1 do not, null is an
  indexable scalar, missing/object/array are not indexed);
* read pins, consumer acknowledgements, compaction floor computation
  (boundary = min(target, blockers), conflict when below current floor) and
  physical version/commit removal rules;
* TTL sweep as a delete batch over the latest version of each key;
* merge patch as read-modify-write through the normal commit path;
* idempotent request-key receipts that survive history reclamation.
"""

import hashlib
import json
import math
import re


class DatabaseError(Exception):
    """Base model error."""


class InvalidDocument(DatabaseError):
    pass


class Conflict(DatabaseError):
    pass


class NotFound(DatabaseError):
    pass


class SnapshotTooOld(DatabaseError):
    pass


class InvalidCursor(DatabaseError):
    pass


NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
MISSING = object()
TYPES = {"string": str, "integer": int, "number": (int, float),
         "boolean": bool, "object": dict, "array": list}
OPS = {"eq", "ne", "lt", "le", "gt", "ge", "in", "exists", "contains"}


# ---------------------------------------------------------------------------
# codec-level rules (independent reimplementation of the JSON contract)

def validate_json(value, depth=0):
    if depth > 32:
        raise InvalidDocument("document nesting exceeds 32 levels")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            validate_json(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for item in value.values():
            validate_json(item, depth + 1)
        return
    raise InvalidDocument("only finite JSON values and string object keys are supported")


def canonical(value):
    validate_json(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def parse(text):
    return json.loads(text)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def check_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise InvalidDocument("names must match [a-z][a-z0-9_]{0,62}")
    return value


def check_key(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 512:
        raise InvalidDocument("document key must be a nonempty string of at most 512 bytes")
    return value


def field(document, path):
    if not isinstance(path, str) or not path or any(not p for p in path.split(".")):
        raise InvalidDocument("field path must have nonempty dot-separated components")
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def scalar_token(value):
    """Index token for a scalar; None means 'not indexable'."""
    if value is MISSING or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return ("b", int(value))
    if isinstance(value, int):
        return ("n", value)
    if isinstance(value, float):
        return ("n", int(value)) if value.is_integer() else ("n", repr(value))
    return ("v", canonical(value))


# ---------------------------------------------------------------------------
# schema

def normalize_schema(schema):
    if not isinstance(schema, dict) or set(schema) - {"fields", "required", "additional"}:
        raise InvalidDocument("schema accepts fields, required and additional")
    fields = schema.get("fields", {})
    required = schema.get("required", [])
    if not isinstance(fields, dict) or not isinstance(required, list):
        raise InvalidDocument("fields must be an object and required an array")
    for path, kind in fields.items():
        field({}, path)
        if not isinstance(kind, str) or kind not in TYPES:
            raise InvalidDocument("unsupported field type: " + str(kind))
    if any(not isinstance(x, str) or x not in fields for x in required):
        raise InvalidDocument("required fields must have declared types")
    additional = schema.get("additional", True)
    if not isinstance(additional, bool) or len(set(required)) != len(required):
        raise InvalidDocument("invalid schema options")
    return {"fields": fields, "required": sorted(required), "additional": additional}


def validate_document(document, schema):
    if not isinstance(document, dict):
        raise InvalidDocument("a document must be a JSON object")
    if len(canonical(document).encode()) > 1024 * 1024:
        raise InvalidDocument("document exceeds 1 MiB")
    for path in schema["required"]:
        if field(document, path) is MISSING:
            raise InvalidDocument("missing required field: " + path)
    for path, kind in schema["fields"].items():
        value = field(document, path)
        if value is MISSING:
            continue
        accepted = isinstance(value, TYPES[kind])
        if kind in {"integer", "number"} and isinstance(value, bool):
            accepted = False
        if not accepted:
            raise InvalidDocument("field " + path + " must be " + kind)
    if not schema["additional"]:
        roots = {path.split(".")[0] for path in schema["fields"]}
        if set(document) - roots:
            raise InvalidDocument("undeclared top-level fields")
    return document


# ---------------------------------------------------------------------------
# predicates

def validate_predicate(expression, depth=0):
    if expression is None:
        return
    if depth > 16 or not isinstance(expression, dict):
        raise InvalidDocument("invalid predicate structure")
    if set(expression) in ({"and"}, {"or"}):
        children = next(iter(expression.values()))
        if not isinstance(children, list) or not children or len(children) > 64:
            raise InvalidDocument("boolean predicates require 1..64 children")
        for child in children:
            validate_predicate(child, depth + 1)
        return
    if set(expression) == {"not"}:
        validate_predicate(expression["not"], depth + 1)
        return
    if set(expression) != {"field", "op", "value"} or not isinstance(expression["op"], str) \
            or expression["op"] not in OPS:
        raise InvalidDocument("predicate must have field, op and value")
    field({}, expression["field"])
    canonical(expression["value"])
    if expression["op"] == "in" and not isinstance(expression["value"], list):
        raise InvalidDocument("in requires an array")
    if expression["op"] == "exists" and not isinstance(expression["value"], bool):
        raise InvalidDocument("exists requires a boolean")


def equal(left, right):
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) or isinstance(right, dict):
        return (isinstance(left, dict) and isinstance(right, dict)
                and left.keys() == right.keys()
                and all(equal(left[key], right[key]) for key in left))
    if isinstance(left, list) or isinstance(right, list):
        return (isinstance(left, list) and isinstance(right, list) and len(left) == len(right)
                and all(equal(a, b) for a, b in zip(left, right)))
    return left == right


def matches(document, expression):
    if expression is None:
        return True
    if "and" in expression:
        return all(matches(document, item) for item in expression["and"])
    if "or" in expression:
        return any(matches(document, item) for item in expression["or"])
    if "not" in expression:
        return not matches(document, expression["not"])
    value = field(document, expression["field"])
    op, other = expression["op"], expression["value"]
    if op == "exists":
        return (value is not MISSING) == other
    if value is MISSING:
        return False
    if op == "eq":
        return equal(value, other)
    if op == "ne":
        return not equal(value, other)
    if op == "in":
        return any(equal(value, choice) for choice in other)
    if op == "contains":
        if isinstance(value, list):
            return any(equal(item, other) for item in value)
        return isinstance(value, str) and isinstance(other, str) and other in value
    if isinstance(value, bool) or isinstance(other, bool):
        return False
    numeric = isinstance(value, (int, float)) and isinstance(other, (int, float))
    if not numeric and not (isinstance(value, str) and isinstance(other, str)):
        return False
    return {"lt": lambda: value < other, "le": lambda: value <= other,
            "gt": lambda: value > other, "ge": lambda: value >= other}[op]()


def equality_terms(expression):
    if expression is None:
        return []
    if "and" in expression:
        return [term for child in expression["and"] for term in equality_terms(child)]
    if expression.get("op") == "eq":
        return [(expression["field"], expression["value"])]
    return []


def merge_patch(document, patch):
    if not isinstance(patch, dict):
        return parse(canonical(patch))
    result = dict(document) if isinstance(document, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


# ---------------------------------------------------------------------------
# the model database

class Model:
    """A logical, in-memory oracle for one database file."""

    def __init__(self, database_id="model-primary"):
        self.database_id = database_id
        self.head = 0
        self.floor = 0
        self.collections = {}        # name -> {"schema": normalized, "generation": int}
        self.versions = {}           # (collection, key) -> [(rev, doc|None, expires|None), ...]
        self.commits = {}            # rev -> {"revision": rev, "operations": [...]}
        self.receipts = {}           # request_key -> {"rev", "hash", "count"}
        self.indexes = {}            # collection -> {name: {"field", "unique"}}
        self.index_values = {}       # (collection, name) -> {key: token}
        self.pins = {}               # name -> rev
        self.consumers = {}          # name -> ack
        self.last_error_kind = None  # instrumentation for coverage statistics
        self.last_sweep_keys = []    # instrumentation for the runner

    # -- helpers -----------------------------------------------------------

    def _fail(self, kind, exc):
        self.last_error_kind = kind
        raise exc

    def checked_revision(self, revision):
        revision = self.head if revision is None else revision
        if isinstance(revision, bool) or not isinstance(revision, int) \
                or revision < 0 or revision > self.head:
            raise InvalidDocument("read revision must be between zero and current head")
        if revision < self.floor:
            raise SnapshotTooOld("revision is older than retained floor")
        return revision

    def current_rev(self, collection, key):
        chain = self.versions.get((collection, key))
        return chain[-1][0] if chain else 0

    def row_at(self, collection, key, revision):
        for entry in reversed(self.versions.get((collection, key), ())):
            if entry[0] <= revision:
                return entry
        return None

    def visible(self, collection, revision):
        """[(key, rev, doc, expires)] for live docs at revision, ordered by key."""
        rows = []
        for (coll, key), chain in self.versions.items():
            if coll != collection:
                continue
            for entry in reversed(chain):
                if entry[0] <= revision:
                    if entry[1] is not None:
                        rows.append((key, entry[0], entry[1], entry[2]))
                    break
        rows.sort(key=lambda item: item[0])
        return rows

    @staticmethod
    def _record(collection, key, rev, doc, expires):
        return {"collection": collection, "key": key, "revision": rev,
                "document": doc, "expires_at": expires}

    # -- catalog -----------------------------------------------------------

    def create_collection(self, name, schema=None):
        check_name(name)
        normalized = normalize_schema({} if schema is None else schema)
        if name in self.collections:
            self._fail("collection_exists", Conflict("collection already exists: " + name))
        self.collections[name] = {"schema": normalized, "generation": 1}
        return self.describe(name)

    def describe(self, collection):
        if collection not in self.collections:
            raise NotFound("unknown collection: " + collection)
        entry = self.collections[collection]
        return {"name": collection, "schema": parse(canonical(entry["schema"])),
                "generation": entry["generation"]}

    def catalog_list(self):
        return [{"name": name, "schema": parse(canonical(self.collections[name]["schema"])),
                 "generation": self.collections[name]["generation"]}
                for name in sorted(self.collections)]

    def evolve(self, collection, schema, expected_generation):
        normalized = normalize_schema(schema)
        if collection not in self.collections:
            raise NotFound("unknown collection: " + collection)
        entry = self.collections[collection]
        if entry["generation"] != expected_generation:
            self._fail("generation_conflict", Conflict("schema generation changed"))
        for _key, _rev, doc, _expires in self.visible(collection, self.head):
            validate_document(doc, normalized)
        entry["schema"] = normalized
        entry["generation"] = expected_generation + 1
        return entry["generation"]

    # -- write path ----------------------------------------------------------

    def commit(self, raw_ops, request_key=None):
        if request_key is not None:
            check_key(request_key)
        ops = []
        seen = set()
        for raw in raw_ops:
            if raw["kind"] == "put":
                collection = check_name(raw["collection"])
                key = check_key(raw["key"])
                document = raw["document"]
                if not isinstance(document, dict):
                    raise InvalidDocument("document must be an object")
                expires = raw.get("expires_at")
                if expires is not None and (isinstance(expires, bool)
                                            or not isinstance(expires, (int, float))
                                            or not math.isfinite(expires)):
                    raise InvalidDocument("expires_at must be a finite timestamp")
                document = parse(canonical(document))
            else:
                collection = check_name(raw["collection"])
                key = check_key(raw["key"])
                document, expires = None, None
            if (collection, key) in seen:
                raise InvalidDocument("one operation per document per batch")
            seen.add((collection, key))
            expected = raw.get("expected")
            if expected is not None and (isinstance(expected, bool)
                                         or not isinstance(expected, int) or expected < 0):
                raise InvalidDocument("expected revision must be nonnegative or None")
            ops.append({"collection": collection, "key": key, "document": document,
                        "expected": expected, "expires_at": expires})
        if not ops or len(ops) > 10000:
            raise InvalidDocument("batch must contain between 1 and 10000 operations")
        fingerprint = digest(ops)
        if request_key is not None and request_key in self.receipts:
            prior = self.receipts[request_key]
            if prior["hash"] != fingerprint:
                self._fail("request_key_conflict",
                           Conflict("request key was used with different operations"))
            return {"revision": prior["rev"], "count": prior["count"], "replayed": True}
        for op in ops:
            if op["collection"] not in self.collections:
                self._fail("unknown_collection",
                           NotFound("unknown collection: " + op["collection"]))
            current = self.current_rev(op["collection"], op["key"])
            if op["expected"] is not None and op["expected"] != current:
                self._fail("expected_conflict", Conflict("revision conflict"))
            if op["document"] is not None:
                try:
                    validate_document(op["document"], self.collections[op["collection"]]["schema"])
                except InvalidDocument:
                    self._fail("schema_invalid", InvalidDocument("schema validation failed"))
        # Apply phase: unique-index conflicts abort the whole batch, so the
        # projection updates are simulated on scratch maps first.
        scratch = {}

        def projection(coll, idx):
            if (coll, idx) not in scratch:
                scratch[(coll, idx)] = dict(self.index_values.get((coll, idx), {}))
            return scratch[(coll, idx)]

        revision = self.head + 1
        for op in ops:
            coll, key, document = op["collection"], op["key"], op["document"]
            for idx in self.indexes.get(coll, {}):
                projection(coll, idx).pop(key, None)
            if document is not None:
                for idx, definition in self.indexes.get(coll, {}).items():
                    token = scalar_token(field(document, definition["field"]))
                    if token is None:
                        continue
                    proj = projection(coll, idx)
                    if definition["unique"] and token in proj.values():
                        self._fail("unique_conflict", Conflict("unique index conflict: " + idx))
                    proj[key] = token
        for op in ops:
            expires = op["expires_at"]
            stored = float(expires) if expires is not None else None
            self.versions.setdefault((op["collection"], op["key"]), []).append(
                (revision, op["document"], stored))
        self.index_values.update(scratch)
        self.head = revision
        self.commits[revision] = {"revision": revision, "operations": parse(canonical(ops))}
        if request_key is not None:
            self.receipts[request_key] = {"rev": revision, "hash": fingerprint,
                                          "count": len(ops)}
        return {"revision": revision, "count": len(ops), "replayed": False}

    # -- reads ---------------------------------------------------------------

    def get(self, collection, key, revision=None):
        if collection not in self.collections:
            raise NotFound("unknown collection: " + collection)
        revision = self.checked_revision(revision)
        entry = self.row_at(collection, key, revision)
        if entry is None or entry[1] is None:
            return None
        return self._record(collection, key, entry[0], entry[1], entry[2])

    def query(self, collection, predicate=None, limit=100, cursor=None,
              revision=None, force_scan=False):
        validate_predicate(predicate)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise InvalidDocument("page size must be between 1 and 1000")
        if collection not in self.collections:
            raise NotFound("unknown collection")
        fingerprint = digest({"collection": collection, "predicate": predicate})
        after = None
        if cursor is not None:
            payload = self._decode_cursor(cursor)
            if payload.get("query") != fingerprint \
                    or (revision is not None and revision != payload.get("revision")):
                raise InvalidCursor("cursor does not belong to this query/read revision")
            revision, after = payload.get("revision"), payload.get("after")
            if not isinstance(after, str):
                raise InvalidCursor("cursor has no key boundary")
        revision = self.checked_revision(revision)
        plan = {"access": "version_scan", "index": None}
        candidates = None
        if not force_scan and revision == self.head:
            for path, value in equality_terms(predicate):
                token = scalar_token(value)
                if token is None:
                    continue
                names = [n for n, d in self.indexes.get(collection, {}).items()
                         if d["field"] == path]
                if not names:
                    continue
                chosen = sorted(names)[0]
                candidates = {key for key, tok in self.index_values.get((collection, chosen), {}).items()
                              if tok == token}
                plan = {"access": "equality_index", "index": chosen}
                break
        if candidates is None:
            rows = [(key, rev, doc, expires)
                    for key, rev, doc, expires in self.visible(collection, revision)]
        else:
            rows = []
            for key in sorted(candidates):
                entry = self.row_at(collection, key, revision)
                rows.append((key, entry[0], entry[1], entry[2]) if entry
                            else (key, None, None, None))
        output = []
        examined = 0
        for key, rev, doc, expires in rows:
            if doc is None or (after is not None and key <= after):
                continue
            examined += 1
            if matches(doc, predicate):
                output.append(self._record(collection, key, rev, doc, expires))
                if len(output) > limit:
                    break
        continuation = None
        if len(output) > limit:
            output = output[:limit]
            continuation = self._encode_cursor({"database": self.database_id,
                                                "query": fingerprint, "revision": revision,
                                                "after": output[-1]["key"]})
        return {"records": output, "revision": revision, "cursor": continuation,
                "plan": {**plan, "examined": examined}}

    def _encode_cursor(self, payload):
        return canonical(payload)

    def _decode_cursor(self, token):
        try:
            payload = json.loads(token)
            if not isinstance(payload, dict) or payload.get("database") != self.database_id:
                raise ValueError("wrong database")
            return payload
        except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
            raise InvalidCursor("invalid or foreign continuation cursor") from exc

    # -- indexes ---------------------------------------------------------------

    def index_create(self, collection, index_name, path, unique=False):
        check_name(index_name)
        field({}, path)
        if not isinstance(unique, bool):
            raise InvalidDocument("unique must be boolean")
        if collection not in self.collections:
            raise NotFound("unknown collection")
        if index_name in self.indexes.get(collection, {}):
            self._fail("index_exists", Conflict("index already exists"))
        projection = {}
        seen = set()
        for key, _rev, doc, _expires in self.visible(collection, self.head):
            token = scalar_token(field(doc, path))
            if token is None:
                continue
            if unique and token in seen:
                self._fail("unique_build_conflict",
                           Conflict("existing documents violate unique index"))
            seen.add(token)
            projection[key] = token
        self.indexes.setdefault(collection, {})[index_name] = {"field": path, "unique": unique}
        self.index_values[(collection, index_name)] = projection
        return {"name": index_name, "field": path, "unique": unique}

    def index_drop(self, collection, index_name):
        if index_name not in self.indexes.get(collection, {}):
            raise NotFound("unknown index")
        del self.indexes[collection][index_name]
        del self.index_values[(collection, index_name)]
        return None

    def index_list(self, collection):
        return [{"collection": collection, "name": name,
                 "field": self.indexes[collection][name]["field"],
                 "unique_flag": int(self.indexes[collection][name]["unique"])}
                for name in sorted(self.indexes.get(collection, {}))]

    # -- retention ---------------------------------------------------------------

    def pin(self, pin_name, revision=None):
        check_name(pin_name)
        revision = self.checked_revision(revision)
        if pin_name in self.pins:
            self._fail("pin_exists", Conflict("read pin already exists"))
        self.pins[pin_name] = revision
        return revision

    def unpin(self, pin_name):
        if pin_name not in self.pins:
            raise NotFound("unknown read pin")
        del self.pins[pin_name]
        return None

    def compact(self, target):
        target = self.checked_revision(target)
        blockers = list(self.pins.values()) + list(self.consumers.values())
        boundary = min([target] + blockers)
        if boundary < self.floor:
            self._fail("compact_below_floor", Conflict("retention metadata violates current floor"))
        removed = 0
        for identity, chain in self.versions.items():
            keep = [entry for entry in chain if entry[0] >= boundary]
            older_or_base = [entry for entry in chain if entry[0] <= boundary]
            if older_or_base and older_or_base[-1][0] < boundary:
                keep.append(older_or_base[-1])
            keep.sort(key=lambda entry: entry[0])
            removed += len(chain) - len(keep)
            self.versions[identity] = keep
        before = len(self.commits)
        self.commits = {rev: payload for rev, payload in self.commits.items() if rev > boundary}
        logs = before - len(self.commits)
        self.floor = boundary
        return {"requested": target, "floor": boundary, "removed_versions": removed,
                "removed_commits": logs, "blocked": boundary < target}

    def sweep(self, now, limit=1000):
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise InvalidDocument("expiry sweep requires a finite timestamp")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise InvalidDocument("sweep limit must be between 1 and 10000")
        due = []
        for (coll, key), chain in self.versions.items():
            rev, doc, expires = chain[-1]
            if doc is not None and expires is not None and expires <= now:
                due.append((expires, coll, key, rev))
        due.sort(key=lambda item: (item[0], item[1], item[2]))
        due = due[:limit]
        self.last_sweep_keys = [(coll, key) for _expires, coll, key, _rev in due]
        if not due:
            return {"expired": 0, "revision": None}
        result = self.commit([{"kind": "delete", "collection": coll, "key": key,
                               "expected": rev} for _expires, coll, key, rev in due])
        return {"expired": result["count"], "revision": result["revision"]}

    # -- patch ---------------------------------------------------------------

    def patch(self, collection, key, patch, expected=None):
        if not isinstance(patch, dict):
            raise InvalidDocument("document merge patch must be an object")
        current = self.get(collection, key)
        if current is None:
            raise NotFound("cannot patch a missing document")
        if expected is not None and expected != current["revision"]:
            self._fail("patch_conflict", Conflict("patch revision changed"))
        document = merge_patch(current["document"], patch)
        return self.commit([{"kind": "put", "collection": collection, "key": key,
                             "document": document, "expected": current["revision"],
                             "expires_at": current["expires_at"]}])

    # -- change feed ---------------------------------------------------------------

    def feed_register(self, consumer, since=None):
        check_name(consumer)
        since = self.checked_revision(since)
        if consumer in self.consumers:
            self._fail("consumer_exists", Conflict("consumer already registered"))
        self.consumers[consumer] = since
        return since

    def feed_position(self, consumer):
        if consumer not in self.consumers:
            raise NotFound("unknown consumer")
        return self.consumers[consumer]

    def feed_ack(self, consumer, revision):
        revision = self.checked_revision(revision)
        if consumer not in self.consumers:
            raise NotFound("unknown consumer")
        if revision < self.consumers[consumer]:
            self._fail("ack_backward", Conflict("consumer acknowledgement cannot move backwards"))
        self.consumers[consumer] = revision
        return revision

    def feed_release(self, consumer):
        if consumer not in self.consumers:
            raise NotFound("unknown consumer")
        del self.consumers[consumer]
        return None

    def feed_read(self, since, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise InvalidDocument("feed limit must be between 1 and 1000 commits")
        since = self.checked_revision(since)
        return [self.commits[rev] for rev in sorted(self.commits) if rev > since][:limit]

    # -- persistence probe -----------------------------------------------------

    def watermarks(self):
        return {"head": self.head, "floor": self.floor}
