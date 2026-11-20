"""Independent interruption verification for the backup and replica flows.

This module is *not* a unit test suite (the project keeps automated tests
deferred). It is a re-runnable, self-contained check that drives the real
production API (``Database``, ``Archives``, ``Replication``) against temporary
databases and interrupts real operating-system processes.

Why separate processes matter
-----------------------------
A successful method return or two equal ``revision`` numbers cannot tell
durable bytes from state cached inside the still-running process. Every
verification in this harness is therefore made from a *fresh* process that
opens the files cold, and the strongest observations additionally force a
WAL checkpoint and prove the main database file alone (no ``-wal``/``-shm``
sidecars) carries the same facts. Interrupts are delivered as ``SIGKILL`` to
worker processes; no production module is monkey-patched or instrumented.

What is exercised
-----------------
* commits, catalog changes, segmented replica receive, duplicate packets and
  hard process interruption,
* recovery-time agreement of document contents, tombstones, index projections
  and the contiguous change log against one shared set of source facts,
* bad checksums, missing ranges and pre-existing targets refusing to damage
  the receiving environment (all-or-nothing, no partial files published).

The strongest checks additionally TRUNCATE-checkpoint the WAL into the main
database, copy only the ``.db`` file (no ``-wal``/``-shm`` sidecars) into an
empty directory, and read it cold, so facts held only in the WAL or in an
in-process cache cannot masquerade as durable state.

Run:  python3 -I examples/interruptions.py [--keep DIR]
"""

import argparse
import copy
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# The harness is launched with "python3 -I <file>", where neither the current
# directory nor the script directory is added to sys.path. Insert the
# repository root explicitly so the production package can be imported without
# consulting any caller-controlled environment variable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from strataleaf import (  # noqa: E402  (path bootstrap above)
    Archives,
    Conflict,
    Database,
    DatabaseError,
    ReadOnly,
)
from strataleaf.archive import inspect_archive  # noqa: E402
from strataleaf.codec import digest, loads  # noqa: E402

SCHEMA = {
    "fields": {"region": "string", "sequence": "integer", "code": "string", "payload": "string"},
    "required": ["region", "sequence"],
}
SCHEMA_EVOLVED = {
    "fields": {"region": "string", "sequence": "integer", "code": "string",
               "payload": "string", "note": "string"},
    "required": ["region", "sequence"],
}
POLL_INTERVAL = 0.0005
WORKER_TIMEOUT = 30.0


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def write_json(path, value):
    path = Path(path)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def expect_error(call, *expected_types):
    try:
        call()
    except expected_types as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
    raise AssertionError("operation unexpectedly succeeded")


def arm_killer(pid, trigger=None, delay=None):
    """Schedule SIGKILL for *pid* after a delay or once a file appears."""
    def run():
        if trigger is not None:
            deadline = time.monotonic() + WORKER_TIMEOUT
            while time.monotonic() < deadline and not Path(trigger).exists():
                time.sleep(POLL_INTERVAL)
            if not Path(trigger).exists():
                return
        time.sleep(delay or 0.0)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer = threading.Thread(target=run, daemon=True)
    timer.start()
    return timer


def run_worker(workdir, mode, *args):
    """Run one worker subcommand in a fresh, isolated Python process."""
    command = [sys.executable, "-I", str(Path(__file__).resolve()), mode, *[str(a) for a in args]]
    return subprocess.run(command, cwd=str(workdir), capture_output=True, text=True, timeout=WORKER_TIMEOUT)


def sidecar_files(path):
    path = Path(path)
    return {suffix: (Path(str(path) + suffix).stat().st_size
                     if Path(str(path) + suffix).exists() else None)
            for suffix in ("-wal", "-shm")}


def list_temp_files(directory, prefix):
    return sorted(p.name for p in Path(directory).iterdir() if p.name.startswith(prefix))


def main_temp_files(directory, prefix):
    """Temp databases excluding SQLite -wal/-shm sidecars."""
    return sorted(name for name in list_temp_files(directory, prefix)
                  if not name.endswith(("-wal", "-shm", "-journal")))


# --------------------------------------------------------------------------- #
# Raw observation independent of production read APIs
# --------------------------------------------------------------------------- #

