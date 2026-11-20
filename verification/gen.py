"""Seeded generator of verification operation sequences.

Every scenario is a plain JSON-serializable list of operations. The generator
is deterministic per seed and does not observe runtime outcomes; symbolic
revision/expectation references (head, floor, pin, current key version, ...)
are resolved per side at execution time by the runner, so a sequence can be
replayed and shrunk standalone.

Coverage is driven two ways:

* weighted random ops interleave commits, deletes, reads at assorted
  revisions, pagination, index maintenance, pins, compaction, TTL sweeps,
  patches, feed acknowledgements and database reopens;
* scripted pattern clusters guarantee the required situations appear even in
  short runs: cross-collection atomic commits, stale preconditions, tombstone
  reuse, unique-index swap/free cycles, multi-page reads at a pinned revision
  with writes in between, idempotent request-key replay across compaction,
  blocked then advancing reclamation, and schema/missing/null/number/boolean
  edge inputs.
"""

import random

SCHEMA_ALPHA = {
    "fields": {"region": "string", "seq": "integer", "flag": "boolean",
               "score": "number", "code": "string", "note": "string"},
    "required": ["region", "seq"],
}
SCHEMA_BETA = {
    "fields": {"tag": "string", "val": "number", "meta.ts": "integer"},
    "required": ["tag"],
    "additional": False,
}
SCHEMA_GAMMA = {}  # schemaless
SCHEMA_DELTA = {"fields": {"name": "string"}, "required": ["name"]}

KEYS = ["k0", "k1", "k2", "k3", "k4", "k5", "é", "中"]
STRINGS = ["east", "west", "south", "north", "", "alpha", "u0", "u1", "u2",
           "u3", "héllo", "东亚", "x" * 40]
INTS = [0, 1, 2, 3, 5, 8, 13, -1, -7, 42, 2 ** 40]
FLOATS = [0.0, 1.0, 2.5, -3.75, 0.5, 1000000.0]
NUMBERS = INTS + FLOATS
EXPIRES = [None, None, None, 25, 75, 150, 300, 0.5]
NOWS = [0, 50, 100, 200, 1000000000]
PIN_NAMES = ["pin_a", "pin_b"]
CONSUMERS = ["sub_a", "sub_b"]
REQUEST_KEYS = ["req0", "req1", "req2", "req3"]
CURSOR_SLOTS = [0, 1, 2, 3]
FOREIGN_SLOT = 99

INDEXED_VALUES = {
    "code": ["u0", "u1", "u2", "u3"],
    "region": STRINGS[:6],
    "val": [0, 1, 1.0, 2, 2.5, -1],
    "any": [1, 1.0, True, None, "1"],
    "meta.ts": INTS,
    "tag": STRINGS,
    "seq": INTS,
    "note": STRINGS,
    "extra": [None, "text", 1],
    "n": NUMBERS,
    "name": STRINGS,
}


def _float_tag(text):
    return {"@float": text}


