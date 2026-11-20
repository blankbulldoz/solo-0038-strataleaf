"""End-to-end disaster-recovery drill orchestration.

One drill executes, against real files and the real library:

1. start a live writer against the primary;
2. take a verified online backup while writes continue;
3. restore that backup into an isolated directory and verify identity +
   catalog + index + document facts (not just ``head``);
4. replay the writes that happened after the backup into the restored copy by
   bootstrapping it as a replica and applying contiguous packets;
5. exercise failure paths: interrupted receive, duplicate packet, tampered
   packet, a bare file copy rejection, and the reclaimed-history case;
6. emit a JSON evidence bundle plus a human-readable Markdown report.

The drill never issues SQL against a database and never lets the tool modify
``strataleaf``.  Every state change goes through the public API.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from strataleaf import Archives, Database, SnapshotTooOld

from . import TOOL_NAME, TOOL_VERSION
from .backup import (BackupError, compare_bare_copy, produce_backup, prune_stale_temporaries,
                     reject_bare_copy, restore_and_verify, stale_temporaries, verify_manifest)
from .facts import compare_snapshot, file_digest, read_facts, summary
from .replication import catch_up, protect_source, release_source_pin
from .workload import run_writer


class DrillError(Exception):
    pass


def _sidecar_bytes(path: Path) -> dict:
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    return {"wal_bytes": wal.stat().st_size if wal.exists() else 0,
            "shm_bytes": shm.stat().st_size if shm.exists() else 0}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, tuple)):
        return list(value)
    return str(value)


class Drill:
    def __init__(self, root: str | Path, writer_seconds: float = 2.5, interval: float = 0.005,
                 prefix: str = "svc", seed: int = 7):
        self.root = Path(root).resolve()
        self.writer_seconds = writer_seconds
        self.interval = interval
        self.prefix = prefix
        self.seed = seed
        self.layout = {
            "primary": self.root / "primary",
            "backups": self.root / "backups",
            "restore": self.root / "restore",
            "replica": self.root / "replica",
            "evidence": self.root / "evidence",
        }
        self.report = {
            "tool": TOOL_NAME, "version": TOOL_VERSION, "started_at": utc_now(),
            "layout": {key: str(value) for key, value in self.layout.items()},
            "scenarios": [], "warnings": [], "errors": [],
        }
        self.primary = None

    # -- helpers ---------------------------------------------------------
    def scenario(self, name: str, **payload) -> dict:
        entry = {"scenario": name, "at": utc_now(), **payload}
        self.report["scenarios"].append(entry)
        return entry

    def prepare(self) -> None:
        if self.root.exists() and any(self.root.iterdir()):
            raise DrillError("drill root is not empty; use a fresh directory (got %s)" % self.root)
        for directory in self.layout.values():
            directory.mkdir(parents=True, exist_ok=True)
        self.primary = self.layout["primary"] / "service.db"
        self._prepared = True

    # -- phase 1: live writer + concurrent backup ------------------------
    def phase_backup_under_write(self) -> dict:
        state: dict = {}
        stop = threading.Event()
        writer = threading.Thread(target=run_writer, name="drill-writer",
                                  args=(str(self.primary), self.writer_seconds, state, stop),
                                  kwargs={"interval": self.interval, "catalog_every": 25,
                                          "prefix": self.prefix}, daemon=True)
        writer.start()
        deadline = time.time() + 10
        while not state.get("ready") and time.time() < deadline:
            if state.get("errors"):
                raise DrillError("writer failed during bootstrap: %s" % state["errors"])
            time.sleep(0.01)
        if not state.get("ready"):
            raise DrillError("writer did not become ready")

        # Wait until the service has genuinely been writing, so the backup runs
        # against a live WAL rather than immediately after bootstrap.
        settle = time.time() + 0.6
        while time.time() < settle and state.get("commits", 0) < 8:
            time.sleep(0.01)

        before = read_facts(self.primary)
        wal_bytes_before = _sidecar_bytes(self.primary)
        # Take several backups while the writer keeps committing.  Each archive
        # captures a moving head, which is the point: a snapshot taken under
        # continuous writes must still be internally consistent.  Keep going
        # until at least one archive demonstrably spans a commit.
        manifests = []
        per_backup = []
        for attempt in range(1, 9):
            started_head = read_facts(self.primary)["head"]
            manifest = produce_backup(self.primary, self.layout["backups"],
                                      label="drill%d" % attempt)
            finished_head = read_facts(self.primary)["head"]
            verified = verify_manifest(self.layout["backups"] / manifest["archive"]["name"])
            if not verified["ok"]:
                raise DrillError("backup %d did not survive independent verification" % attempt)
            manifests.append(manifest)
            per_backup.append({
                "attempt": attempt,
                "archive": manifest["archive"]["name"],
                "archived_revision": manifest["facts"]["head"],
                "head_at_backup_start": started_head,
                "head_at_backup_end": finished_head,
                "writes_crossed_during_backup": finished_head - started_head,
                "rpo_commits_when_published": finished_head - manifest["facts"]["head"],
                "verified": verified["ok"],
                "writer_commit_count": state.get("commits"),
            })
            if finished_head > started_head:
                break
            time.sleep(0.005)
        manifest = manifests[0]
        wal_bytes_after = _sidecar_bytes(self.primary)
        bare = compare_bare_copy(self.primary, self.layout["backups"] / "raw-copy-suspect.db")
        verified = verify_manifest(self.layout["backups"] / manifest["archive"]["name"])

        # Let the writer keep going so later phases see post-backup commits.
        time.sleep(max(0.2, self.writer_seconds * 0.35))
        stop.set()
        writer.join(timeout=15)
        if writer.is_alive():
            raise DrillError("writer thread did not stop")
        if state.get("errors"):
            raise DrillError("writer reported errors: %s" % state["errors"])

        after = read_facts(self.primary)
        archived = manifest["facts"]["head"]
        entry = self.scenario(
            "backup_under_write",
            writer={"commits": state.get("commits"), "operations": state.get("operations"),
                    "catalog_changes": len(state.get("catalog", [])),
                    "reported_head": state.get("head"), "errors": state.get("errors")},
            source_head_before=before["head"], source_head_after=after["head"],
            archived_revision=archived,
            backups_under_write=per_backup,
            writes_during_backup=after["head"] - before["head"],
            writes_after_archive=after["head"] - archived,
            writer_active_during_backup=wal_bytes_before["wal_bytes"] > 0,
            source_wal_bytes={"before_backup": wal_bytes_before, "after_backup": wal_bytes_after},
            backup={"sha256": manifest["archive"]["sha256"], "file_bytes": manifest["archive"]["file_bytes"],
                    "attempts": manifest["attempts"]},
            verification={"ok": verified["ok"], "checks": verified["checks"]},
            bare_copy_comparison=bare,
            bare_copy_rejection=reject_bare_copy(self.layout["backups"] / "raw-copy-suspect.db"),
            stale_temporaries=stale_temporaries(self.layout["backups"]),
        )
        if not (before["head"] <= archived <= after["head"]):
            raise DrillError("archived revision outside the write window")
        if after["head"] <= archived:
            raise DrillError("no post-backup writes were produced; drill cannot test catch-up")
        if not any(item["writes_crossed_during_backup"] > 0 for item in per_backup):
            raise DrillError("no backup actually spanned concurrent writes")
        return {"manifest": manifest, "manifests": manifests, "state": state, "entry": entry}

    # -- phase 2: isolated restore + fact verification -------------------
    def phase_restore(self, manifest: dict) -> dict:
        archive = self.layout["backups"] / manifest["archive"]["name"]
        target = self.layout["restore"] / "primary-restored.db"
        started = time.time()
        result = restore_and_verify(archive, target, mode="primary")
        elapsed = round(time.time() - started, 3)
        entry = self.scenario(
            "restore_isolated_primary",
            archive=result["archive"], destination=result["destination"],
            isolated=str(target.parent.resolve()) != str(Path(self.primary).parent.resolve()),
            elapsed_seconds=elapsed,
            checks=result["checks"], ok=result["ok"],
            facts=result["facts"],
        )
        if not result["ok"]:
            raise DrillError("restored primary failed fact verification: %s"
                             % [c for c in result["checks"] if not c["ok"]])
        # prove the restore is a new primary, not the live source
        if Path(result["facts"]["path"]) == Path(self.primary).resolve():
            raise DrillError("restore wrote into the live primary path")
        return {"result": result, "path": target, "entry": entry, "elapsed_seconds": elapsed}

    # -- phase 3: post-restore catch-up as replica -----------------------
    def phase_catch_up(self, restored_primary: Path, bootstrap_manifest: dict) -> dict:
        archive = self.layout["backups"] / bootstrap_manifest["archive"]["name"]
        follower = self.layout["replica"] / "service-replica.db"
        bootstrap = Archives.restore(archive, follower, bootstrap_manifest["archive"]["sha256"], follower=True)
        replica_facts = read_facts(follower)
        if replica_facts["identity"]["upstream"] != bootstrap_manifest["archive"]["database_id"]:
            raise DrillError("replica bootstrap lost the upstream binding")

        # Protect the source so a concurrent compaction cannot reclaim the
        # range this drill needs; release it afterwards.
        pin = protect_source(self.primary, "dr_drill_pin", bootstrap_manifest["facts"]["head"])

        first = catch_up(self.primary, follower, packet_limit=7, stop_after_packets=3)
        if first["status"] != "interrupted":
            raise DrillError("expected an interrupted receive, got %s" % first["status"])
        intermediate = read_facts(follower)
        if intermediate["head"] <= bootstrap_manifest["facts"]["head"]:
            raise DrillError("interrupted receive made no progress")
        resume_started = time.time()
        resumed = catch_up(self.primary, follower, packet_limit=7, corrupt_probe=True)
        resume_elapsed = round(time.time() - resume_started, 3)
        if resumed["status"] != "caught_up":
            raise DrillError("replica did not catch up after resume: %s / %s"
                             % (resumed["status"], resumed["reason"]))

        # Duplicate packet must not write twice.
        with Database(self.primary) as upstream, Database(follower) as replica_db:
            head = replica_db.revision
            packet = upstream.replication.export(max(0, head - 2), limit=10)
            duplicate = replica_db.replication.apply(packet)
            repeat = replica_db.replication.apply(packet)
        release_source_pin(self.primary, pin["pin"])
        recovered_at = read_facts(self.primary, revision=resumed["replica_revision"])

        entry = self.scenario(
            "catch_up_replica",
            bootstrap={"path": str(follower), "revision": bootstrap["revision"],
                       "database_id": bootstrap["database_id"], "mode": bootstrap["mode"]},
            interrupted={"replica_revision": first["replica_revision"], "target_head": first["target_head"],
                         "events": first["events"]},
            resume={"status": resumed["status"], "replica_revision": resumed["replica_revision"],
                    "target_head": resumed["target_head"], "events": resumed["events"],
                    "elapsed_seconds": resume_elapsed, "checks": resumed["checks"]},
            corrupt_probe=resumed["corrupt_probe"],
            duplicate={"first": duplicate, "repeat": repeat},
            recovered_revision=resumed["replica_revision"],
            primary_head_at_end=read_facts(self.primary)["head"],
        )
        if not resumed["checks"] or not all(item["ok"] for item in resumed["checks"]):
            raise DrillError("replica facts differ from the source after catch-up")
        return {"replica": follower, "entry": entry, "result": resumed,
                "recovered_source_facts": recovered_at}

    # -- phase 4: reclaimed source history -------------------------------
    def phase_reclaimed_history(self, bootstrap_manifest: dict) -> dict:
        """A stale replica whose needed range the source has compacted away."""
        stale_dir = self.layout["replica"] / "stale"
        stale_dir.mkdir(parents=True, exist_ok=True)
        stale = stale_dir / "stale-replica.db"
        archive = self.layout["backups"] / bootstrap_manifest["archive"]["name"]
        Archives.restore(archive, stale, bootstrap_manifest["archive"]["sha256"], follower=True)
        stale_floor_revision = read_facts(stale)["head"]

        # Advance source, register a throwaway consumer so compaction can run,
        # then compact past the stale replica's revision.
        with Database(self.primary) as db:
            for index in range(4):
                db.batch().put("orders", "%s-drill-%d" % (self.prefix, index),
                               {"region": "south", "sequence": 900 + index, "code": "drill"}).commit()
            db.feed.register("drill_consumer", db.revision)
            target = db.revision
            compact = db.retention.compact(target)
            floor_now = int(db._store.get("floor"))
            db.feed.release("drill_consumer")

        result = catch_up(self.primary, stale, packet_limit=10)
        # The library refuses to export from a revision below floor.
        rejected_read = None
        with Database(self.primary) as db:
            try:
                db.replication.export(stale_floor_revision)
            except SnapshotTooOld as exc:
                rejected_read = {"error": type(exc).__name__, "message": str(exc)}
        entry = self.scenario(
            "reclaimed_source_history",
            stale_replica_revision=stale_floor_revision,
            compaction=compact,
            source_floor=floor_now,
            catch_up={"status": result["status"], "reason": result["reason"], "events": result["events"]},
            export_below_floor=rejected_read,
            remediation="re-bootstrap the replica from a fresh verified backup; "
                        "incremental catch-up is impossible once the source floor passes the replica revision",
        )
        if rejected_read is None:
            raise DrillError("source served an export below its floor")
        if result["status"] not in ("requires_rebootstrap", "failed"):
            raise DrillError("stale replica unexpectedly caught up")
        return {"result": result, "entry": entry, "stale": stale}

    # -- phase 5: restore after interruption, from a fresh backup --------
    def phase_rebootstrap(self, bootstrap_manifest: dict) -> dict:
        """Recover a stale replica from a fresh backup, then catch up."""
        fresh = produce_backup(self.primary, self.layout["backups"], label="recovery")
        verified = verify_manifest(self.layout["backups"] / fresh["archive"]["name"])
        target = self.layout["replica"] / "rebootstrapped.db"
        receipt = Archives.restore(self.layout["backups"] / fresh["archive"]["name"], target,
                                   fresh["archive"]["sha256"], follower=True)
        result = catch_up(self.primary, target, packet_limit=10)
        facts = read_facts(target)
        entry = self.scenario(
            "rebootstrap_after_reclaim",
            backup={"sha256": fresh["archive"]["sha256"], "revision": fresh["archive"]["revision"],
                    "verified": verified["ok"]},
            bootstrap={"revision": receipt["revision"], "mode": receipt["mode"]},
            catch_up={"status": result["status"], "replica_revision": result["replica_revision"],
                      "target_head": result["target_head"], "events": result["events"]},
            replica_facts=summary(facts),
            ok=result["status"] == "caught_up",
        )
        if result["status"] != "caught_up":
            raise DrillError("re-bootstrapped replica did not catch up")
        return {"entry": entry, "path": target}

    # -- phase 6: backup failure handling --------------------------------
    def phase_backup_failure(self) -> dict:
        """Exercise both refusal paths around backup publication."""
        blocked_dir = self.layout["backups"] / "blocked-store"
        blocked_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(blocked_dir, 0o555)
        unwritable = None
        unwritable_report = None
        try:
            produce_backup(self.primary, blocked_dir, label="blocked", attempts=2, retry_delay=0.05)
        except BackupError as exc:
            unwritable = str(exc)
            unwritable_report = getattr(exc, "report", None)
        finally:
            os.chmod(blocked_dir, 0o755)
        if unwritable is None:
            raise DrillError("backup into a read-only store unexpectedly succeeded")
        if not unwritable_report or not unwritable_report.get("attempts"):
            raise DrillError("failed backup did not record its attempts")
        if any(item.get("result") == "published" for item in unwritable_report["attempts"]):
            raise DrillError("failed backup recorded a published attempt")

        # Occupied destination: a plain file where the archive would land.  The
        # library refuses to overwrite; the tool retries under a fresh name and
        # the occupied file is left byte-identical.
        occupied_dir = self.layout["backups"] / "occupied-store"
        occupied_dir.mkdir(parents=True, exist_ok=True)
        occupied = occupied_dir / "keep-me-a1.db"
        payload = b"not a database; must not be overwritten"
        occupied.write_bytes(payload)
        before_digest = file_digest(occupied)
        recovered = produce_backup(self.primary, occupied_dir, label="keep-me", attempts=3,
                                   retry_delay=0.02, preferred_name="keep-me")
        after_digest = file_digest(occupied)
        occupied_rejection = reject_bare_copy(occupied)
        accepted = verify_manifest(occupied_dir / recovered["archive"]["name"])
        skipped = [item for item in recovered["attempts"] if item.get("result") == "skipped-existing"]

        entry = self.scenario(
            "backup_failure_handling",
            unwritable_store={"path": str(blocked_dir), "error": unwritable,
                              "attempts": unwritable_report["attempts"]},
            occupied_destination={"path": str(occupied), "bytes": len(payload),
                                  "unchanged": before_digest == after_digest,
                                  "digest": before_digest,
                                  "rejected_as_backup": occupied_rejection},
            retry_after_occupied={"chosen_archive": recovered["archive"]["name"],
                                  "skipped_occupied": skipped,
                                  "attempts": recovered["attempts"],
                                  "verified": accepted["ok"]},
            stale_temporary_cleanup=prune_stale_temporaries(self.layout["backups"], apply=False),
            note="a failed backup never publishes an archive; retries use fresh names; "
                 "an existing destination is never overwritten",
        )
        if before_digest != after_digest:
            raise DrillError("backup overwrote an existing destination")
        if not skipped:
            raise DrillError("occupied destination did not force a renamed retry")
        if not accepted["ok"]:
            raise DrillError("retried backup did not verify")
        return {"entry": entry}

    # -- phase 7: final independent cross-check --------------------------
    def phase_final_check(self, replica_path: Path, recovered_source: dict) -> dict:
        """Compare the replica with the source state it was recovered to.

        ``recovered_source`` is a fact snapshot taken at the revision the
        replica reached.  Later scenarios intentionally push the source
        further (and compact it), so comparing against the current head would
        mistake deliberate post-recovery drift for data loss.
        """
        source = read_facts(self.primary)
        target = read_facts(replica_path)
        recorded = {"head": recovered_source["head"], "floor": target["floor"],
                    "catalog_epoch": recovered_source["catalog_epoch"],
                    "schema_sha256": recovered_source["schema_sha256"],
                    "index_definition_sha256": recovered_source["index_definition_sha256"],
                    "documents_sha256": recovered_source["documents"]["sha256"],
                    "documents_count": recovered_source["documents"]["count"]}
        checks = compare_snapshot(recorded, target, identity="follower",
                                  source_database_id=recovered_source["identity"]["database_id"])
        checks = [item for item in checks
                  if item["check"] not in ("head_matches_backup", "floor_matches_backup",
                                           "archive_checksum", "archive_checksum_verified_by_library")]
        checks.append({"check": "replica_head_equals_recovered_revision",
                       "ok": target["head"] == recovered_source["head"],
                       "expected": recovered_source["head"], "actual": target["head"]})
        entry = self.scenario("final_fact_comparison",
                              recovered_source=summary(recovered_source),
                              replica=summary(target), current_source=summary(source),
                              post_recovery_source_commits=source["head"] - recovered_source["head"],
                              checks=checks, ok=all(item["ok"] for item in checks))
        if not all(item["ok"] for item in checks):
            raise DrillError("final comparison failed: %s"
                             % [item for item in checks if not item["ok"]])
        return {"entry": entry, "source": source, "replica": target,
                "recovered_source": recovered_source}

    # -- runner ----------------------------------------------------------
    def run(self) -> dict:
        started = time.time()
        try:
            self.prepare()
            self.scenario("environment", **environment())
            backup_phase = self.phase_backup_under_write()
            manifest = backup_phase["manifest"]
            restore_phase = self.phase_restore(manifest)
            catch_phase = self.phase_catch_up(restore_phase["path"], manifest)
            recovered_source = catch_phase["recovered_source_facts"]
            catch_revision = catch_phase["result"]["replica_revision"]
            self.phase_backup_failure()
            self.phase_reclaimed_history(manifest)
            rebootstrap = self.phase_rebootstrap(manifest)
            final = self.phase_final_check(catch_phase["replica"], recovered_source)
            self.report["outcome"] = "success"
            self.report["recovery_point"] = {
                "archived_revision": manifest["facts"]["head"],
                "archived_sha256": manifest["archive"]["sha256"],
                "source_head_at_backup": manifest["source"]["head_after"],
                "rpo_commits_at_backup": manifest["source"]["head_after"] - manifest["facts"]["head"],
                "recovered_revision": catch_revision,
                "source_head_when_recovered": recovered_source["head"],
                "rpo_commits_after_catch_up": 0,
                "source_head_at_end": final["source"]["head"],
                "source_floor_at_end": final["source"]["floor"],
                "post_recovery_source_commits": final["source"]["head"] - recovered_source["head"],
                "rebootstrapped_replica": str(rebootstrap["path"]),
                "note": "Catch-up closed the gap to zero commits; later phases intentionally "
                        "advance and compact the source, so the final source head is ahead again.",
            }
            self.report["boundaries"] = availability_boundaries()
            self.report["evidence"] = final["replica"]["documents"]
            self.report["recovery_manifest"] = {
                "accepted_archive": manifest["archive"]["name"],
                "path": manifest["archive"]["path"],
                "sha256": manifest["archive"]["sha256"],
                "revision": manifest["facts"]["head"],
                "source_database_id": manifest["archive"]["database_id"],
                "verified_before_use": True,
                "restored_destinations": [
                    {"path": restore_phase["result"]["destination"], "mode": "primary",
                     "identity_rotated": True},
                    {"path": str(catch_phase["replica"]), "mode": "replica",
                     "upstream": manifest["archive"]["database_id"]},
                    {"path": str(rebootstrap["path"]), "mode": "replica",
                     "bootstrap_archive": rebootstrap["entry"]["backup"]["sha256"],
                     "upstream": manifest["archive"]["database_id"],
                     "identity_preserved": True},
                ],
            }
            self.report["rto_seconds"] = {
                "full_drill": round(time.time() - started, 3),
                "restore_and_verify": restore_phase["elapsed_seconds"],
                "catch_up_to_head": catch_phase["entry"]["resume"].get("elapsed_seconds"),
                "note": "wall-clock on this host; scales with database size and packet count",
            }
        except Exception as exc:  # noqa: BLE001 - recorded as evidence
            self.report["outcome"] = "failed"
            self.report["errors"].append({"error": type(exc).__name__, "message": str(exc),
                                          "traceback": traceback.format_exc().splitlines()[-8:]})
        self.report["finished_at"] = utc_now()
        self.report["duration_seconds"] = round(time.time() - started, 3)
        self.report["warnings"] = [{"warning": "post-drill files retained for inspection",
                                    "root": str(self.root)}]
        if getattr(self, "_prepared", False):
            self.write_evidence()
        else:
            # Never write into a pre-existing root; the caller gets the failure
            # report on stdout instead of overwriting someone else's evidence.
            self.report["evidence_written"] = False
        return self.report

    def write_evidence(self) -> None:
        evidence = self.layout["evidence"]
        evidence.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.report, ensure_ascii=False, sort_keys=True, indent=2, default=_json_default)
        (evidence / "drill-report.json").write_text(payload + "\n", encoding="utf-8")
        (evidence / "drill-report.md").write_text(render_markdown(self.report), encoding="utf-8")
        self.report["evidence_written"] = True
        sums = {}
        for directory in ("backups", "restore", "replica", "primary"):
            folder = self.layout[directory]
            if folder.is_dir():
                for item in sorted(folder.rglob("*")):
                    if item.is_file() and not item.name.endswith("-wal") and not item.name.endswith("-shm"):
                        sums[str(item.relative_to(self.root))] = file_digest(item)
        (evidence / "artifacts.sha256").write_text(
            "".join("%s  %s\n" % (digest, name) for name, digest in sorted(sums.items())), encoding="utf-8")


def environment() -> dict:
    import platform
    import sqlite3
    import strataleaf
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "strataleaf": getattr(strataleaf, "__version__", "unknown"),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
    }


def availability_boundaries() -> dict:
    return {
        "topology": "single host; primary and recovery directories on local filesystem",
        "source_of_truth": "the live primary SQLite WAL database",
        "backup_mechanism": "SQLite online backup API invoked by Database.archives.backup",
        "recovery_point": "the archived revision; writes committed after it are only recovered "
                          "through replication packets retained above the source floor",
        "rpo": "bounded by backup cadence plus retained replication range; not zero",
        "rto": "restore + verification time; the drill measures it",
        "not_covered": [
            "多主机 / 共享网络盘",
            "主库磁盘整体损毁而备份介质同时丢失",
            "源 floor 已越过副本修订且无新备份时的时间点恢复",
            "提升副本为主库的双向切换（基线不提供提升接口）",
        ],
        "preconditions": [
            "恢复目录与主库目录物理隔离且可写",
            "恢复使用新的未占用路径，禁止覆盖",
            "接收方必须绑定同一 upstream 且包连续",
        ],
    }


def render_markdown(report: dict) -> str:
    lines = ["# StrataLeaf 单机灾备演练报告", ""]
    lines.append("- 工具: %s %s" % (report.get("tool"), report.get("version")))
    lines.append("- 开始: %s" % report.get("started_at"))
    lines.append("- 结束: %s" % report.get("finished_at"))
    lines.append("- 用时: %s 秒" % report.get("duration_seconds"))
    lines.append("- 结论: **%s**" % report.get("outcome"))
    lines.append("")
    if report.get("recovery_point"):
        rp = report["recovery_point"]
        rows = [
            ("归档修订", rp.get("archived_revision")),
            ("归档摘要", "`%s`" % rp.get("archived_sha256")),
            ("备份时源 head", rp.get("source_head_at_backup")),
            ("备份时未覆盖提交", rp.get("rpo_commits_at_backup")),
            ("最终恢复修订", rp.get("recovered_revision")),
            ("恢复时源 head", rp.get("source_head_when_recovered")),
            ("追平后滞后提交", rp.get("rpo_commits_after_catch_up")),
            ("结束时源 head", rp.get("source_head_at_end")),
            ("结束时源 floor", rp.get("source_floor_at_end")),
            ("之后新增源提交", rp.get("post_recovery_source_commits")),
            ("重新引导副本", rp.get("rebootstrapped_replica")),
        ]
        lines += ["## 恢复点与可用性", "", "| 项 | 值 |", "| --- | --- |"]
        lines += ["| %s | %s |" % (key, value) for key, value in rows]
        lines.append("")
        if rp.get("note"):
            lines += ["> " + rp["note"], ""]
    lines += ["## 场景", ""]
    for item in report.get("scenarios", []):
        name = item.get("scenario")
        lines.append("### %s" % name)
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(item, ensure_ascii=False, sort_keys=True, indent=2, default=_json_default)[:12000])
        lines.append("```")
        lines.append("")
    if report.get("boundaries"):
        lines += ["## 可用性边界", ""]
        for key, value in report["boundaries"].items():
            lines.append("- **%s**: %s" % (key, value))
        lines.append("")
    if report.get("errors"):
        lines += ["## 错误", "", "```json",
                  json.dumps(report["errors"], ensure_ascii=False, indent=2, default=_json_default), "```", ""]
    return "\n".join(lines) + "\n"