def raw_facts(path):
    """Read every durable fact straight from SQLite in a cold open.

    The connection is opened read-only and never goes through the production
    package, so the result cannot be influenced by any in-process cache. A
    PASSIVE checkpoint is attempted first so that, when no other handle is
    open, the main file itself (not the WAL) holds the observations.
    """
    path = Path(path).resolve()
    sidecars_before = sidecar_files(path)
    checker = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        integrity = checker.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = checker.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        checker.close()

    checkpoint = None
    writer = sqlite3.connect(str(path))
    try:
        checkpoint = tuple(writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
    finally:
        writer.close()

    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        meta = dict(connection.execute("SELECT key,value FROM meta"))
        head, floor = int(meta["head"]), int(meta["floor"])
        collections = [dict(row) for row in
                       connection.execute("SELECT name,schema,generation FROM collections ORDER BY name")]
        index_defs = [tuple(row) for row in
                      connection.execute("SELECT collection,name,field,unique_flag FROM index_defs ORDER BY 1,2")]
        index_values = [tuple(row) for row in
                        connection.execute("SELECT collection,name,token,key FROM index_values ORDER BY 1,2,3,4")]
        visible = {}
        tombstones = {}
        for row in connection.execute("SELECT name FROM collections ORDER BY name"):
            collection = row["name"]
            visible[collection] = []
            tombstones[collection] = []
            rows = connection.execute(
                """SELECT v.key,v.rev,v.body,v.expires FROM versions v JOIN
                   (SELECT key,MAX(rev) AS r FROM versions WHERE collection=? AND rev<=? GROUP BY key) latest
                   ON v.key=latest.key AND v.rev=latest.r ORDER BY v.key""",
                (collection, head)).fetchall()
            for item in rows:
                if item["body"] is None:
                    tombstones[collection].append({"key": item["key"], "rev": item["rev"]})
                else:
                    visible[collection].append({"key": item["key"], "rev": item["rev"],
                                                "document": loads(item["body"]),
                                                "expires_at": item["expires"]})
        versions = [tuple(row) for row in connection.execute(
            "SELECT collection,key,rev,body,expires FROM versions ORDER BY 1,2,3")]
        commit_revs = [row[0] for row in connection.execute("SELECT rev FROM commits ORDER BY rev")]
        receipts = [tuple(row) for row in connection.execute(
            "SELECT request_key,rev,count FROM receipts ORDER BY request_key")]
        counts = {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                  for table in ("collections", "versions", "commits", "index_defs",
                                "index_values", "pins", "consumers", "receipts")}
        contiguous = commit_revs == list(range(floor + 1, head + 1))
    finally:
        connection.close()

    return {
        "path": str(path),
        "meta": meta,
        "head": head,
        "floor": floor,
        "integrity": integrity,
        "foreign_key_issues": [tuple(item) for item in foreign],
        "checkpoint": {"busy": checkpoint[0], "log": checkpoint[1], "checkpointed": checkpoint[2]},
        "sidecars_before": sidecars_before,
        "sidecars_after": sidecar_files(path),
        "collections": collections,
        "index_defs": index_defs,
        "index_values": index_values,
        "visible": visible,
        "tombstones": tombstones,
        "versions": versions,
        "commit_revs": commit_revs,
        "commits_contiguous": contiguous,
        "receipts": receipts,
        "counts": counts,
        "fact_digest": digest({
            "head": head,
            "floor": floor,
            "collections": collections,
            "visible": visible,
            "tombstones": tombstones,
            "index_defs": [list(item) for item in index_defs],
            "index_values": [list(item) for item in index_values],
            "versions": [list(item) for item in versions],
            "commit_revs": commit_revs,
        }),
    }


def cold_inspection(path):
    """Open the database with the production API in a brand-new process."""
    output = Path(path).parent / "observe.json"
    output.unlink(missing_ok=True)
    proc = run_worker(Path(path).parent, "observe", path)
    if proc.returncode != 0:
        raise AssertionError("observer failed: " + proc.stderr)
    return read_json(output)


def main_file_only_facts(path, work_dir):
    """Prove the main .db file alone (no -wal/-shm) carries every fact.

    A PASSIVE checkpoint first folds WAL frames into the main database while
    no production handle is open. The main file is then copied into an empty
    directory with no sidecars and read cold: any fact that lived only in the
    WAL or in an in-process cache would be missing from the copy.
    """
    source = Path(path)
    # Fold the WAL into the main file from a short-lived independent connection.
    checker = sqlite3.connect(str(source))
    try:
        result = tuple(checker.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    finally:
        checker.close()
    if result[0] != 0:
        raise AssertionError("could not exclusively checkpoint the WAL for the main-file proof")
    isolated_dir = Path(work_dir) / ("mainfile_" + source.stem)
    isolated_dir.mkdir(parents=True, exist_ok=True)
    lone = isolated_dir / source.name
    shutil.copyfile(source, lone)
    if list(lone.parent.iterdir()) != [lone]:
        raise AssertionError("main-file proof directory must contain only the copied database")
    facts = raw_facts(lone)
    return {"checkpoint_truncate": {"busy": result[0], "log": result[1],
                                    "checkpointed": result[2]},
            "copied_without_sidecars": True,
            "fact_digest": facts["fact_digest"],
            "head": facts["head"],
            "integrity": facts["integrity"],
            "commits_contiguous": facts["commits_contiguous"]}


# --------------------------------------------------------------------------- #
# Cross-fact assertions over one shared source of truth
# --------------------------------------------------------------------------- #

def assert_same_facts(actual, expected, *, identity_differ=False, allow_empty_indexes=False):
    """Document content, tombstones, indexes and the log must tell one story."""
    for field_name in ("head", "floor", "collections", "visible", "tombstones",
                       "index_defs", "index_values", "versions", "commit_revs",
                       "commits_contiguous"):
        if actual[field_name] != expected[field_name]:
            raise AssertionError("fact divergence on " + field_name
                                 + ": " + repr(actual[field_name])[:200]
                                 + " != " + repr(expected[field_name])[:200])
    # pins/consumers are ephemeral local state and are cleared on primary
    # restore; every durable content table must still match exactly.
    for table in ("collections", "versions", "commits", "index_defs",
                  "index_values", "receipts"):
        if actual["counts"][table] != expected["counts"][table]:
            raise AssertionError("row count divergence on " + table)
    if actual["fact_digest"] != expected["fact_digest"]:
        raise AssertionError("fact digest divergence")
    if actual["integrity"] != "ok" or actual["foreign_key_issues"]:
        raise AssertionError("physical integrity failure in recovered database")
    if not actual["commits_contiguous"]:
        raise AssertionError("recovered change log is not contiguous")
    if not allow_empty_indexes and not actual["index_defs"]:
        raise AssertionError("expected index projections to be installed")
    if identity_differ:
        if actual["meta"]["database_id"] == expected["meta"]["database_id"]:
            raise AssertionError("restored primary kept the source database identity")
        if actual["meta"]["cursor_secret"] == expected["meta"]["cursor_secret"]:
            raise AssertionError("restored primary kept the source cursor secret")
        if actual["meta"]["mode"] != "primary":
            raise AssertionError("restored primary mode is not primary")
    else:
        if actual["meta"]["database_id"] != expected["meta"]["database_id"]:
            raise AssertionError("replica lost the upstream database identity")
        if actual["meta"]["mode"] != "replica":
            raise AssertionError("replica mode flag was not preserved")


def assert_unchanged(before, after):
    for field_name in ("head", "fact_digest", "counts", "index_defs", "index_values",
                       "visible", "tombstones", "commit_revs"):
        if before[field_name] != after[field_name]:
            raise AssertionError("receiving environment changed after a rejected packet: " + field_name)


# --------------------------------------------------------------------------- #
# Source-of-truth construction
# --------------------------------------------------------------------------- #

def build_source_phase1(db_path):
    """First history block; the replica base backup is taken at this head."""
    with Database(db_path) as db:
        db.catalog.create("segments", SCHEMA)
        db.catalog.create("marks")
        request_rev = db.batch("seed_segments").put(
            "segments", "seg_00", {"region": "east", "sequence": 0, "code": "c0", "payload": "seed"},
            expected=0).commit().revision
        # A growing, then deleted, then recreated key exercises tombstone chains.
        db.batch().put("segments", "seg_life",
                       {"region": "west", "sequence": 1, "code": "clife", "payload": "born"}).commit()
        db.batch().delete("segments", "seg_life").commit()
        db.batch().put("segments", "seg_life",
                       {"region": "north", "sequence": 2, "code": "clife2", "payload": "reborn"}).commit()
        # A first block of ordinary revisions.
        for index in range(1, 30):
            db.batch().put(
                "segments", f"seg_{index:03}",
                {"region": ("east" if index % 2 else "west"), "sequence": index,
                 "code": f"c{index}", "payload": "x" * 512}).commit()
        base_head = db.revision
    return {"base_head": base_head, "request_rev": request_rev}


def extend_source(db_path, request_rev):
    """History appended *after* the replica base backup: real catch-up work."""
    with Database(db_path) as db:
        # Many ordinary revisions so apply windows are wide enough to hit.
        for index in range(30, 150):
            db.batch().put(
                "segments", f"seg_{index:03}",
                {"region": ("east" if index % 2 else "west"), "sequence": index,
                 "code": f"c{index}", "payload": "x" * 512}).commit()
        # Explicit expiry sweep produces a real delete commit with a tombstone.
        db.batch().put("segments", "seg_ttl",
                       {"region": "south", "sequence": 900, "code": "cttl", "payload": "tick"},
                       expires_at=100.0).commit()
        swept = db.retention.sweep(100.0)
        # Catalog evolution (a catalog-epoch change carried by normal commits).
        generation = db.catalog.evolve("segments", SCHEMA_EVOLVED, 1)
        db.batch().put("marks", "m0", {"segment": "seg_00", "note": "cross-collection batch"}).commit()
        # Indexes installed at the head; they must survive backup and final catch-up.
        db.indexes.create("segments", "by_region", "region")
        db.indexes.create("segments", "unique_code", "code", unique=True)
        # A catalog-only epoch change with no document commit: creating and then
        # dropping a throwaway index leaves index defs unchanged but moves the
        # epoch past the document head, so a final empty envelope can transport
        # the newest epoch and the current index definitions.
        db.indexes.create("segments", "temp_epoch", "region")
        db.indexes.drop("segments", "temp_epoch")
        document_head = db.revision
        # A second, strictly-later catalog-only epoch change. The head revision
        # (a commit-revision number) does not move, but catalog_epoch does, so
        # an empty envelope aligned to the head carries the newer epoch.
        db.indexes.create("segments", "temp_epoch_two", "region")
        db.indexes.drop("segments", "temp_epoch_two")
        # Pins and consumers exist in the source; restore must strip them.
        db.retention.pin("reader_a", request_rev)
        db.feed.register("mirror", 0)
        head = db.revision
        if head != document_head:
            raise AssertionError("catalog-only changes must not move the commit head")
        fingerprint = db.diagnostics.fingerprint()
        inspection = db.diagnostics.inspect(deep=True)
    return {"head": head, "document_head": document_head, "generation": generation,
            "sweep": swept, "fingerprint": fingerprint, "inspection": inspection}


def calibrate_backup(source_path, work_dir):
    durations = []
    for index in range(3):
        target = work_dir / f"calibrate_{index}.db"
        started = time.monotonic()
        with Database(source_path) as db:
            db.archives.backup(target)
        durations.append(time.monotonic() - started)
        target.unlink()
        # SIGKILL-free runs always unlink the temp database itself; zero-length
        # SQLite sidecars may remain and are not a leaked archive.
        if main_temp_files(work_dir, ".strataleaf-"):
            raise AssertionError("clean backup run left temp files behind")
    return sorted(durations)[1]


def export_envelope(source_path, since, limit):
    with Database(source_path) as db:
        return db.replication.export(since, limit=limit)


def export_to_file(source_path, since, limit, envelope_path):
    envelope = export_envelope(source_path, since, limit)
    write_json(envelope_path, envelope)
    return envelope["body"]["through"], envelope["body"]["catalog_epoch"]


# --------------------------------------------------------------------------- #
# Scenario A: hard interruption of a committing process must not fake durability
# --------------------------------------------------------------------------- #

def scenario_commit_durability(folder, source_path):
    work = folder / "commit"
    work.mkdir()
    db_path = work / "durability.db"
    # The worker creates the database, performs one commit, then is SIGKILLed
    # immediately after commit() returns. Nothing in the worker process flushes
    # or closes the handle, so a reopened file proves WAL durability alone.
    proc = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), "commit-kill", db_path],
        cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    marker = work / "committed.flag"
    arm_killer(proc.pid, trigger=marker)
    proc.wait(timeout=WORKER_TIMEOUT)
    if proc.returncode != -signal.SIGKILL:
        raise AssertionError("commit worker was not killed by SIGKILL: rc=%s %s" % (proc.returncode, proc.stderr.read()))

    # Fresh process, cold open, raw SQLite observation.
    facts = raw_facts(db_path)
    if facts["head"] != 1 or facts["counts"]["versions"] != 1:
        raise AssertionError("killed commit did not durably land")
    document = facts["visible"]["segments"][0]
    if document["key"] != "k_0" or document["document"]["sequence"] != 42:
        raise AssertionError("persisted document content differs")
    if facts["integrity"] != "ok" or not facts["commits_contiguous"]:
        raise AssertionError("killed database is not intact")

    # A second, normal process must keep writing on top of the durable history,
    # and the idempotent request must replay from the persisted receipt.
    proc = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), "commit-resume", db_path],
        cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc.wait(timeout=WORKER_TIMEOUT)
    if proc.returncode != 0:
        raise AssertionError("resume worker failed: " + proc.stderr.read())
    resume = read_json(work / "resume.json")
    facts_after = raw_facts(db_path)
    if facts_after["head"] != 2 or not resume["replayed"]:
        raise AssertionError("history/idempotency did not survive the process boundary")
    if facts_after["fact_digest"] == facts["fact_digest"]:
        raise AssertionError("second commit failed to extend the durable fact set")
    # Strongest durability proof: the main .db file alone (WAL folded in, no
    # sidecars copied) still carries both committed revisions and the receipt.
    lone = main_file_only_facts(db_path, work)
    if lone["fact_digest"] != facts_after["fact_digest"]:
        raise AssertionError("main-file-only copy lost committed facts")
    return {
        "kill_signal": "SIGKILL",
        "head_after_kill": facts["head"],
        "checkpoint": facts["checkpoint"],
        "sidecars_before": facts["sidecars_before"],
        "sidecars_after": facts["sidecars_after"],
        "head_after_resume": facts_after["head"],
        "replayed_receipt": resume["replayed"],
        "fact_digest_after": facts_after["fact_digest"],
        "integrity": facts_after["integrity"],
        "main_file_only_proof": lone,
    }


