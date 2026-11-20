"""Differential runner: real strataleaf.Database vs. the independent model.

Each operation is executed against both sides and the normalized outcomes are
compared with type-strict equality. The real side always goes through the
production API and a real SQLite file on disk; the model side only computes
the documented semantics. Cursors are opaque and side-specific, so the runner
keeps per-side cursor slots and compares only cursor presence/absence plus
everything the cursor protects (page contents, revisions, rejections).

Symbolic references in operations are resolved per side against that side's
own observed state:

* revisions: head / head+offset / floor / floor+offset / literal / named pin /
  default(None) / deliberately invalid bool;
* expectations: none / zero / literal / current version of the key / invalid
  bool / negative.

If a reference dangles (e.g. its producing operation was removed while
shrinking) the operation is skipped symmetrically on both sides.
"""

import os
import tempfile
from dataclasses import dataclass, field as dataclass_field

from strataleaf import Database
from strataleaf.errors import DatabaseError

from .gen import FOREIGN_SLOT, SCHEMA_ALPHA
from . import model as model_oracle
from .model import Model


class Skip(Exception):
    """Raised when a symbolic reference cannot be resolved on this side."""


SKIP_OUTCOME = {"skipped": True}


def decode_tags(value):
    """Materialize tagged non-JSON floats ({"@float": "nan"}) for execution."""
    if isinstance(value, dict):
        if set(value) == {"@float"}:
            return float(value["@float"])
        return {key: decode_tags(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_tags(item) for item in value]
    return value


def strict_equal(left, right):
    """Type-strict structural equality (int 1 and float 1.0 differ here)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(strict_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(strict_equal(a, b) for a, b in zip(left, right))
    return left == right


def resolve_rev(spec, head, floor, pins):
    if spec is None:
        return None
    kind = spec["kind"]
    if kind == "default":
        return None
    if kind == "head":
        return head
    if kind == "head_offset":
        return head + spec["offset"]
    if kind == "floor":
        return floor
    if kind == "floor_offset":
        return floor + spec["offset"]
    if kind == "literal":
        return spec["value"]
    if kind == "bool":
        return True
    if kind == "pin":
        if spec["name"] not in pins:
            raise Skip("unknown pin " + spec["name"])
        return pins[spec["name"]]
    raise AssertionError("unknown revision spec " + kind)


def resolve_expected(spec, current):
    if spec is None:
        return None
    kind = spec["kind"]
    if kind == "none":
        return None
    if kind == "zero":
        return 0
    if kind == "literal":
        return spec["value"]
    if kind == "current":
        return current
    if kind == "bool":
        return True
    if kind == "negative":
        return -1
    raise AssertionError("unknown expected spec " + kind)


def record_norm(rec):
    if rec is None:
        return None
    return {"collection": rec["collection"], "key": rec["key"], "revision": rec["revision"],
            "document": rec["document"], "expires_at": rec["expires_at"]}


@dataclass
class Divergence:
    index: int
    op: dict
    real: dict
    model: dict
    transcript: list = dataclass_field(default_factory=list)
    note: str = ""

    def signature(self):
        def kind(outcome):
            if "error" in outcome:
                return "error:" + outcome["error"]
            if "ok" in outcome:
                return "ok"
            return "other"
        return (self.op.get("op"), kind(self.real), kind(self.model))


class RealContext:
    def __init__(self, path):
        self.path = path
        self.db = Database(path)
        self.head = 0
        self.floor = 0
        self.pins = {}
        self.generations = {}
        self.last_written = {}
        self.last_kind = {}
        self.slots = {}
        self.inspect_issues = []

    def reopen(self):
        self.db.close()
        self.db = Database(self.path)

    def close(self):
        self.db.close()


class ModelContext:
    def __init__(self):
        self.model = Model()
        self.slots = {}


def mint_foreign_cursor(workdir):
    """Create a scratch database pair and mint one continuation cursor from it."""
    path = os.path.join(workdir, "scratch.db")
    seed_docs = [{"region": "x", "seq": i} for i in range(3)]
    db = Database(path)
    db.catalog.create("alpha", SCHEMA_ALPHA)
    batch = db.batch()
    for i, doc in enumerate(seed_docs):
        batch.put("alpha", "f%d" % i, doc)
    batch.commit()
    page = db.query("alpha", {"field": "seq", "op": "ge", "value": -100}, limit=1)
    token = page.cursor
    db.close()
    model = Model(database_id="model-scratch")
    model.create_collection("alpha", SCHEMA_ALPHA)
    model.commit([{"kind": "put", "collection": "alpha", "key": "f%d" % i,
                   "document": doc, "expected": None, "expires_at": None}
                  for i, doc in enumerate(seed_docs)])
    model_token = model.query("alpha", {"field": "seq", "op": "ge", "value": -100},
                              limit=1)["cursor"]
    assert token and model_token
    return token, model_token


# ---------------------------------------------------------------------------
# real side

def _real_commit(rctx, op):
    resolved = []
    for raw in op["ops"]:
        current = rctx.last_written.get((raw["collection"], raw["key"]), 0)
        expected = resolve_expected(raw.get("expected"), current)
        resolved.append((raw, expected))
    try:
        batch = rctx.db.batch(op.get("request_key"))
        for raw, expected in resolved:
            if raw["kind"] == "put":
                batch.put(raw["collection"], raw["key"], raw["document"],
                          expected=expected, expires_at=raw.get("expires_at"))
            else:
                batch.delete(raw["collection"], raw["key"], expected=expected)
        result = batch.commit()
        return {"ok": {"revision": result.revision, "count": result.count,
                       "replayed": result.replayed}}
    except DatabaseError as exc:
        return {"error": type(exc).__name__}


def _real_query(rctx, op):
    try:
        revision = resolve_rev(op.get("revision"), rctx.head, rctx.floor, rctx.pins)
    except Skip:
        return SKIP_OUTCOME
    token = None
    if op.get("cursor_slot") is not None:
        if op["cursor_slot"] not in rctx.slots:
            return SKIP_OUTCOME
        token = rctx.slots[op["cursor_slot"]]
    try:
        page = rctx.db.query(op["collection"], op["predicate"], limit=op["limit"],
                             cursor=token, revision=revision,
                             force_scan=op.get("force_scan", False))
    except DatabaseError as exc:
        if op.get("save_slot") is not None:
            rctx.slots.pop(op["save_slot"], None)
        return {"error": type(exc).__name__}
    if op.get("save_slot") is not None:
        if page.cursor:
            rctx.slots[op["save_slot"]] = page.cursor
        else:
            rctx.slots.pop(op["save_slot"], None)
    return {"ok": {"records": [record_norm(rec.__dict__) for rec in page.records],
                   "revision": page.revision, "has_cursor": page.cursor is not None,
                   "plan": page.plan}}


def execute_real(rctx, op):
    kind = op["op"]
    try:
        if kind == "create_collection":
            return _call(lambda: rctx.db.catalog.create(op["name"], op.get("schema")))
        if kind == "describe":
            return _call(lambda: rctx.db.catalog.describe(op["collection"]))
        if kind == "catalog_list":
            return _call(lambda: rctx.db.catalog.list())
        if kind == "evolve":
            if op["generation"]["kind"] == "current":
                if op["name"] not in rctx.generations:
                    return SKIP_OUTCOME
                generation = rctx.generations[op["name"]]
            else:
                generation = op["generation"]["value"]
            return _call(lambda: rctx.db.catalog.evolve(op["name"], op["schema"], generation))
        if kind == "commit":
            return _real_commit(rctx, op)
        if kind == "get":
            try:
                revision = resolve_rev(op["revision"], rctx.head, rctx.floor, rctx.pins)
            except Skip:
                return SKIP_OUTCOME
            try:
                rec = rctx.db.get(op["collection"], op["key"], revision)
            except DatabaseError as exc:
                return {"error": type(exc).__name__}
            return {"ok": record_norm(rec.__dict__) if rec is not None else None}
        if kind == "query":
            return _real_query(rctx, op)
        if kind == "index_create":
            return _call(lambda: rctx.db.indexes.create(op["collection"], op["name"],
                                                        op["field"], op["unique"]))
        if kind == "index_drop":
            return _call(lambda: rctx.db.indexes.drop(op["collection"], op["name"]))
        if kind == "index_list":
            return _call(lambda: rctx.db.indexes.list(op["collection"]))
        if kind == "pin":
            try:
                revision = resolve_rev(op["revision"], rctx.head, rctx.floor, rctx.pins)
            except Skip:
                return SKIP_OUTCOME
            return _call(lambda: rctx.db.retention.pin(op["name"], revision))
        if kind == "unpin":
            return _call(lambda: rctx.db.retention.unpin(op["name"]))
        if kind == "compact":
            try:
                target = resolve_rev(op["target"], rctx.head, rctx.floor, rctx.pins)
            except Skip:
                return SKIP_OUTCOME
            return _call(lambda: rctx.db.retention.compact(target))
        if kind == "sweep":
            return _call(lambda: rctx.db.retention.sweep(op["now"], op.get("limit", 1000)))
        if kind == "patch":
            current = rctx.last_written.get((op["collection"], op["key"]), 0)
            expected = resolve_expected(op.get("expected"), current)
            return _call(lambda: _commit_result(rctx.db.patch(op["collection"], op["key"],
                                                              op["patch"], expected=expected)))
        if kind == "feed_register":
            try:
                since = resolve_rev(op.get("since"), rctx.head, rctx.floor, rctx.pins)
            except Skip:
                return SKIP_OUTCOME
            return _call(lambda: rctx.db.feed.register(op["name"], since))
        if kind == "feed_ack":
            try:
                revision = resolve_rev(op["revision"], rctx.head, rctx.floor, rctx.pins)
            except Skip:
                return SKIP_OUTCOME
            return _call(lambda: rctx.db.feed.acknowledge(op["name"], revision))
        if kind == "feed_release":
            return _call(lambda: rctx.db.feed.release(op["name"]))
        if kind == "feed_position":
            return _call(lambda: rctx.db.feed.position(op["name"]))
        if kind == "feed_read":
            try:
                since = resolve_rev(op["since"], rctx.head, rctx.floor, rctx.pins)
            except Skip:
                return SKIP_OUTCOME
            return _call(lambda: rctx.db.feed.read(since, op.get("limit", 100)))
        if kind == "reopen":
            rctx.reopen()
            report = rctx.db.diagnostics.inspect()
            return {"ok": {"head": rctx.db.revision, "floor": report["floor"]}}
        if kind == "inspect":
            report = rctx.db.diagnostics.inspect(deep=True)
            rctx.inspect_issues = report["issues"]
            return {"ok": {"ok": report["ok"], "head": report["head"], "floor": report["floor"]}}
    except DatabaseError as exc:
        return {"error": type(exc).__name__}
    except Skip:
        return SKIP_OUTCOME
    except Exception as exc:  # a production crash is a first-class finding
        return {"crash": type(exc).__name__ + ": " + str(exc)}
    raise AssertionError("unknown op " + kind)


def _commit_result(result):
    return {"revision": result.revision, "count": result.count, "replayed": result.replayed}


def _call(func):
    try:
        value = func()
    except DatabaseError as exc:
        return {"error": type(exc).__name__}
    return {"ok": value}


# ---------------------------------------------------------------------------
# model side

def execute_model(mctx, op):
    model = mctx.model
    model.last_error_kind = None
    kind = op["op"]
    try:
        if kind == "create_collection":
            return {"ok": model.create_collection(op["name"], op.get("schema"))}
        if kind == "describe":
            return {"ok": model.describe(op["collection"])}
        if kind == "catalog_list":
            return {"ok": model.catalog_list()}
        if kind == "evolve":
            if op["generation"]["kind"] == "current":
                if op["name"] not in model.collections:
                    return SKIP_OUTCOME
                generation = model.collections[op["name"]]["generation"]
            else:
                generation = op["generation"]["value"]
            return {"ok": model.evolve(op["name"], op["schema"], generation)}
        if kind == "commit":
            ops = []
            for raw in op["ops"]:
                current = model.current_rev(raw["collection"], raw["key"])
                expected = resolve_expected(raw.get("expected"), current)
                item = {"kind": raw["kind"], "collection": raw["collection"],
                        "key": raw["key"], "expected": expected,
                        "expires_at": raw.get("expires_at")}
                if raw["kind"] == "put":
                    item["document"] = raw["document"]
                ops.append(item)
            return {"ok": model.commit(ops, op.get("request_key"))}
        if kind == "get":
            revision = resolve_rev(op["revision"], model.head, model.floor, model.pins)
            return {"ok": model.get(op["collection"], op["key"], revision)}
        if kind == "query":
            revision = resolve_rev(op.get("revision"), model.head, model.floor, model.pins)
            token = None
            if op.get("cursor_slot") is not None:
                if op["cursor_slot"] not in mctx.slots:
                    return SKIP_OUTCOME
                token = mctx.slots[op["cursor_slot"]]
            try:
                page = model.query(op["collection"], op["predicate"], limit=op["limit"],
                                   cursor=token, revision=revision,
                                   force_scan=op.get("force_scan", False))
            except model_oracle.DatabaseError:
                if op.get("save_slot") is not None:
                    mctx.slots.pop(op["save_slot"], None)
                raise
            if op.get("save_slot") is not None:
                if page["cursor"]:
                    mctx.slots[op["save_slot"]] = page["cursor"]
                else:
                    mctx.slots.pop(op["save_slot"], None)
            return {"ok": {"records": page["records"], "revision": page["revision"],
                           "has_cursor": page["cursor"] is not None, "plan": page["plan"]}}
        if kind == "index_create":
            return {"ok": model.index_create(op["collection"], op["name"],
                                             op["field"], op["unique"])}
        if kind == "index_drop":
            return {"ok": model.index_drop(op["collection"], op["name"])}
        if kind == "index_list":
            return {"ok": model.index_list(op["collection"])}
        if kind == "pin":
            revision = resolve_rev(op["revision"], model.head, model.floor, model.pins)
            return {"ok": model.pin(op["name"], revision)}
        if kind == "unpin":
            return {"ok": model.unpin(op["name"])}
        if kind == "compact":
            target = resolve_rev(op["target"], model.head, model.floor, model.pins)
            return {"ok": model.compact(target)}
        if kind == "sweep":
            return {"ok": model.sweep(op["now"], op.get("limit", 1000))}
        if kind == "patch":
            current = model.current_rev(op["collection"], op["key"])
            expected = resolve_expected(op.get("expected"), current)
            return {"ok": model.patch(op["collection"], op["key"], op["patch"],
                                      expected=expected)}
        if kind == "feed_register":
            since = resolve_rev(op.get("since"), model.head, model.floor, model.pins)
            return {"ok": model.feed_register(op["name"], since)}
        if kind == "feed_ack":
            revision = resolve_rev(op["revision"], model.head, model.floor, model.pins)
            return {"ok": model.feed_ack(op["name"], revision)}
        if kind == "feed_release":
            return {"ok": model.feed_release(op["name"])}
        if kind == "feed_position":
            return {"ok": model.feed_position(op["name"])}
        if kind == "feed_read":
            since = resolve_rev(op["since"], model.head, model.floor, model.pins)
            return {"ok": model.feed_read(since, op.get("limit", 100))}
        if kind == "reopen":
            return {"ok": model.watermarks()}
        if kind == "inspect":
            return {"ok": {"ok": True, **model.watermarks()}}
    except model_oracle.DatabaseError as exc:
        return {"error": type(exc).__name__}
    except Skip:
        return SKIP_OUTCOME
    raise AssertionError("unknown op " + kind)


# ---------------------------------------------------------------------------
# statistics

class Stats:
    FIELDS = [
        "ops", "skipped", "commits", "commits_ok", "commits_replayed",
        "cross_collection_commits", "tombstone_revivals", "stale_expected_conflicts",
        "unique_conflicts", "unique_build_conflicts", "schema_rejections",
        "request_key_conflicts", "unknown_collection_commits", "empty_or_invalid_batches",
        "gets", "get_hits", "get_misses", "snapshot_too_old",
        "queries", "pages", "pages_via_index", "pages_via_scan", "continuations",
        "cursor_rejections", "foreign_cursor_rejections", "predicate_rejections",
        "index_creates", "index_drops", "pins", "unpins", "compactions",
        "compactions_blocked", "compactions_advanced", "compaction_conflicts",
        "sweeps", "sweeps_expired", "patches", "patch_conflicts", "feed_reads",
        "feed_registers", "feed_acks", "reopens", "evolves", "inspects_ok",
    ]

    ATTEMPTS = {"commit": "commits", "get": "gets", "query": "queries"}

    def __init__(self):
        self.counts = {name: 0 for name in self.FIELDS}

    def bump(self, name, amount=1):
        self.counts[name] += amount

    def merge(self, other):
        for name, value in other.counts.items():
            self.counts[name] += value

    def as_dict(self):
        return dict(self.counts)


def _update_stats(stats, rctx, mctx, op, outcome):
    kind = op["op"]
    stats.bump("ops")
    if "skipped" in outcome:
        stats.bump("skipped")
        return
    if kind in Stats.ATTEMPTS:
        stats.bump(Stats.ATTEMPTS[kind])
    error_kind = mctx.model.last_error_kind
    if "error" in outcome:
        name = outcome["error"]
        if name == "SnapshotTooOld":
            stats.bump("snapshot_too_old")
        elif name == "InvalidCursor":
            stats.bump("cursor_rejections")
            if op.get("cursor_slot") == FOREIGN_SLOT:
                stats.bump("foreign_cursor_rejections")
        elif name == "InvalidDocument" and kind == "query":
            stats.bump("predicate_rejections")
        elif name == "Conflict" and kind == "commit":
            if error_kind == "expected_conflict":
                stats.bump("stale_expected_conflicts")
            elif error_kind == "unique_conflict":
                stats.bump("unique_conflicts")
            elif error_kind == "request_key_conflict":
                stats.bump("request_key_conflicts")
        elif name == "NotFound" and kind == "commit":
            stats.bump("unknown_collection_commits")
        elif name == "Conflict" and kind == "compact":
            stats.bump("compaction_conflicts")
        elif name == "Conflict" and kind == "index_create" \
                and error_kind == "unique_build_conflict":
            stats.bump("unique_build_conflicts")
        elif name == "Conflict" and kind == "patch":
            stats.bump("patch_conflicts")
        elif name == "InvalidDocument" and kind == "commit":
            if error_kind == "schema_invalid":
                stats.bump("schema_rejections")
            else:
                stats.bump("empty_or_invalid_batches")
        return
    if "crash" in outcome:
        return
    value = outcome["ok"]
    if kind == "commit":
        stats.bump("commits_ok")
        if value["replayed"]:
            stats.bump("commits_replayed")
            return
        collections = {raw["collection"] for raw in op["ops"]}
        if len(collections) > 1:
            stats.bump("cross_collection_commits")
        for raw in op["ops"]:
            identity = (raw["collection"], raw["key"])
            if raw["kind"] == "put" and rctx.last_kind.get(identity) == "delete":
                stats.bump("tombstone_revivals")
    elif kind == "get":
        stats.bump("get_hits" if value is not None else "get_misses")
    elif kind == "query":
        stats.bump("pages")
        stats.bump("pages_via_index" if value["plan"]["access"] == "equality_index"
                   else "pages_via_scan")
        if op.get("cursor_slot") is not None:
            stats.bump("continuations")
    elif kind == "index_create":
        stats.bump("index_creates")
    elif kind == "index_drop":
        stats.bump("index_drops")
    elif kind == "pin":
        stats.bump("pins")
    elif kind == "unpin":
        stats.bump("unpins")
    elif kind == "compact":
        stats.bump("compactions")
        stats.bump("compactions_blocked" if value["blocked"] else "compactions_advanced")
    elif kind == "sweep":
        stats.bump("sweeps")
        if value["expired"]:
            stats.bump("sweeps_expired")
    elif kind == "patch":
        stats.bump("patches")
    elif kind == "feed_read":
        stats.bump("feed_reads")
    elif kind == "feed_register":
        stats.bump("feed_registers")
    elif kind == "feed_ack":
        stats.bump("feed_acks")
    elif kind == "reopen":
        stats.bump("reopens")
    elif kind == "evolve":
        stats.bump("evolves")
    elif kind == "inspect":
        if value["ok"]:
            stats.bump("inspects_ok")


def _track(rctx, mctx, op, outcome):
    """Update the real-side mirrors after an agreed successful outcome."""
    if "ok" not in outcome:
        return
    value = outcome["ok"]
    kind = op["op"]
    if kind == "commit":
        if value["replayed"]:
            return
        rctx.head = value["revision"]
        for raw in op["ops"]:
            identity = (raw["collection"], raw["key"])
            rctx.last_written[identity] = value["revision"]
            rctx.last_kind[identity] = raw["kind"]
    elif kind == "patch":
        rctx.head = value["revision"]
        identity = (op["collection"], op["key"])
        rctx.last_written[identity] = value["revision"]
        rctx.last_kind[identity] = "put"
    elif kind == "sweep" and value["revision"] is not None:
        rctx.head = value["revision"]
        for identity in mctx.model.last_sweep_keys:
            rctx.last_written[identity] = value["revision"]
            rctx.last_kind[identity] = "delete"
    elif kind == "compact":
        rctx.floor = value["floor"]
    elif kind == "pin":
        rctx.pins[op["name"]] = value
    elif kind == "unpin":
        rctx.pins.pop(op["name"], None)
    elif kind == "create_collection":
        rctx.generations[op["name"]] = 1
    elif kind == "evolve":
        rctx.generations[op["name"]] = value
    elif kind == "reopen":
        rctx.head, rctx.floor = value["head"], value["floor"]


# ---------------------------------------------------------------------------
# main loop

def run_ops(ops, workdir, stats=None):
    """Execute one scenario. Returns a Divergence or None."""
    rctx = RealContext(os.path.join(workdir, "main.db"))
    mctx = ModelContext()
    transcript = []
    try:
        real_foreign, model_foreign = mint_foreign_cursor(workdir)
        rctx.slots[FOREIGN_SLOT] = real_foreign
        mctx.slots[FOREIGN_SLOT] = model_foreign
        for index, original in enumerate(ops):
            op = decode_tags(original)
            real = execute_real(rctx, op)
            model = execute_model(mctx, op)
            if stats is not None:
                _update_stats(stats, rctx, mctx, op, real)
            if ("skipped" in real) != ("skipped" in model) or not strict_equal(real, model):
                note = ""
                if op.get("op") == "inspect" and rctx.inspect_issues:
                    note = "inspect issues: " + "; ".join(rctx.inspect_issues)
                return Divergence(index, original, real, model,
                                  transcript=list(transcript), note=note)
            _track(rctx, mctx, op, real)
            transcript.append((index, original, real))
        return None
    finally:
        rctx.close()


def run_scenario(ops, stats=None):
    """Run one scenario in a fresh temporary directory."""
    with tempfile.TemporaryDirectory(prefix="strataleaf-verify-") as workdir:
        return run_ops(ops, workdir, stats)