class Generator:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.collections = ["alpha", "beta", "gamma"]
        self.generation = {}
        self.request_log = {}      # request_key -> ops used
        self.unique_indexes = []   # (collection, field)
        self.value_indexes = []    # non-unique (collection, field)
        self.slot = 0

    # -- value pools -------------------------------------------------------

    def _string(self):
        return self.rng.choice(STRINGS)

    def _number(self):
        return self.rng.choice(NUMBERS)

    def _scalar(self):
        return self.rng.choice(STRINGS + NUMBERS + [True, False, None])

    def _json_value(self, depth=0):
        choice = self.rng.random()
        if depth >= 3 or choice < 0.55:
            return self._scalar()
        if choice < 0.8:
            return [self._json_value(depth + 1) for _ in range(self.rng.randint(0, 3))]
        return {self.rng.choice(["a", "b", "x", "中"]): self._json_value(depth + 1)
                for _ in range(self.rng.randint(0, 3))}

    def _doc_alpha(self):
        doc = {"region": self.rng.choice(STRINGS[:6]), "seq": self.rng.choice(INTS)}
        if self.rng.random() < 0.5:
            doc["flag"] = self.rng.choice([True, False])
        if self.rng.random() < 0.5:
            doc["score"] = self._number()
        if self.rng.random() < 0.6:
            doc["code"] = self.rng.choice(["u0", "u1", "u2", "u3"])
        if self.rng.random() < 0.3:
            doc["note"] = self._string()
        if self.rng.random() < 0.25:
            doc["extra"] = self.rng.choice([None, [1, "a", True], {"n": 1.0}, "text"])
        return doc

    def _doc_beta(self):
        doc = {"tag": self._string()}
        if self.rng.random() < 0.6:
            doc["val"] = self.rng.choice([0, 1, 1.0, 2, 2.5, -1])
        if self.rng.random() < 0.5:
            doc["meta"] = {"ts": self.rng.choice(INTS), "free": self._scalar()}
        return doc

    def _doc_gamma(self):
        doc = {}
        for _ in range(self.rng.randint(0, 4)):
            doc[self.rng.choice(["any", "tags", "n", "s", "b", "z"])] = self._json_value()
        if self.rng.random() < 0.5:
            doc["any"] = self.rng.choice([1, 1.0, True, "1", None, [1], {"a": 1}])
        return doc

    def _doc_delta(self):
        return {"name": self._string(), "misc": self._json_value()}

    def _doc(self, collection):
        builders = {"alpha": self._doc_alpha, "beta": self._doc_beta,
                    "gamma": self._doc_gamma, "delta": self._doc_delta}
        return builders.get(collection, self._doc_gamma)()

    def _mutate_doc(self, collection, doc):
        """Return a schema-violating variant of a document."""
        doc = dict(doc)
        mutation = self.rng.choice(["drop_required", "null_declared", "bool_for_int",
                                    "float_for_int", "str_for_number", "extra_root",
                                    "deep", "nan"])
        if mutation == "drop_required":
            for required in ({"alpha": "region", "beta": "tag", "delta": "name"}.get(collection),):
                if required:
                    doc.pop(required, None)
            if collection == "gamma":
                doc = 42  # not an object at all
        elif mutation == "null_declared":
            target = {"alpha": "region", "beta": "tag", "delta": "name"}.get(collection)
            if target:
                doc[target] = None
        elif mutation == "bool_for_int":
            doc["seq" if collection == "alpha" else "val"] = True
            if collection == "beta":
                doc["val"] = True
        elif mutation == "float_for_int":
            if collection == "alpha":
                doc["seq"] = 1.5
            else:
                doc["meta"] = {"ts": 2.5}
        elif mutation == "str_for_number":
            doc["score" if collection == "alpha" else "val"] = "loud"
        elif mutation == "extra_root":
            doc["undeclared"] = 1  # only beta rejects this (additional=false)
        elif mutation == "deep":
            nested = leaf = {}
            for _ in range(34):
                leaf["d"] = {}
                leaf = leaf["d"]
            doc["nested"] = nested
        elif mutation == "nan":
            doc["bad_float"] = _float_tag("nan")
        return doc

    # -- symbolic references -------------------------------------------------

    def _rev(self, allow_pin=True):
        choice = self.rng.random()
        if choice < 0.30:
            return {"kind": "head"}
        if choice < 0.45:
            return {"kind": "head_offset", "offset": -self.rng.randint(1, 3)}
        if choice < 0.55:
            return {"kind": "floor"}
        if choice < 0.62:
            return {"kind": "floor_offset", "offset": -1}
        if choice < 0.67 and allow_pin:
            return {"kind": "pin", "name": self.rng.choice(PIN_NAMES)}
        if choice < 0.72:
            return {"kind": "default"}
        if choice < 0.74:
            return {"kind": "bool"}
        return {"kind": "literal", "value": self.rng.randint(0, 8)}

    def _expected(self, collection, key):
        choice = self.rng.random()
        if choice < 0.45:
            return {"kind": "none"}
        if choice < 0.55:
            return {"kind": "current"}
        if choice < 0.65:
            return {"kind": "zero"}
        if choice < 0.68:
            return {"kind": "bool"}
        if choice < 0.71:
            return {"kind": "negative"}
        return {"kind": "literal", "value": self.rng.randint(0, 6)}

    # -- operation builders ----------------------------------------------------

    def _put(self, collection=None, key=None, mutate=False):
        collection = collection or self.rng.choice(self.collections)
        key = key or self.rng.choice(KEYS)
        doc = self._doc(collection)
        if mutate:
            doc = self._mutate_doc(collection, doc)
        op = {"kind": "put", "collection": collection, "key": key, "document": doc,
              "expected": self._expected(collection, key),
              "expires_at": self.rng.choice(EXPIRES)}
        return op

    def _delete(self, collection=None, key=None):
        collection = collection or self.rng.choice(self.collections)
        key = key or self.rng.choice(KEYS)
        return {"kind": "delete", "collection": collection, "key": key,
                "expected": self._expected(collection, key)}

    def _commit_op(self, ops, request_key=None):
        return {"op": "commit", "request_key": request_key, "ops": ops}

    def _random_commit(self):
        roll = self.rng.random()
        if roll < 0.06:
            return self._commit_op([], None)  # empty batch -> InvalidDocument
        size = self.rng.randint(1, 4)
        ops = []
        for _ in range(size):
            if self.rng.random() < 0.68:
                ops.append(self._put(mutate=self.rng.random() < 0.12))
            else:
                ops.append(self._delete())
        if self.rng.random() < 0.05 and len(ops) > 1:
            ops[1] = dict(ops[0])  # duplicate identity -> InvalidDocument
        if self.rng.random() < 0.04:
            ops.append(self._put(collection="nope"))
        request_key = None
        roll = self.rng.random()
        if roll < 0.10 and self.request_log:
            request_key = self.rng.choice(list(self.request_log))
            if self.rng.random() < 0.5:
                ops = self.request_log[request_key]  # exact replay
            else:
                ops = [dict(item, **{"expected": {"kind": "none"}})
                       for item in self.request_log[request_key]][:1]
                ops[0] = dict(ops[0], key=ops[0]["key"] + "_z")  # same key, different ops
        elif roll < 0.22:
            request_key = self.rng.choice(REQUEST_KEYS)
        if request_key is not None:
            self.request_log[request_key] = ops
        return self._commit_op(ops, request_key)

    def _random_get(self):
        return {"op": "get", "collection": self.rng.choice(self.collections + ["nope"]),
                "key": self.rng.choice(KEYS), "revision": self._rev()}

    def _predicate(self, collection):
        rng = self.rng
        simple = [
            {"field": "region", "op": "eq", "value": rng.choice(STRINGS[:5])},
            {"field": "seq", "op": rng.choice(["ge", "lt", "gt", "le"]),
             "value": rng.choice(NUMBERS)},
            {"field": "seq", "op": "eq", "value": rng.choice([1, 1.0, True])},
            {"field": "flag", "op": "eq", "value": rng.choice([True, 1])},
            {"field": "note", "op": "exists", "value": rng.choice([True, False])},
            {"field": "extra", "op": "eq", "value": None},
            {"field": "code", "op": "eq", "value": rng.choice(["u0", "u1", "u2", "u3"])},
            {"field": "code", "op": "in", "value": ["u0", "u2"]},
            {"field": "tag", "op": "eq", "value": rng.choice(STRINGS)},
            {"field": "val", "op": rng.choice(["eq", "le", "gt"]), "value": rng.choice(NUMBERS)},
            {"field": "meta.ts", "op": rng.choice(["le", "ge"]), "value": rng.choice(INTS)},
            {"field": "any", "op": "eq", "value": rng.choice([1, 1.0, True, None, "1"])},
            {"field": "tags", "op": "contains", "value": rng.choice(["a", 1, True])},
            {"field": "region", "op": "contains", "value": "a"},
            {"field": "missing", "op": "ne", "value": 1},
        ]
        roll = rng.random()
        if roll < 0.55:
            return rng.choice(simple)
        if roll < 0.75:
            return {"and": [rng.choice(simple), rng.choice(simple)]}
        if roll < 0.85:
            return {"or": [rng.choice(simple), rng.choice(simple)]}
        if roll < 0.93:
            return {"not": rng.choice(simple)}
        if roll < 0.97:
            return {"field": "seq", "op": "between", "value": 1}  # invalid op
        return {"and": []}  # invalid boolean form

    def _random_query(self):
        roll = self.rng.random()
        if roll < 0.42 and self.rng.random() < 0.75:
            # continue an existing pagination chain
            slot = self.rng.choice(CURSOR_SLOTS + [FOREIGN_SLOT])
            revision = None
            if self.rng.random() < 0.15:
                revision = {"kind": "literal", "value": self.rng.randint(0, 4)}
            return {"op": "query", "collection": self.rng.choice(self.collections),
                    "predicate": self._predicate("alpha"), "limit": self.rng.randint(1, 3),
                    "revision": revision, "cursor_slot": slot, "save_slot": None,
                    "force_scan": False}
        collection = self.rng.choice(self.collections + ["nope"])
        self.slot = (self.slot + 1) % len(CURSOR_SLOTS)
        return {"op": "query", "collection": collection,
                "predicate": self._predicate(collection),
                "limit": self.rng.choice([1, 2, 2, 3, 5, 1000]),
                "revision": self._rev() if self.rng.random() < 0.5 else {"kind": "default"},
                "cursor_slot": None, "save_slot": self.slot,
                "force_scan": self.rng.random() < 0.2}

    def _random_index(self):
        if self.rng.random() < 0.6:
            collection = self.rng.choice(self.collections)
            path = {"alpha": self.rng.choice(["region", "code", "seq", "note", "extra"]),
                    "beta": self.rng.choice(["tag", "val", "meta.ts"]),
                    "gamma": self.rng.choice(["any", "n"]),
                    "delta": "name"}[collection]
            name = "idx_" + path.replace(".", "_")
            unique = self.rng.random() < 0.4
            if unique:
                self.unique_indexes.append((collection, path))
            else:
                self.value_indexes.append((collection, path))
            return {"op": "index_create", "collection": collection, "name": name,
                    "field": path, "unique": unique}
        if self.rng.random() < 0.5:
            return {"op": "index_drop", "collection": self.rng.choice(self.collections),
                    "name": self.rng.choice(["idx_region", "idx_code", "idx_val", "idx_any"])}
        return {"op": "index_list", "collection": self.rng.choice(self.collections)}

    def _random_retention(self):
        roll = self.rng.random()
        if roll < 0.30:
            return {"op": "pin", "name": self.rng.choice(PIN_NAMES), "revision": self._rev(allow_pin=False)}
        if roll < 0.50:
            return {"op": "unpin", "name": self.rng.choice(PIN_NAMES)}
        if roll < 0.85:
            return {"op": "compact", "target": self._rev(allow_pin=False)}
        return {"op": "sweep", "now": self.rng.choice(NOWS + [_float_tag("nan")]),
                "limit": self.rng.randint(1, 5)}

    def _random_patch(self):
        collection = self.rng.choice(self.collections)
        patch = {}
        for _ in range(self.rng.randint(1, 3)):
            field_name = self.rng.choice(["region", "seq", "note", "extra", "flag",
                                          "tag", "val", "any", "nested"])
            patch[field_name] = self.rng.choice([None, self._scalar(),
                                                 {"sub": self._scalar()}])
        expected = None
        roll = self.rng.random()
        if roll < 0.4:
            expected = {"kind": "current"}
        elif roll < 0.55:
            expected = {"kind": "literal", "value": self.rng.randint(0, 5)}
        return {"op": "patch", "collection": collection, "key": self.rng.choice(KEYS),
                "patch": patch, "expected": expected}

    def _random_feed(self):
        roll = self.rng.random()
        name = self.rng.choice(CONSUMERS)
        if roll < 0.25:
            return {"op": "feed_register", "name": name,
                    "since": self._rev(allow_pin=False) if self.rng.random() < 0.6 else None}
        if roll < 0.55:
            return {"op": "feed_ack", "name": name, "revision": self._rev(allow_pin=False)}
        if roll < 0.65:
            return {"op": "feed_release", "name": name}
        if roll < 0.80:
            return {"op": "feed_position", "name": name}
        return {"op": "feed_read", "since": self._rev(allow_pin=False),
                "limit": self.rng.randint(1, 4)}

    # -- scripted pattern clusters --------------------------------------------

    def _pattern_atomic_commit(self):
        """One batch spanning collections; one op carries a stale precondition."""
        key_a, key_b = self.rng.sample(KEYS, 2)
        good = {"kind": "put", "collection": "alpha", "key": key_a,
                "document": self._doc_alpha(), "expected": {"kind": "current"},
                "expires_at": None}
        bad_expected = {"kind": "literal", "value": self.rng.randint(0, 3)}
        ops = [good,
               {"kind": "put", "collection": "beta", "key": key_b,
                "document": self._doc_beta(), "expected": bad_expected,
                "expires_at": None},
               {"kind": "delete", "collection": "gamma", "key": self.rng.choice(KEYS),
                "expected": {"kind": "none"}}]
        self.rng.shuffle(ops)
        return [self._commit_op(ops),
                {"op": "get", "collection": "alpha", "key": key_a, "revision": {"kind": "head"}},
                {"op": "get", "collection": "beta", "key": key_b, "revision": {"kind": "head"}}]

    def _pattern_tombstone(self):
        key = self.rng.choice(KEYS)
        coll = self.rng.choice(self.collections)
        return [
            self._commit_op([{"kind": "put", "collection": coll, "key": key,
                              "document": self._doc(coll), "expected": {"kind": "none"},
                              "expires_at": None}]),
            self._commit_op([{"kind": "delete", "collection": coll, "key": key,
                              "expected": {"kind": "current"}}]),
            # recreate over the tombstone carrying the tombstone revision
            self._commit_op([{"kind": "put", "collection": coll, "key": key,
                              "document": self._doc(coll), "expected": {"kind": "current"},
                              "expires_at": None}]),
            {"op": "get", "collection": coll, "key": key, "revision": {"kind": "head"}},
            {"op": "get", "collection": coll, "key": key,
             "revision": {"kind": "head_offset", "offset": -2}},
            # stale zero precondition on a now-versioned key
            self._commit_op([{"kind": "delete", "collection": coll, "key": key,
                              "expected": {"kind": "zero"}}]),
        ]

    def _pattern_unique(self):
        if not self.unique_indexes:
            return []
        collection, path = self.rng.choice(self.unique_indexes)
        key_a, key_b = self.rng.sample(KEYS, 2)
        if collection == "alpha":
            value = self.rng.choice(["u0", "u1", "u2", "u3"])
            doc_a = dict(self._doc_alpha(), code=value)
            doc_b = dict(self._doc_alpha(), code=value)
        elif collection == "beta":
            value = self.rng.choice([1, 1.0, 2])
            doc_a = dict(self._doc_beta(), val=value)
            doc_b = dict(self._doc_beta(), val=value)
        elif collection == "gamma":
            value = self.rng.choice([1, 1.0, True, None, "dup"])
            doc_a, doc_b = {"any": value}, {"any": value}
        else:
            doc_a = doc_b = {"name": "dup"}
        none = {"kind": "none"}
        current = {"kind": "current"}
        return [
            self._commit_op([{"kind": "put", "collection": collection, "key": key_a,
                              "document": doc_a, "expected": none, "expires_at": None}]),
            # collision with the value held by key_a
            self._commit_op([{"kind": "put", "collection": collection, "key": key_b,
                              "document": doc_b, "expected": none, "expires_at": None}]),
            # free-then-take in one batch succeeds
            self._commit_op([{"kind": "delete", "collection": collection, "key": key_a,
                              "expected": current},
                             {"kind": "put", "collection": collection, "key": key_b,
                              "document": doc_b, "expected": none, "expires_at": None}]),
            # take-before-free in one batch fails atomically
            self._commit_op([{"kind": "put", "collection": collection, "key": key_a,
                              "document": doc_a, "expected": none, "expires_at": None},
                             {"kind": "delete", "collection": collection, "key": key_b,
                              "expected": current}]),
        ]

    def _pattern_pagination(self):
        coll = "alpha"
        keys = self.rng.sample(KEYS, 4)
        seed_commits = [self._commit_op([
            {"kind": "put", "collection": coll, "key": key,
             "document": self._doc_alpha(), "expected": {"kind": "none"},
             "expires_at": None} for key in keys])]
        slot = self.slot = (self.slot + 1) % len(CURSOR_SLOTS)
        predicate = {"field": "seq", "op": "ge", "value": -100}
        return seed_commits + [
            {"op": "query", "collection": coll, "predicate": predicate, "limit": 2,
             "revision": {"kind": "default"}, "cursor_slot": None, "save_slot": slot,
             "force_scan": False},
            # writes land between pages; the continuation keeps the old view
            self._random_commit(),
            {"op": "query", "collection": coll, "predicate": predicate, "limit": 2,
             "revision": None, "cursor_slot": slot, "save_slot": slot, "force_scan": False},
            self._random_commit(),
            {"op": "query", "collection": coll, "predicate": predicate, "limit": 2,
             "revision": None, "cursor_slot": slot, "save_slot": None, "force_scan": False},
        ]

    def _pattern_compaction(self):
        pin = self.rng.choice(PIN_NAMES)
        key = self.rng.choice(KEYS)
        return [
            {"op": "unpin", "name": pin},
            {"op": "pin", "name": pin, "revision": {"kind": "head"}},
            self._random_commit(),
            self._random_commit(),
            {"op": "compact", "target": {"kind": "head"}},   # blocked by the pin
            {"op": "get", "collection": "alpha", "key": key,
             "revision": {"kind": "pin", "name": pin}},        # pinned view still readable
            {"op": "unpin", "name": pin},
            {"op": "compact", "target": {"kind": "head"}},   # advances
            {"op": "get", "collection": "alpha", "key": key,
             "revision": {"kind": "floor_offset", "offset": -1}},  # reclaimed
            {"op": "get", "collection": "alpha", "key": key,
             "revision": {"kind": "floor"}},
        ]

    def _pattern_idempotent(self):
        request_key = self.rng.choice(REQUEST_KEYS)
        ops = [{"kind": "put", "collection": "alpha", "key": self.rng.choice(KEYS),
                "document": self._doc_alpha(), "expected": {"kind": "none"},
                "expires_at": None}]
        mutated = [dict(ops[0], document=self._doc_alpha())]
        self.request_log[request_key] = ops
        return [
            self._commit_op(ops, request_key),
            self._commit_op([dict(item) for item in ops], request_key),   # replay
            self._commit_op(mutated, request_key),                        # key reuse conflict
            {"op": "compact", "target": {"kind": "head"}},
            self._commit_op([dict(item) for item in ops], request_key),   # receipt survives
        ]

    def _pattern_reopen(self):
        slot = self.rng.choice(CURSOR_SLOTS)
        return [
            {"op": "reopen"},
            {"op": "get", "collection": "alpha", "key": self.rng.choice(KEYS),
             "revision": {"kind": "head"}},
            {"op": "query", "collection": "alpha",
             "predicate": {"field": "seq", "op": "ge", "value": -100}, "limit": 2,
             "revision": None, "cursor_slot": slot, "save_slot": None,
             "force_scan": False},
        ]

    def _pattern_sweep(self):
        coll = self.rng.choice(self.collections)
        keys = self.rng.sample(KEYS, 3)
        return [
            self._commit_op([
                {"kind": "put", "collection": coll, "key": key,
                 "document": self._doc(coll), "expected": {"kind": "none"},
                 "expires_at": self.rng.choice([10, 20, 5000])} for key in keys]),
            {"op": "sweep", "now": 100, "limit": 10},
            {"op": "get", "collection": coll, "key": keys[0], "revision": {"kind": "head"}},
            {"op": "get", "collection": coll, "key": keys[0],
             "revision": {"kind": "head_offset", "offset": -1}},
        ]

    def _pattern_schema_edge(self):
        coll = self.rng.choice(["alpha", "beta"])
        ops = [self._commit_op([self._put(collection=coll, mutate=True)])]
        if self.rng.random() < 0.5:
            widened = dict(SCHEMA_ALPHA if coll == "alpha" else SCHEMA_BETA)
            widened = {**widened, "fields": {**widened["fields"], "extra_note": "string"}}
            ops.append({"op": "evolve", "name": coll, "schema": widened,
                        "generation": {"kind": "current"}})
            ops.append(self._commit_op([self._put(collection=coll)]))
        return ops

    def _pattern_foreign_cursor(self):
        return [{"op": "query", "collection": "alpha",
                 "predicate": {"field": "seq", "op": "ge", "value": -100}, "limit": 2,
                 "revision": None, "cursor_slot": FOREIGN_SLOT, "save_slot": None,
                 "force_scan": False}]

    def _pattern_indexed_query(self):
        """Drive the equality-index path and cross-check it against a scan."""
        choices = self.unique_indexes + self.value_indexes
        if not choices:
            return []
        collection, path = self.rng.choice(choices)
        value = self.rng.choice(INDEXED_VALUES.get(path, STRINGS))
        predicate = {"field": path, "op": "eq", "value": value}
        slot = self.slot = (self.slot + 1) % len(CURSOR_SLOTS)
        return [
            {"op": "query", "collection": collection, "predicate": predicate, "limit": 2,
             "revision": {"kind": "head"}, "cursor_slot": None, "save_slot": slot,
             "force_scan": False},
            {"op": "query", "collection": collection, "predicate": predicate, "limit": 2,
             "revision": None, "cursor_slot": slot, "save_slot": None, "force_scan": False},
            {"op": "query", "collection": collection, "predicate": predicate, "limit": 1000,
             "revision": {"kind": "head"}, "cursor_slot": None, "save_slot": None,
             "force_scan": True},
        ]

    def _pattern_feed(self):
        """Register a consumer, acknowledge, and let it block compaction."""
        consumer = self.rng.choice(CONSUMERS)
        return [
            {"op": "feed_release", "name": consumer},
            {"op": "feed_register", "name": consumer, "since": {"kind": "head"}},
            self._random_commit(),
            {"op": "feed_ack", "name": consumer, "revision": {"kind": "head"}},
            {"op": "feed_read", "since": {"kind": "floor"}, "limit": 5},
            {"op": "compact", "target": {"kind": "head"}},
            {"op": "feed_position", "name": consumer},
        ]

    PATTERNS = [
        ("atomic", 8), ("tombstone", 8), ("unique", 7), ("pagination", 8),
        ("compaction", 7), ("idempotent", 5), ("reopen", 5), ("sweep", 5),
        ("schema_edge", 6), ("foreign_cursor", 2), ("indexed_query", 6),
        ("feed", 5),
    ]

    def _pattern(self):
        table = {"atomic": self._pattern_atomic_commit,
                 "tombstone": self._pattern_tombstone,
                 "unique": self._pattern_unique,
                 "pagination": self._pattern_pagination,
                 "compaction": self._pattern_compaction,
                 "idempotent": self._pattern_idempotent,
                 "reopen": self._pattern_reopen,
                 "sweep": self._pattern_sweep,
                 "schema_edge": self._pattern_schema_edge,
                 "foreign_cursor": self._pattern_foreign_cursor,
                 "indexed_query": self._pattern_indexed_query,
                 "feed": self._pattern_feed}
        names = [name for name, _w in self.PATTERNS]
        weights = [w for _name, w in self.PATTERNS]
        return table[self.rng.choices(names, weights=weights, k=1)[0]]()

    # -- scenario assembly -------------------------------------------------------

    def _setup(self):
        ops = [
            {"op": "create_collection", "name": "alpha", "schema": SCHEMA_ALPHA},
            {"op": "create_collection", "name": "beta", "schema": SCHEMA_BETA},
            {"op": "create_collection", "name": "gamma", "schema": SCHEMA_GAMMA},
        ]
        seed_ops = []
        for coll in ("alpha", "beta", "gamma"):
            for key in self.rng.sample(KEYS, 3):
                seed_ops.append({"kind": "put", "collection": coll, "key": key,
                                 "document": self._doc(coll), "expected": {"kind": "none"},
                                 "expires_at": None})
        ops.append(self._commit_op(seed_ops))
        if self.rng.random() < 0.75:
            ops.append({"op": "index_create", "collection": "alpha", "name": "idx_code",
                        "field": "code", "unique": True})
            self.unique_indexes.append(("alpha", "code"))
        if self.rng.random() < 0.5:
            ops.append({"op": "index_create", "collection": "alpha", "name": "idx_region",
                        "field": "region", "unique": False})
            self.value_indexes.append(("alpha", "region"))
        if self.rng.random() < 0.4:
            ops.append({"op": "index_create", "collection": "beta", "name": "idx_val",
                        "field": "val", "unique": True})
            self.unique_indexes.append(("beta", "val"))
        if self.rng.random() < 0.3:
            ops.append({"op": "index_create", "collection": "gamma", "name": "idx_any",
                        "field": "any", "unique": True})
            self.unique_indexes.append(("gamma", "any"))
        if self.rng.random() < 0.25:
            ops.append({"op": "create_collection", "name": "delta", "schema": SCHEMA_DELTA})
            self.collections.append("delta")
        return ops

    def _drain(self):
        """Full-state comparison and cleanup at the end of a scenario."""
        ops = []
        for coll in self.collections:
            ops.append({"op": "query", "collection": coll, "predicate": None, "limit": 1000,
                        "revision": {"kind": "head"}, "cursor_slot": None, "save_slot": None,
                        "force_scan": True})
            ops.append({"op": "query", "collection": coll, "predicate": None, "limit": 1000,
                        "revision": {"kind": "floor"}, "cursor_slot": None, "save_slot": None,
                        "force_scan": True})
            for key in KEYS:
                ops.append({"op": "get", "collection": coll, "key": key,
                            "revision": {"kind": "head"}})
        for name in PIN_NAMES:
            ops.append({"op": "unpin", "name": name})
        for name in CONSUMERS:
            ops.append({"op": "feed_release", "name": name})
        ops.append({"op": "inspect"})
        return ops

    def scenario(self, length):
        ops = self._setup()
        singles = [(self._random_commit, 34), (self._random_get, 14),
                   (self._random_query, 16), (self._random_index, 5),
                   (self._random_retention, 10), (self._random_patch, 6),
                   (self._random_feed, 8),
                   (lambda: {"op": "reopen"}, 3),
                   (lambda: {"op": "describe", "collection": self.rng.choice(self.collections)}, 2),
                   (lambda: {"op": "catalog_list"}, 1)]
        while len(ops) < length:
            if self.rng.random() < 0.30:
                ops.extend(self._pattern())
            else:
                funcs = [f for f, _w in singles]
                weights = [w for _f, w in singles]
                ops.append(self.rng.choices(funcs, weights=weights, k=1)[0]())
        ops.extend(self._drain())
        return ops


def generate(seed, length):
    return Generator(seed).scenario(length)