# --------------------------------------------------------------------------- #
# Scenario B: backup interruption never publishes a broken archive
# --------------------------------------------------------------------------- #

def scenario_backup_interruption(folder, source_path, source_facts):
    work = folder / "backup"
    work.mkdir()
    median = calibrate_backup(source_path, work)
    trials = []
    for index, fraction in enumerate((0.2, 0.5, 0.85, 0.97)):
        trial_dir = work / f"trial_{index}"
        trial_dir.mkdir()
        destination = trial_dir / "published.db"
        proc = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).resolve()), "backup-kill",
             source_path, destination, str(median * fraction)],
            cwd=str(trial_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        proc.wait(timeout=WORKER_TIMEOUT)
        killed = proc.returncode == -signal.SIGKILL
        leftovers = list_temp_files(trial_dir, ".strataleaf-")
        main_leftovers = main_temp_files(trial_dir, ".strataleaf-")
        if destination.exists():
            # A destination can only exist if the atomic hard-link publish
            # completed: it must then be a fully valid, restorable archive.
            inspection = inspect_archive(destination)
            with Database(destination) as published:
                if published.diagnostics.inspect(deep=True)["ok"] is not True:
                    raise AssertionError("published backup failed deep inspection")
        else:
            inspection = None
        # A retry on the same directory must succeed in spite of the crashed
        # worker's inert temp files, and must publish a valid archive.
        retry_receipt = None
        with Database(source_path) as db:
            retry_receipt = db.archives.backup(trial_dir / "retry.db")
        retry_inspection = inspect_archive(trial_dir / "retry.db")
        trials.append({"fraction": fraction, "killed": killed,
                       "destination_published": destination.exists(),
                       "leftover_temp_files": leftovers,
                       "main_temp_file_present": bool(main_leftovers),
                       "archive_inspection": inspection,
                       "retry_succeeded_after_leftover": retry_inspection["revision"] == source_facts["head"],
                       "retry_sha256": retry_receipt["sha256"]})
        if not killed:
            raise AssertionError("backup worker escaped the scheduled SIGKILL")
        if destination.exists() and inspection is None:
            raise AssertionError("published destination was not validated")

    # Uninterrupted backup on the same source must publish and restore.
    good_backup = work / "good.db"
    receipt = None
    with Database(source_path) as db:
        receipt = db.archives.backup(good_backup)
    restored = work / "restored.db"
    restore_receipt = Archives.restore(good_backup, restored, expected_sha256=receipt["sha256"])
    restored_facts = raw_facts(restored)
    restored_cold = cold_inspection(restored)
    assert_same_facts(restored_facts, source_facts, identity_differ=True)
    # Pins/consumers are deliberately cleared on restore.
    if restored_facts["counts"]["pins"] != 0 or restored_facts["counts"]["consumers"] != 0:
        raise AssertionError("restore did not clear pins/consumers")
    if restored_facts["sidecars_after"].get("-wal") not in (0, None):
        raise AssertionError("checkpoint did not fold the WAL into the main file")
    lone_restored = main_file_only_facts(restored, work)
    if lone_restored["fact_digest"] != source_facts["fact_digest"]:
        raise AssertionError("restored main-file-only copy differs from the source")
    return {
        "calibrated_backup_seconds": round(median, 6),
        "interruption_trials": trials,
        "backup_receipt": receipt,
        "restore_receipt": restore_receipt,
        "restored_fact_digest": restored_facts["fact_digest"],
        "source_fact_digest": source_facts["fact_digest"],
        "restored_cold_inspection_ok": restored_cold["inspection"]["ok"],
        "restored_fingerprint": restored_cold["fingerprint"],
        "identity_replaced": restored_facts["meta"]["database_id"] != source_facts["meta"]["database_id"],
        "checkpoint": restored_facts["checkpoint"],
        "sidecars_after_checkpoint": restored_facts["sidecars_after"],
        "main_file_only_proof": lone_restored,
    }


# --------------------------------------------------------------------------- #
# Scenario C: restore guards and interruption
# --------------------------------------------------------------------------- #

def make_corrupt_archive(good_backup, corrupt_path):
    data = good_backup.read_bytes()
    # Truncate a real SQLite archive mid-file: a physically damaged backup,
    # written into its own fresh directory as untrusted input.
    corrupt_path.write_bytes(data[: max(1, len(data) // 2)])


def scenario_restore_guards(folder, source_path, source_facts):
    work = folder / "restore"
    work.mkdir()
    good_backup = work / "good.db"
    with Database(source_path) as db:
        receipt = db.archives.backup(good_backup)

    # C1: restore interrupted while copying into the temp file.
    interrupted_dir = work / "interrupted"
    interrupted_dir.mkdir()
    destination = interrupted_dir / "restored.db"
    proc = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), "restore-kill",
         good_backup, destination, "copy"],
        cwd=str(interrupted_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc.wait(timeout=WORKER_TIMEOUT)
    if proc.returncode != -signal.SIGKILL:
        raise AssertionError("restore worker was not SIGKILLed")
    if destination.exists():
        raise AssertionError("interrupted restore published a destination")
    killed_temp_files = main_temp_files(interrupted_dir, ".strataleaf-restore-")

    # The same destination path is reusable afterwards and lands identical
    # facts, even though the killed process left inert temp files behind.
    rerun_receipt = Archives.restore(good_backup, destination, expected_sha256=receipt["sha256"])
    rerun_facts = raw_facts(destination)
    assert_same_facts(rerun_facts, source_facts, identity_differ=True)

    # C2: wrong checksum must refuse before touching the destination.
    bad_checksum_dir = work / "bad_checksum"
    bad_checksum_dir.mkdir()
    bad_destination = bad_checksum_dir / "out.db"
    bad_checksum = expect_error(
        lambda: Archives.restore(good_backup, bad_destination, expected_sha256="0" * 64),
        DatabaseError)
    if bad_destination.exists() or main_temp_files(bad_checksum_dir, ".strataleaf-restore-"):
        raise AssertionError("checksum rejection left files behind")

    # C3: physically corrupt archive must be rejected.
    corrupt_dir = work / "corrupt"
    corrupt_dir.mkdir()
    corrupt_archive = corrupt_dir / "corrupt.db"
    make_corrupt_archive(good_backup, corrupt_archive)
    corrupt_destination = corrupt_dir / "out.db"
    corrupt_error = expect_error(
        lambda: Archives.restore(corrupt_archive, corrupt_destination), DatabaseError)
    if corrupt_destination.exists():
        raise AssertionError("corrupt archive produced a destination")

    # C4: an existing target is never overwritten; the target stays byte-identical.
    occupied_dir = work / "occupied"
    occupied_dir.mkdir()
    guard_db = occupied_dir / "guard.db"
    with Database(guard_db) as guard:
        guard.catalog.create("guard_col")
        guard.batch().put("guard_col", "g1", {"marker": "untouched"}).commit()
    guard_before = raw_facts(guard_db)
    occupied_error = expect_error(
        lambda: Archives.restore(good_backup, guard_db, expected_sha256=receipt["sha256"]), Conflict)
    guard_after = raw_facts(guard_db)
    assert_unchanged(guard_before, guard_after)
    if guard_after["visible"] != {"guard_col": [{"key": "g1", "rev": 1,
        "document": {"marker": "untouched"}, "expires_at": None}]}:
        raise AssertionError("existing restore target was modified")

    # C5: backup itself also refuses an occupied destination.
    def backup_into_occupied():
        with Database(source_path) as source_db:
            source_db.archives.backup(guard_db)

    backup_occupied = expect_error(backup_into_occupied, Conflict)
    guard_after_backup = raw_facts(guard_db)
    assert_unchanged(guard_after, guard_after_backup)
    return {
        "interrupted_restore": {"killed": True, "destination_published": False,
                                "inert_temp_files_left_by_sigkill": killed_temp_files,
                                "rerun_succeeded": True,
                                "rerun_fact_digest": rerun_facts["fact_digest"]},
        "bad_checksum": bad_checksum,
        "corrupt_archive": corrupt_error,
        "existing_target": {"error": occupied_error, "guard_digest_unchanged":
                            guard_before["fact_digest"] == guard_after["fact_digest"]},
        "backup_existing_target": backup_occupied,
        "source_fact_digest": source_facts["fact_digest"],
    }


# --------------------------------------------------------------------------- #
# Scenario D: segmented replica receive, duplicates, gaps and interruption
# --------------------------------------------------------------------------- #

def make_replica(source_backup, replica_path):
    Archives.restore(source_backup, replica_path, follower=True)


def segmented_ranges(base_head, head, page):
    ranges = []
    since = base_head
    while since < head:
        through = min(since + page, head)
        ranges.append((since, through))
        since = through
    return ranges


def scenario_replica_interruption(folder, source_path, source_facts, base_backup, base_head):
    work = folder / "replica"
    work.mkdir()
    head = source_facts["head"]

    # D1: segmented catch-up with hard interruption during a large apply.
    replica_a = work / "replica_a.db"
    make_replica(base_backup, replica_a)
    envelope_dir = work / "envelopes_a"
    envelope_dir.mkdir()
    paths_a = []
    for since, through in segmented_ranges(base_head, head, 5):
        envelope_path = envelope_dir / f"env_{since:04}_{through:04}.json"
        export_to_file(source_path, since, 5, envelope_path)
        paths_a.append((envelope_path, since, through))

    apply_trials = []
    for index, fraction in enumerate((0.15, 0.45, 0.8)):
        envelope_path, since, through = paths_a[len(apply_trials)]
        before = raw_facts(replica_a)
        proc = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).resolve()), "apply-kill",
             replica_a, envelope_path, str(fraction)],
            cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        proc.wait(timeout=WORKER_TIMEOUT)
        killed = proc.returncode == -signal.SIGKILL
        after = raw_facts(replica_a)
        # Atomicity: the whole packet is present or the whole packet is absent.
        if not (after["head"] == through or after["head"] == since):
            raise AssertionError("partial packet application after SIGKILL")
        if after["head"] == through:
            if after["head"] <= before["head"]:
                raise AssertionError("applied packet did not advance the head")
        apply_trials.append({"envelope": [since, through], "killed": killed,
                             "head_before": before["head"], "head_after": after["head"],
                             "outcome": "applied" if after["head"] == through else "rolled_back"})
        # Re-deliver the same packet until it durably lands (retry semantics).
        while raw_facts(replica_a)["head"] < through:
            proc = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).resolve()), "apply-worker",
                 replica_a, envelope_path],
                cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            proc.wait(timeout=WORKER_TIMEOUT)
            if proc.returncode != 0:
                raise AssertionError("retry apply failed: " + proc.stderr.read())

    # Apply any remaining envelopes normally (the trial consumed the first ones).
    for envelope_path, since, through in paths_a[len(apply_trials):]:
        result = run_worker(work, "apply-worker", replica_a, envelope_path)
        if result.returncode != 0:
            raise AssertionError("segment apply failed: " + result.stderr)
    final_a = raw_facts(replica_a)
    if final_a["head"] != head:
        raise AssertionError("segmented catch-up did not reach the source head")
    # Indexes are only installed once the replica reaches the exported head.
    if not final_a["index_defs"] or final_a["index_values"] != source_facts["index_values"]:
        raise AssertionError("final replica index projections differ from the source")
    assert_same_facts(final_a, source_facts)
    cold_a = cold_inspection(replica_a)
    if not cold_a["inspection"]["ok"]:
        raise AssertionError("replica deep inspection failed after interrupted catch-up")
    # Main-file-only proof that interrupted catch-up persisted every fact on
    # disk, rather than holding them in the applying process.
    lone_a = main_file_only_facts(replica_a, work)
    if lone_a["fact_digest"] != source_facts["fact_digest"]:
        raise AssertionError("replica main-file-only copy differs from the source")

    # D2: duplicate packets never double-write, even across process boundaries.
    duplicate_results = []
    for envelope_path, since, through in paths_a[-3:]:
        before = raw_facts(replica_a)
        proc = run_worker(work, "apply-worker", replica_a, envelope_path)
        if proc.returncode != 0:
            raise AssertionError("duplicate apply crashed: " + proc.stderr)
        after = raw_facts(replica_a)
        assert_unchanged(before, after)
        duplicate_results.append(read_json(work / "apply_result.json"))

    # D3: rejected packets leave the receiving environment untouched.
    immunity = _replica_immunity(work, base_backup, source_path, head, base_head, source_facts)

    # D4: catalog-only envelope (zero document commits) still advances the epoch.
    catalog_only = _catalog_only_delivery(work, base_backup, source_path, head, base_head, source_facts)

    return {
        "base_backup_head": base_head,
        "interruption_trials": apply_trials,
        "final_head": final_a["head"],
        "final_fact_digest": final_a["fact_digest"],
        "source_fact_digest": source_facts["fact_digest"],
        "indexes_match": final_a["index_values"] == source_facts["index_values"],
        "duplicate_packets": duplicate_results,
        "cold_inspection_ok": cold_a["inspection"]["ok"],
        "main_file_only_proof": lone_a,
        "immunity": immunity,
        "catalog_only": catalog_only,
    }


