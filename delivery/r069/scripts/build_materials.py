"""Build real StrataLeaf handoff materials at three points in time.

This is a one-off handoff preparation script, not a diagnostics tool.
It uses only the public strataleaf API (Database / archives / replication /
transfer / catalog / indexes / feed / retention). After the run the source
database is closed and never opened for writing again.

Layout is created fresh: each material set lands in its own new, empty
directory. Run with: python3 -I build_materials.py <root_dir> <package_root>
"""
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])

from strataleaf import Database, Archives


def read_database_id(path):
    uri = Path(path).resolve().as_uri() + "?immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        return con.execute("SELECT value FROM meta WHERE key='database_id'").fetchone()[0]
    finally:
        con.close()


SCHEMA_V1 = {
    "fields": {"region": "string", "sequence": "integer", "code": "string"},
    "required": ["region", "sequence"],
}
SCHEMA_V2 = {
    "fields": {"region": "string", "sequence": "integer", "code": "string",
               "zone": "string"},
    "required": ["region", "sequence"],
}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2),
                    encoding="utf-8")


def main(root):
    root = Path(root).resolve()
    if root.exists():
        shutil.rmtree(root)
    live_dir = root / "live_source"
    materials = root / "materials"
    for directory in (live_dir, materials / "t1", materials / "t2", materials / "t3"):
        directory.mkdir(parents=True, exist_ok=True)

    ledger = []
    source_path = live_dir / "source_primary.db"

    with Database(source_path) as db:
        # ---- point in time T1: seed history ---------------------------------
        db.catalog.create("segments", SCHEMA_V1)
        seed = db.batch("seed_segments")
        for i in range(4):
            seed.put("segments", f"seg_{i:02}",
                     {"region": "east" if i % 2 == 0 else "west",
                      "sequence": i, "code": f"c{i}"})
        r1 = seed.commit()
        ledger.append({"step": "t1_seed", "revision": r1.revision, "count": r1.count,
                       "replayed": r1.replayed, "request_key": "seed_segments"})

        update = db.batch("t1_update").put(
            "segments", "seg_01",
            {"region": "west", "sequence": 11, "code": "c1"}, expected=r1.revision).commit()
        ledger.append({"step": "t1_update", "revision": update.revision})

        db.feed.register("mirror_a", 0)
        db.feed.register("mirror_b", 0)
        db.retention.pin("long_reader", update.revision)
        t1_head = update.revision

        t1_pack = db.replication.export(0)
        write_json(materials / "t1" / "replication_pack_t1.json", t1_pack)
        t1_export = db.transfer.export_lines("segments")
        write_json(materials / "t1" / "logical_export_segments_t1.json", t1_export)
        t1_backup = db.archives.backup(materials / "t1" / "backup_t1.db")
        write_json(materials / "t1" / "backup_receipt_t1.json", t1_backup)

        # ---- point in time T2: catalog epoch moves without/with head moves ---
        db.indexes.create("segments", "by_region", "region")            # epoch +1, head unchanged
        db.indexes.create("segments", "unique_code", "code", unique=True)
        gen = db.catalog.evolve("segments", SCHEMA_V2, 1)               # epoch +1, head unchanged
        ledger.append({"step": "t2_catalog_only", "schema_generation": gen,
                       "head_after_catalog_only": db.revision})

        t2_doc = db.batch("t2_writes").put(
            "segments", "seg_04",
            {"region": "north", "sequence": 4, "code": "c4", "zone": "z1"}).delete(
            "segments", "seg_03").commit()                                # tombstone for seg_03
        ledger.append({"step": "t2_documents", "revision": t2_doc.revision,
                       "count": t2_doc.count})
        db.feed.acknowledge("mirror_a", t2_doc.revision)                 # mirror_b lags on purpose

        t2_pack = db.replication.export(0)
        write_json(materials / "t2" / "replication_pack_t2.json", t2_pack)
        t2_export = db.transfer.export_lines("segments")
        write_json(materials / "t2" / "logical_export_segments_t2.json", t2_export)
        t2_backup = db.archives.backup(materials / "t2" / "backup_t2.db")
        write_json(materials / "t2" / "backup_receipt_t2.json", t2_backup)
        t2_head = t2_doc.revision

        # ---- point in time T3: index removal, TTL tombstone, compaction -----
        db.indexes.drop("segments", "by_region")                          # epoch +1, head unchanged
        ttl = db.batch("t3_ttl_put").put(
            "segments", "seg_ttl",
            {"region": "south", "sequence": 9, "code": "c9", "zone": "z2"},
            expires_at=1000.0).commit()
        swept = db.retention.sweep(1000)                                  # explicit TTL tombstone commit
        ledger.append({"step": "t3_sweep", **swept})

        db.feed.acknowledge("mirror_a", swept["revision"])
        db.feed.acknowledge("mirror_b", t2_head)
        db.retention.unpin("long_reader")
        compact = db.retention.compact(db.revision)                       # historical baseline truncation
        ledger.append({"step": "t3_compact", **compact})
        t3_head = db.revision

        t3_pack = db.replication.export(compact["floor"])
        write_json(materials / "t3" / "replication_pack_t3.json", t3_pack)
        t3_export = db.transfer.export_lines("segments")
        write_json(materials / "t3" / "logical_export_segments_t3.json", t3_export)
        t3_backup = db.archives.backup(materials / "t3" / "backup_t3.db")
        write_json(materials / "t3" / "backup_receipt_t3.json", t3_backup)

    # ---- independent origins, built only through public restore/transfer ----
    restored_primary_dir = root / "independent_origins" / "restored_primary"
    transferred_dir = root / "independent_origins" / "transferred_origin"
    restored_primary_dir.mkdir(parents=True, exist_ok=True)
    transferred_dir.mkdir(parents=True, exist_ok=True)

    # origin B: restore T3 backup as a NEW primary (new database_id, new cursor
    # secret, pins/consumers cleared) — documented restore path, no byte patching.
    b_restore = Archives.restore(
        materials / "t3" / "backup_t3.db",
        restored_primary_dir / "origin_b.db",
        expected_sha256=t3_backup["sha256"])
    write_json(restored_primary_dir / "restore_receipt_origin_b.json", b_restore)

    # origin C: brand new database that independently replays the same logical
    # commit sequence through public write/transfer APIs. It reaches the same
    # head number and the same current read view, but has its own database_id,
    # cursor secret, commit history and on-disk bytes.
    origin_c_path = transferred_dir / "origin_c.db"
    t1_export = json.loads((materials / "t1" / "logical_export_segments_t1.json").read_text())
    with Database(origin_c_path) as origin_c:
        origin_c.catalog.create("segments", SCHEMA_V1)
        # rev 1: seed the four initial documents via the transfer import path.
        imported = origin_c.transfer.import_lines(
            "segments", t1_export["lines"], request_key="c_seed")
        # rev 2: replay the seg_01 update against its rev-1 predecessor.
        c_r2 = origin_c.batch("c_update").put(
            "segments", "seg_01",
            {"region": "west", "sequence": 11, "code": "c1"}, expected=1).commit()
        # catalog-only moves mirror origin A (no head change).
        origin_c.indexes.create("segments", "by_region", "region")
        origin_c.indexes.create("segments", "unique_code", "code", unique=True)
        origin_c.catalog.evolve("segments", SCHEMA_V2, 1)
        # rev 3: same put + tombstone pair as origin A.
        c_r3 = origin_c.batch("c_t2_writes").put(
            "segments", "seg_04",
            {"region": "north", "sequence": 4, "code": "c4", "zone": "z1"}).delete(
            "segments", "seg_03").commit()
        origin_c.indexes.drop("segments", "by_region")
        # rev 4: TTL document; rev 5: explicit sweep tombstone commit.
        c_r4 = origin_c.batch("c_ttl_put").put(
            "segments", "seg_ttl",
            {"region": "south", "sequence": 9, "code": "c9", "zone": "z2"},
            expires_at=1000.0).commit()
        c_sweep = origin_c.retention.sweep(1000)
        c_receipts = {"seed_import": imported.__dict__, "rev2": c_r2.__dict__,
                      "rev3": c_r3.__dict__, "rev4": c_r4.__dict__,
                      "sweep": c_sweep}
        write_json(transferred_dir / "import_receipt_origin_c.json", c_receipts)
        c_head = origin_c.revision

    write_json(root / "build_ledger.json",
               {"origin_a_database_id": read_database_id(source_path),
                "source_path": str(source_path),
                "heads": {"t1": t1_head, "t2": t2_head, "t3": t3_head,
                          "origin_c_replayed": c_head},
                "steps": ledger})

    # SQLite leaves empty -wal/-shm sidecars next to files opened for backup
    # and restore. They are not handoff materials; remove every sidecar and
    # hidden temporary artifact so each delivery directory contains only the
    # real files. The .db files themselves are self-contained backups.
    removed = []
    for path in root.rglob("*"):
        if path.is_file() and (path.name.endswith(("-wal", "-shm"))
                               or path.name.startswith(".strataleaf")):
            removed.append(str(path.relative_to(root)))
            path.unlink()
    write_json(root / "sidecar_cleanup.json", {"removed": sorted(removed)})
    print(json.dumps({"ok": True, "root": str(root),
                      "heads": {"t1": t1_head, "t2": t2_head, "t3": t3_head},
                      "sidecars_removed": len(removed)}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])