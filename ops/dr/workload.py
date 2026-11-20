"""A deterministic, long-running writer used by the drill.

The writer models an embedded service: it owns one ``Database`` handle and
commits small multi-collection batches at a steady rate, occasionally deleting
and re-creating a key, and periodically advancing catalog state (a new index).
It only uses the public StrataLeaf API.
"""

from __future__ import annotations

import random
import threading
import time

from strataleaf import Database

SCHEMA = {
    "fields": {"region": "string", "sequence": "integer", "code": "string", "payload": "object"},
    "required": ["region", "sequence"],
}


class Workload:
    def __init__(self, path: str, prefix: str = "svc", seed: int = 7):
        self.path = path
        self.prefix = prefix
        self.random = random.Random(seed)
        self.db = Database(path)
        self.commits = 0
        self.operations = 0
        self.last_revision = 0
        self.errors = []
        self._deleted = set()

    def bootstrap(self) -> dict:
        db = self.db
        db.catalog.create("orders", SCHEMA)
        db.catalog.create("audit", {"fields": {"action": "string", "order": "string"}, "required": ["action"]})
        batch = db.batch(self.prefix + "-seed")
        for index in range(20):
            key = "%s-%04d" % (self.prefix, index)
            batch.put("orders", key, self._order(index))
            batch.put("audit", "audit-%04d" % index, {"action": "created", "order": key})
        result = batch.commit()
        db.indexes.create("orders", "by_region", "region")
        self.commits, self.operations = 1, 40
        self.last_revision = result.revision
        return {"revision": result.revision, "head": db.revision}

    def _order(self, index: int) -> dict:
        return {
            "region": self.random.choice(["east", "west", "north"]),
            "sequence": index,
            "code": "code-%d" % index,
            "payload": {"attempt": index, "tag": "t%d" % (index % 7)},
        }

    def step(self) -> int:
        db = self.db
        head = db.revision
        key = "%s-%04d" % (self.prefix, self.random.randrange(0, 400))
        choice = self.random.random()
        batch = db.batch()
        if choice < 0.12:
            batch.delete("orders", key)
            batch.put("audit", "audit-%d" % head, {"action": "deleted", "order": key})
        else:
            batch.put("orders", key, self._order(self.random.randrange(0, 100000)), expires_at=None)
            batch.put("audit", "audit-%d" % head, {"action": "updated", "order": key})
        result = batch.commit()
        self.commits += 1
        self.operations += 2
        self.last_revision = result.revision
        return result.revision

    def advance_catalog(self, tag: str) -> dict:
        """Add a uniquely named index; catalog changes move the epoch, not head."""
        name = "idx_%s_%d" % ("".join(ch for ch in tag if ch.isalnum() or ch == "_"), self.db.revision)
        self.db.indexes.create("audit", name[:60], "action")
        return {"index": name[:60], "catalog_epoch": self._epoch()}

    def _epoch(self) -> int:
        with self.db._store.read():
            return int(self.db._store.get("catalog_epoch"))

    def live_documents(self) -> int:
        return sum(len(self.db.query(collection, limit=1000).records)
                   for collection in ("orders", "audit"))

    def close(self):
        self.db.close()


def run_writer(path: str, duration: float | None, state: dict, stop: threading.Event,
               interval: float = 0.02, catalog_every: int = 40, prefix: str = "svc") -> None:
    """Thread body: write until ``stop`` is set or ``duration`` elapses."""
    workload = Workload(path, prefix=prefix)
    state.update({"ready": False, "commits": 0, "errors": [], "revisions": []})
    try:
        state["bootstrap"] = workload.bootstrap()
        state["ready"] = True
        started = time.time()
        while not stop.is_set():
            if duration is not None and time.time() - started >= duration:
                break
            revision = workload.step()
            state["commits"] = workload.commits
            state["operations"] = workload.operations
            state["head"] = revision
            state["revisions"].append(revision)
            if workload.commits % catalog_every == 0:
                state.setdefault("catalog", []).append(workload.advance_catalog("w%d" % workload.commits))
            stop.wait(interval)
    except BaseException as exc:  # noqa: BLE001 - surfaced in the drill report
        state.setdefault("errors", []).append({"error": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        state["final_head"] = workload.last_revision
        state["ready"] = False
        workload.close()