def _replica_immunity(work, base_backup, source_path, head, base_head, source_facts):
    results = {}
    # A fresh replica advanced one segment past its base backup, reset between cases.
    fresh = work / "immunity_replica.db"
    make_replica(base_backup, fresh)
    first_env = work / "immunity_first.json"
    export_to_file(source_path, base_head, 5, first_env)
    run_worker(work, "apply-worker", fresh, first_env).check_returncode()
    snapshot = raw_facts(fresh)

    def reset_replica():
        for suffix in ("", "-wal", "-shm"):
            Path(str(fresh) + suffix).unlink(missing_ok=True)
        make_replica(base_backup, fresh)
        run_worker(work, "apply-worker", fresh, first_env).check_returncode()

    # Bad checksum: flip the digest without touching the body.
    reset_replica()
    envelope = read_json(first_env)
    bad = copy.deepcopy(envelope)
    bad["sha256"] = "f" * 64
    bad_path = work / "immunity_bad_checksum.json"
    write_json(bad_path, bad)
    results["bad_checksum"] = _expect_apply_failure(work, fresh, bad_path)
    assert_unchanged(snapshot, raw_facts(fresh))

    # Gap: claim a through revision that the commit list does not cover.
    reset_replica()
    gapped = copy.deepcopy(envelope)
    gapped["body"]["through"] = gapped["body"]["through"] + 1
    gapped["sha256"] = digest(gapped["body"])
    gap_path = work / "immunity_gap.json"
    write_json(gap_path, gapped)
    results["revision_gap"] = _expect_apply_failure(work, fresh, gap_path)
    assert_unchanged(snapshot, raw_facts(fresh))

    # Wrong upstream identity.
    reset_replica()
    foreign = copy.deepcopy(envelope)
    foreign["body"]["source"] = "00000000-0000-0000-0000-000000000000"
    foreign["sha256"] = digest(foreign["body"])
    foreign_path = work / "immunity_foreign.json"
    write_json(foreign_path, foreign)
    results["wrong_upstream"] = _expect_apply_failure(work, fresh, foreign_path)
    assert_unchanged(snapshot, raw_facts(fresh))

    # Malformed envelope (missing required field).
    reset_replica()
    malformed = {"body": {"format": 1}, "sha256": digest({"format": 1})}
    malformed_path = work / "immunity_malformed.json"
    write_json(malformed_path, malformed)
    results["malformed"] = _expect_apply_failure(work, fresh, malformed_path)
    assert_unchanged(snapshot, raw_facts(fresh))

    # A replica never accepts primary writes.
    reset_replica()
    readonly = run_worker(work, "readonly-worker", fresh)
    if readonly.returncode != 0:
        raise AssertionError("readonly worker failed: " + readonly.stderr)
    results["primary_write_rejected"] = read_json(work / "readonly_result.json")
    assert_unchanged(snapshot, raw_facts(fresh))

    results["head_after_all_rejections"] = raw_facts(fresh)["head"]
    results["fact_digest_unchanged"] = raw_facts(fresh)["fact_digest"] == snapshot["fact_digest"]
    return results


