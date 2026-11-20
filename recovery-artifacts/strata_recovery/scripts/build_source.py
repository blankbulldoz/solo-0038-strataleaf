"""Build the real source library (temporary DB) with the required input timeline.

Uses only the existing strataleaf public API. Produces, under the data dir:
  source/source.db  - the source library (must stay unchanged afterwards)
  timeline.json     - the full input timeline (ground truth for independent checks)
  requests.json     - receiver requests listed by deletion event
                      (collection/key/delete commit revision only; no content versions)

Timeline coverage required by the task:
  * same key deleted and recreated multiple times      -> docs/A (3 deletes, 4 lifecycles)
  * content changed back to the original value         -> docs/A rev 17 == rev 9 body
  * identical bodies across different lifecycles       -> docs/A life1-last (rev 10) == life2 (rev 13)
  * key-only references in another collection          -> refs/*
  * a section of already recycled history              -> retention.compact(6), commits 1..6 gone
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")

from strataleaf import Database

DOCS_SCHEMA = {"fields": {"text": "string", "seq": "integer", "n": "integer"}, "required": ["text"]}
REFS_SCHEMA = {"fields": {"target": "string", "note": "string"}, "required": ["target"]}

# (op, collection, key, body, expected) - expected makes every commit self-describing:
# expected=0 proves "key never appeared before"; expected=<rev> chains to the prior version.
TIMELINE = [
    ("put", "docs", "C", {"text": "gamma-1", "seq": 1}, 0),       # rev 1   C life 1
    ("put", "docs", "C", {"text": "gamma-2", "seq": 2}, 1),       # rev 2
    ("del", "docs", "C", None, 2),                                # rev 3   delete C #1  -> recycled
    ("put", "refs", "rc", {"target": "C", "note": "rc"}, 0),      # rev 4   commit -> recycled
    ("put", "docs", "C", {"text": "gamma-3", "seq": 3}, 3),       # rev 5   C life 2 (live at head)
    ("put", "refs", "rd", {"target": "D", "note": "rd"}, 0),      # rev 6   commit -> recycled
    # --- retention.compact(6) here: commits 1..6 recycled, floor = 6 ---
    ("put", "docs", "D", {"text": "delta-1"}, 0),                 # rev 7   D life 1
    ("del", "docs", "D", None, 7),                                # rev 8   delete D #1
    ("put", "docs", "A", {"text": "alpha-1", "n": 1}, 0),         # rev 9   A life 1
    ("put", "docs", "A", {"text": "alpha-2", "n": 2}, 9),         # rev 10
    ("put", "refs", "r1", {"target": "A", "note": "r1"}, 0),      # rev 11  ref while A in life 1
    ("del", "docs", "A", None, 10),                               # rev 12  delete A #1
    ("put", "docs", "A", {"text": "alpha-2", "n": 2}, 12),        # rev 13  A life 2, body identical to life 1 last
    ("put", "refs", "r2", {"target": "A", "note": "r2"}, 0),      # rev 14  ref while A in life 2
    ("del", "docs", "A", None, 13),                               # rev 15  delete A #2
    ("put", "docs", "A", {"text": "alpha-3", "n": 3}, 15),        # rev 16  A life 3
    ("put", "docs", "A", {"text": "alpha-1", "n": 1}, 16),        # rev 17  content back to the original value
    ("del", "docs", "A", None, 17),                               # rev 18  delete A #3
    ("put", "docs", "A", {"text": "alpha-current", "n": 4}, 18),  # rev 19  A life 4 (live at head)
    ("put", "refs", "r3", {"target": "A", "note": "r3"}, 0),      # rev 20  ref while A in life 4 (live)
    ("put", "refs", "r4", {"target": "B", "note": "r4"}, 0),      # rev 21  ref to never-existing B
    ("del", "docs", "B", None, 0),                                # rev 22  delete of never-existing B
    ("put", "refs", "r5", {"target": "D", "note": "r5"}, 0),      # rev 23  ref while D tombstoned
]
COMPACT_TARGET = 6

REQUESTS = [
    {"id": "req-1", "collection": "docs", "key": "C", "delete_revision": 3},
    {"id": "req-2", "collection": "docs", "key": "D", "delete_revision": 8},
    {"id": "req-3", "collection": "docs", "key": "A", "delete_revision": 12},
    {"id": "req-4", "collection": "docs", "key": "A", "delete_revision": 15},
    {"id": "req-5", "collection": "docs", "key": "A", "delete_revision": 18},
    {"id": "req-6", "collection": "docs", "key": "B", "delete_revision": 22},
    {"id": "req-7", "collection": "docs", "key": "A", "delete_revision": 21},
]


def main(data_dir):
    data = Path(data_dir)
    source_dir = data / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    db_path = source_dir / "source.db"
    if db_path.exists():
        raise SystemExit("source db already exists: " + str(db_path))

    events = []
    with Database(db_path) as db:
        db.catalog.create("docs", DOCS_SCHEMA)
        db.catalog.create("refs", REFS_SCHEMA)
        for index, (op, collection, key, body, expected) in enumerate(TIMELINE, start=1):
            if index == 7:  # recycle the first section of history before rev 7
                outcome = db.retention.compact(COMPACT_TARGET)
                if outcome["floor"] != COMPACT_TARGET or outcome["blocked"]:
                    raise SystemExit("unexpected compact outcome: " + json.dumps(outcome))
            batch = db.batch()
            if op == "put":
                batch.put(collection, key, body, expected=expected)
            else:
                batch.delete(collection, key, expected=expected)
            result = batch.commit()
            if result.revision != index:
                raise SystemExit(f"revision drift: expected {index}, got {result.revision}")
            events.append({"rev": index, "op": "put" if op == "put" else "delete",
                           "collection": collection, "key": key, "body": body, "expected": expected})
        state = db.diagnostics.inspect(deep=True)

    timeline = {"collections": {"docs": DOCS_SCHEMA, "refs": REFS_SCHEMA},
                "compact_target": COMPACT_TARGET, "floor": state["floor"], "head": state["head"],
                "events": events}
    (data / "timeline.json").write_text(json.dumps(timeline, indent=2, ensure_ascii=False) + "\n")
    (data / "requests.json").write_text(json.dumps({"requests": REQUESTS}, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"source_db": str(db_path), "head": state["head"], "floor": state["floor"],
                      "events": len(events), "requests": len(REQUESTS)}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/strata_recovery/data")