def _expect_apply_failure(work, replica, envelope_path):
    proc = run_worker(work, "apply-worker", replica, envelope_path)
    if proc.returncode == 0:
        raise AssertionError("bad envelope was accepted")
    return {"returncode": proc.returncode,
            "stderr_tail": proc.stderr.strip().splitlines()[-1][:200] if proc.stderr.strip() else ""}


def _catalog_only_delivery(work, base_backup, source_path, head, base_head, source_facts):
    # Part 1: catch a fresh replica only to head-1. An envelope whose through
    # is below the source head must withhold the current index definitions.
    replica = work / "catalog_replica.db"
    make_replica(base_backup, replica)
    penultimate = head - 1
    since = base_head
    envelope_paths = []
    while since < penultimate:
        page = min(5, penultimate - since)
        path = work / f"catalog_env_{since}.json"
        through, _ = export_to_file(source_path, since, page, path)
        envelope_paths.append(path)
        since = through
    for path in envelope_paths:
        run_worker(work, "apply-worker", replica, path).check_returncode()
    mid = raw_facts(replica)
    if mid["head"] != penultimate:
        raise AssertionError("catalog setup did not stop the replica at head-1")
    if mid["index_defs"]:
        raise AssertionError("sub-head replica must not receive the current index definitions")

    # Part 2: the head-aligned envelope. The source's last commits include no
    # document writes for the catalog-only tail; the envelope carries the newest
    # catalog epoch and installs the current index definitions exactly once the
    # replica reaches the head.
    head_envelope = export_envelope(source_path, penultimate, 100)
    if not head_envelope["body"]["commits"]:
        raise AssertionError("test setup expected the head envelope to contain the last commit")
    head_path = work / "catalog_head.json"
    write_json(head_path, head_envelope)
    run_worker(work, "apply-worker", replica, head_path).check_returncode()
    caught_up = raw_facts(replica)
    if caught_up["head"] != head:
        raise AssertionError("head envelope did not align the replica to the source head")
    if caught_up["index_defs"] != source_facts["index_defs"]:
        raise AssertionError("head envelope did not install the current index definitions")
    if int(caught_up["meta"]["catalog_epoch"]) != int(source_facts["meta"]["catalog_epoch"]):
        raise AssertionError("head envelope did not carry the newest catalog epoch")

    # Part 3: re-deliver an empty head-aligned envelope. It must be idempotent
    # for documents while still carrying the catalog facts and not diverging.
    empty_envelope = export_envelope(source_path, head, 100)
    if empty_envelope["body"]["commits"]:
        raise AssertionError("test setup expected an empty head-aligned commit range")
    empty_path = work / "catalog_final.json"
    write_json(empty_path, empty_envelope)
    before_empty = raw_facts(replica)
    run_worker(work, "apply-worker", replica, empty_path).check_returncode()
    final = raw_facts(replica)
    assert_unchanged(before_empty, final)
    if int(final["meta"]["catalog_epoch"]) != int(source_facts["meta"]["catalog_epoch"]):
        raise AssertionError("empty envelope lost the catalog epoch")
    assert_same_facts(final, source_facts)
    return {"stopped_at_head_minus_one": penultimate,
            "indexes_withheld_sub_head": [],
            "head_envelope_commits": len(head_envelope["body"]["commits"]),
            "final_epoch": int(final["meta"]["catalog_epoch"]),
            "empty_envelope_idempotent": True,
            "final_fact_digest": final["fact_digest"]}


# --------------------------------------------------------------------------- #
# Worker subcommands (each runs in its own fresh process)
# --------------------------------------------------------------------------- #

def worker_commit_kill(db_path):
    marker = Path(db_path).parent / "committed.flag"
    with Database(db_path) as db:
        db.catalog.create("segments", SCHEMA)
        db.batch("durable_request").put(
            "segments", "k_0", {"region": "east", "sequence": 42}, expected=0).commit()
        marker.write_text("committed", encoding="utf-8")
        # Stay alive until the watchdog delivers SIGKILL; do not close/flush.
        time.sleep(WORKER_TIMEOUT + 5)


def worker_commit_resume(db_path):
    with Database(db_path) as db:
        replay = db.batch("durable_request").put(
            "segments", "k_0", {"region": "east", "sequence": 42}, expected=0).commit()
        second = db.batch().put(
            "segments", "k_1", {"region": "west", "sequence": 43}).commit()
        write_json(Path(db_path).parent / "resume.json",
                   {"replayed": replay.replayed, "replay_revision": replay.revision,
                    "second_revision": second.revision})


def worker_backup_kill(source_path, destination, delay):
    arm_killer(os.getpid(), delay=float(delay))
    with Database(source_path) as db:
        db.archives.backup(destination)
    time.sleep(WORKER_TIMEOUT + 5)


def worker_restore_kill(source_path, destination, phase):
    if phase == "copy":
        # Kill as soon as the restore temp file appears.
        parent = Path(destination).parent

        def watcher():
            deadline = time.monotonic() + WORKER_TIMEOUT
            while time.monotonic() < deadline:
                if main_temp_files(parent, ".strataleaf-restore-"):
                    os.kill(os.getpid(), signal.SIGKILL)
                time.sleep(POLL_INTERVAL)

        threading.Thread(target=watcher, daemon=True).start()
    Archives.restore(source_path, destination)
    time.sleep(WORKER_TIMEOUT + 5)


def worker_apply_worker(replica_path, envelope_path):
    envelope = read_json(envelope_path)
    with Database(replica_path) as db:
        result = db.replication.apply(envelope)
    write_json(Path(replica_path).parent / "apply_result.json", result)


def worker_apply_kill(replica_path, envelope_path, fraction):
    envelope = read_json(envelope_path)
    # Calibrate a normal apply of the same envelope on a throwaway copy so the
    # kill delay scales with the real work on this machine.
    calibrate_copy = Path(str(replica_path) + ".calibrate")
    for suffix in ("", "-wal", "-shm"):
        source_sidecar = Path(str(replica_path) + suffix)
        if source_sidecar.exists():
            Path(str(calibrate_copy) + suffix).write_bytes(source_sidecar.read_bytes())
    started = time.monotonic()
    with Database(calibrate_copy) as scratch:
        scratch.replication.apply(copy.deepcopy(envelope))
    duration = time.monotonic() - started
    for suffix in ("", "-wal", "-shm"):
        Path(str(calibrate_copy) + suffix).unlink(missing_ok=True)
    arm_killer(os.getpid(), delay=max(0.001, duration * float(fraction)))
    with Database(replica_path) as db:
        db.replication.apply(envelope)
    time.sleep(WORKER_TIMEOUT + 5)


def worker_observe(db_path):
    with Database(db_path) as db:
        inspection = db.diagnostics.inspect(deep=True)
        fingerprint = db.diagnostics.fingerprint()
    write_json(Path(db_path).parent / "observe.json",
               {"inspection": inspection, "fingerprint": fingerprint})


def worker_readonly(replica_path):
    with Database(replica_path) as db:
        result = expect_error(
            lambda: db.batch().delete("segments", "seg_000").commit(), ReadOnly)
    write_json(Path(replica_path).parent / "readonly_result.json", result)


WORKERS = {
    "commit-kill": lambda argv: worker_commit_kill(Path(argv[0])),
    "commit-resume": lambda argv: worker_commit_resume(Path(argv[0])),
    "backup-kill": lambda argv: worker_backup_kill(Path(argv[0]), Path(argv[1]), argv[2]),
    "restore-kill": lambda argv: worker_restore_kill(Path(argv[0]), Path(argv[1]), argv[2]),
    "apply-worker": lambda argv: worker_apply_worker(Path(argv[0]), Path(argv[1])),
    "apply-kill": lambda argv: worker_apply_kill(Path(argv[0]), Path(argv[1]), argv[2]),
    "observe": lambda argv: worker_observe(Path(argv[0])),
    "readonly-worker": lambda argv: worker_readonly(Path(argv[0])),
}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

UNCOVERED_WINDOWS = [
    "SIGKILL cannot land inside one SQLite IMMEDIATE transaction; the atomicity "
    "of a single commit/apply boundary is guaranteed by SQLite WAL, not probed "
    "at an intra-transaction instruction. The harness instead proves the "
    "all-or-nothing outcome from a fresh process after the kill.",
    "A crash during the final os.link publish of backup/restore is not "
    "synthesized: on POSIX the link itself is atomic, and the harness covers "
    "the windows immediately before it (temp-file copy) and verifies every "
    "published destination afterwards.",
    "Power loss, torn disk writes, ENOSPC and fsync failure are not injected; "
    "they rely on PRAGMA synchronous=FULL plus SQLite's own crash recovery, "
    "which the hard-kill reopen exercises but cannot exhaustively emulate.",
    "Interruption of an inspect/checksum read, a passive WAL checkpoint or a "
    "feed read is not covered; those paths mutate no committed facts.",
    "Network transport identity/authentication is out of scope; envelopes are "
    "treated as trusted-channel data whose checksum detects corruption only.",
]


def run(keep_dir=None):
    if keep_dir is not None:
        root = Path(keep_dir)
        root.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        root = Path(tempfile.mkdtemp(prefix="strataleaf-interruption-"))
        cleanup = True
    started = time.monotonic()
    try:
        source_path = root / "source.db"
        phase1 = build_source_phase1(source_path)
        base_head = phase1["base_head"]
        # The replica base backup is taken mid-history, before the source keeps
        # receiving commits, so catch-up has real work to do.
        replica_base = root / "replica_base.db"
        with Database(source_path) as db:
            base_receipt = db.archives.backup(replica_base)
        summary = extend_source(source_path, phase1["request_rev"])
        source_facts = raw_facts(source_path)
        write_json(root / "source_facts.json", source_facts)

        scenarios = {
            "commit_durability": scenario_commit_durability(root, source_path),
            "backup_interruption": scenario_backup_interruption(
                root, source_path, source_facts),
            "restore_guards": scenario_restore_guards(root, source_path, source_facts),
            "replica_interruption": scenario_replica_interruption(
                root, source_path, source_facts, replica_base, base_head),
        }
        # The source must remain byte-for-byte logically untouched throughout.
        source_after = raw_facts(source_path)
        if source_after["fact_digest"] != source_facts["fact_digest"]:
            raise AssertionError("source database was modified by the verification")
        report = {
            "status": "passed",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "source": {"path": str(source_path), "head": source_facts["head"],
                       "floor": source_facts["floor"],
                       "base_backup_head": base_head,
                       "base_backup_receipt": base_receipt,
                       "fact_digest": source_facts["fact_digest"],
                       "inspection_ok": summary["inspection"]["ok"],
                       "fingerprint": summary["fingerprint"]},
            "scenarios": scenarios,
            "uncovered_interruption_windows": UNCOVERED_WINDOWS,
            "observation_method": "fresh OS processes open every file cold; raw "
                                  "read-only sqlite3 reads plus a TRUNCATE checkpoint prove "
                                  "the main database file (not an in-process cache or the WAL) "
                                  "holds the facts; production diagnostics are re-run in a "
                                  "separate process as an independent secondary check.",
        }
        write_json(root / "report.json", report)
        return report
    finally:
        if cleanup:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("# evidence directory retained at " + str(root), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backup/replica interruption verification")
    parser.add_argument("--keep", metavar="DIR", default=None,
                        help="retain every temporary database and the JSON report in DIR")
    args = parser.parse_args(argv)
    report = run(keep_dir=args.keep)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in WORKERS:
        WORKERS[sys.argv[1]](sys.argv[2:])
    else:
        main()
