"""Backup production and independent verification.

A drill backup is only accepted when it carries a manifest produced by this
module.  The manifest records *how* the archive was produced (StrataLeaf's
``archives.backup``, i.e. the SQLite online backup API), the SHA-256 of the
published file and the logical facts at its revision.  A file dropped into
the store by hand - including a raw copy of the live ``*.db`` - has no
manifest and is rejected by :func:`verify_manifest`.

Backup failures are retried with fresh unique names because the library
refuses to overwrite an existing destination.  Every attempt is recorded in
the manifest of the successful archive (or in a failure report when all
attempts fail), and stale ``.strataleaf-*`` temporary files left by a killed
process are reported rather than silently deleted.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from strataleaf import Database

from .facts import FactsError, compare_snapshot, file_digest, read_facts, summary

MANIFEST_SUFFIX = ".manifest.json"
METHOD = "strataleaf.archives.backup (SQLite online backup API)"
TEMP_PREFIXES = (".strataleaf-", ".strataleaf-restore-")


class BackupError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def load_manifest(archive: str | Path) -> dict:
    archive = Path(archive).resolve()
    path = archive.with_name(archive.name + MANIFEST_SUFFIX)
    if not path.is_file():
        raise BackupError("no manifest beside archive; a bare copy is not an accepted backup: " + str(archive))
    with open(path, encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest.get("method") != METHOD:
        raise BackupError("manifest method is not the sanctioned online-backup path: " + str(manifest.get("method")))
    if manifest.get("archive", {}).get("sha256") is None:
        raise BackupError("manifest does not pin an archive checksum")
    return manifest


def stale_temporaries(directory: str | Path) -> list:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted(str(item) for item in directory.iterdir()
                  if item.name.startswith(TEMP_PREFIXES) and item.is_file())


def prune_stale_temporaries(directory: str | Path, apply: bool = False) -> dict:
    """Remove temporary sidecars whose main temp file is already gone.

    ``Archives.backup`` unlinks its temporary main file but a live SQLite
    connection can leave ``-wal``/``-shm`` siblings behind.  Those orphans are
    safe to delete because the database they belonged to no longer exists.
    Dry-run by default; never touches files that are not ``.strataleaf-*``.
    """
    directory = Path(directory)
    orphans, kept = [], []
    for item in stale_temporaries(directory):
        name = Path(item).name
        base = name
        for suffix in ("-wal", "-shm"):
            if name.endswith(suffix):
                base = name[: -len(suffix)]
                break
        main = directory / base
        if base == name:
            kept.append(item)  # a main temp file still exists: in-flight backup
        elif not main.exists():
            orphans.append(item)
        else:
            kept.append(item)
    removed = []
    if apply:
        for item in orphans:
            try:
                os.unlink(item)
                removed.append(item)
            except OSError:
                pass
    return {"directory": str(directory), "orphans": orphans, "kept": kept,
            "removed": removed, "applied": apply}


def produce_backup(source: str | Path, store: str | Path, label: str | None = None,
                   attempts: int = 3, retry_delay: float = 0.2,
                   preferred_name: str | None = None) -> dict:
    """Take a verified online backup into ``store`` and publish a manifest.

    ``preferred_name`` fixes the first attempt's stem (without ``.db``).  If
    that name is already published, the library refuses to overwrite and the
    tool retries under a fresh name, which lets a drill exercise the
    occupied-destination path deterministically.
    """
    source = Path(source).resolve()
    store = Path(store).resolve()
    store.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        raise BackupError("source database does not exist: " + str(source))

    before = read_facts(source, hash_file=False)
    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = preferred_name or "%s-%s" % (label or "backup", tag)
    log = []
    last_error = None

    for attempt in range(1, attempts + 1):
        name = stem + "-a%d" % attempt
        destination = store / (name + ".db")
        if destination.exists():
            log.append({"attempt": attempt, "name": destination.name, "result": "skipped-existing"})
            destination = store / ("%s-retry%d-%s.db" % (stem, attempt, uuid.uuid4().hex[:6]))
        started = time.time()
        try:
            with Database(source) as db:
                receipt = db.archives.backup(destination)
            # An archive must be self-contained: a non-empty WAL beside it would
            # mean the published main file does not carry all committed pages.
            sidecar = destination.with_name(destination.name + "-wal")
            if sidecar.exists() and sidecar.stat().st_size:
                raise BackupError("published archive depends on a non-empty WAL sidecar")
            facts = read_facts(destination, hash_file=True)
            manifest = {
                "tool": "strataleaf-dr",
                "version": "1.0",
                "created_at": utc_now(),
                "method": METHOD,
                "label": label or "backup",
                "source": {
                    "path": str(source),
                    "database_id": before["identity"]["database_id"],
                    "head_before": before["head"],
                    "head_after": None,  # filled below from a re-read
                    "floor": before["floor"],
                    "catalog_epoch": before["catalog_epoch"],
                },
                "archive": {
                    "path": str(destination),
                    "name": destination.name,
                    "sha256": receipt["sha256"],
                    "revision": receipt["revision"],
                    "floor": receipt["floor"],
                    "database_id": receipt["database_id"],
                    "file_bytes": destination.stat().st_size,
                },
                "facts": {
                    "head": facts["head"],
                    "floor": facts["floor"],
                    "catalog_epoch": facts["catalog_epoch"],
                    "documents_count": facts["documents"]["count"],
                    "documents_sha256": facts["documents"]["sha256"],
                    "schema_sha256": facts["schema_sha256"],
                    "index_definition_sha256": facts["index_definition_sha256"],
                    "index_projection_ok": facts["index_projection"].get("ok"),
                    "commit_contiguity": facts["commit_contiguity"]["ok"],
                },
                "attempts": log + [{"attempt": attempt, "name": destination.name, "result": "published",
                                    "seconds": round(time.time() - started, 3)}],
                "stale_temporaries_observed": stale_temporaries(store),
            }
            if not facts["ok"]:
                raise BackupError("archive failed independent fact verification: " + ",".join(facts["issues"]))
            after = read_facts(source)
            manifest["source"]["head_after"] = after["head"]
            # The copy runs concurrently with writes, so its revision lies
            # somewhere between the pre- and post-copy head.  It must still be
            # inside the source's retained history.
            if not before["floor"] <= facts["head"] <= after["head"]:
                raise BackupError("archived revision %d is outside the source read window %d..%d"
                                  % (facts["head"], before["floor"], after["head"]))
            # Strongest check available without trusting the copy: compare the
            # archived revision's content digest against the live source at the
            # same revision.  Skip only if the source compacted past it.
            if after["floor"] <= facts["head"]:
                live = read_facts(source, revision=facts["head"])
                if live["documents"]["sha256"] != facts["documents"]["sha256"]:
                    raise BackupError("archived documents differ from source at revision %d" % facts["head"])
                manifest["source"]["verified_against_source_at"] = facts["head"]
            if manifest["archive"]["sha256"] != file_digest(destination):
                raise BackupError("published archive changed after checksumming")
            _write_json_atomic(destination.with_name(destination.name + MANIFEST_SUFFIX), manifest)
            return manifest
        except Exception as exc:  # noqa: BLE001 - operational retry boundary
            last_error = exc
            log.append({"attempt": attempt, "name": destination.name, "result": "failed",
                        "error": type(exc).__name__, "message": str(exc),
                        "seconds": round(time.time() - started, 3)})
            if destination.exists():
                # A published-but-unverifiable archive must never be accepted.
                failed = destination.with_name(destination.name + ".rejected")
                os.replace(destination, failed)
            if attempt < attempts:
                time.sleep(retry_delay)
    report = {"tool": "strataleaf-dr", "version": "1.0", "created_at": utc_now(), "label": label or "backup",
              "method": METHOD, "source": str(source), "attempts": log,
              "error": "%s: %s" % (type(last_error).__name__, last_error)}
    report_path = store / (stem + ".failed" + MANIFEST_SUFFIX)
    try:
        _write_json_atomic(report_path, report)
        report["failure_report"] = str(report_path)
    except OSError as write_error:
        # A fully read-only store cannot accept even the failure record; the
        # caller still receives the structured attempts below.
        report["failure_report"] = None
        report["failure_report_error"] = "%s: %s" % (type(write_error).__name__, write_error)
    error = BackupError(report["error"])
    error.report = report
    raise error


def verify_manifest(archive: str | Path) -> dict:
    """Re-verify an archived backup from its manifest, without trusting it."""
    archive = Path(archive).resolve()
    manifest = load_manifest(archive)
    checks = []
    if not archive.is_file():
        raise BackupError("archive file is missing: " + str(archive))
    digest_now = file_digest(archive)
    checks.append({"check": "archive_sha256", "ok": digest_now == manifest["archive"]["sha256"],
                   "expected": manifest["archive"]["sha256"], "actual": digest_now})
    facts = read_facts(archive, revision=manifest["facts"]["head"], hash_file=False)
    recorded = manifest["facts"]
    pairs = [
        ("head", recorded["head"], facts["head"]),
        ("floor", recorded["floor"], facts["floor"]),
        ("catalog_epoch", recorded["catalog_epoch"], facts["catalog_epoch"]),
        ("documents_count", recorded["documents_count"], facts["documents"]["count"]),
        ("documents_sha256", recorded["documents_sha256"], facts["documents"]["sha256"]),
        ("schema_sha256", recorded["schema_sha256"], facts["schema_sha256"]),
        ("index_definition_sha256", recorded["index_definition_sha256"], facts["index_definition_sha256"]),
        ("index_projection_ok", recorded["index_projection_ok"], facts["index_projection"].get("ok")),
        ("commit_contiguity", recorded["commit_contiguity"], facts["commit_contiguity"]["ok"]),
        ("database_id", manifest["archive"]["database_id"], facts["identity"]["database_id"]),
    ]
    for name, expected, actual in pairs:
        checks.append({"check": name, "ok": expected == actual, "expected": expected, "actual": actual})
    checks.append({"check": "sqlite_integrity", "ok": facts["sqlite_integrity"] == ["ok"],
                   "expected": ["ok"], "actual": facts["sqlite_integrity"]})
    checks.append({"check": "foreign_keys", "ok": facts["foreign_key_violations"] == [],
                   "expected": [], "actual": facts["foreign_key_violations"]})
    return {"archive": str(archive), "manifest": manifest, "checks": checks,
            "ok": all(item["ok"] for item in checks), "facts": summary(facts)}


def restore_and_verify(archive: str | Path, destination: str | Path, *, mode: str = "primary",
                       expected_sha256: str | None = None) -> dict:
    """Restore with the library API, then compare independent facts."""
    from strataleaf import Archives

    archive, destination = Path(archive).resolve(), Path(destination).resolve()
    manifest = load_manifest(archive)
    checksum = expected_sha256 or manifest["archive"]["sha256"]
    receipt = Archives.restore(archive, destination, checksum, follower=(mode == "follower"))
    facts = read_facts(destination, hash_file=True)
    recorded = manifest["facts"]
    snapshot = {
        "head": recorded["head"],
        "floor": recorded["floor"],
        "catalog_epoch": recorded["catalog_epoch"],
        "schema_sha256": recorded["schema_sha256"],
        "index_definition_sha256": recorded["index_definition_sha256"],
        "documents_sha256": recorded["documents_sha256"],
        "documents_count": recorded["documents_count"],
    }
    checks = compare_snapshot(snapshot, facts, identity=mode,
                              source_database_id=manifest["archive"]["database_id"],
                              expected_sha256=checksum)
    return {"archive": str(archive), "destination": str(destination), "mode": mode,
            "receipt": receipt, "checks": checks, "ok": all(item["ok"] for item in checks),
            "facts": summary(facts)}


def reject_bare_copy(path: str | Path) -> dict:
    """Explain why a manifest-less file is not an accepted backup."""
    path = Path(path).resolve()
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    sidecars = {suffix: {"path": str(item), "bytes": item.stat().st_size}
                for suffix, item in (("-wal", wal), ("-shm", shm)) if item.exists()}
    try:
        facts = read_facts(path, hash_file=True)
    except FactsError as exc:
        return {"accepted": False, "reason": "not a readable StrataLeaf database",
                "detail": str(exc), "sidecars": sidecars}
    reasons = ["no manifest; origin unverifiable (raw copy suspected)"]
    if sidecars:
        reasons.append("live sidecar files are present, which indicates a copy of an active database")
    return {"accepted": False, "reason": "; ".join(reasons),
            "detail": "accepted backups must be produced by archives.backup and carry a manifest",
            "sidecars": sidecars, "observed": summary(facts)}


def compare_bare_copy(source: str | Path, destination: str | Path) -> dict:
    """Copy the live main file only, then show what that copy cannot prove.

    This deliberately reproduces the anti-pattern the drill forbids so the
    report contains direct evidence of the difference rather than an
    assertion.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    live = read_facts(source)
    source_wal = source.with_name(source.name + "-wal")
    wal_bytes = source_wal.stat().st_size if source_wal.exists() else 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    try:
        copied = read_facts(destination, hash_file=True)
        copied_facts = summary(copied)
        readable = True
    except FactsError as exc:
        copied_facts, readable = {"error": str(exc)}, False
    return {
        "source": str(source),
        "copy": str(destination),
        "source_wal_bytes": wal_bytes,
        "copy_readable": readable,
        "copy_facts": copied_facts,
        "source_head": live["head"],
        "copied_head": copied_facts.get("head") if readable else None,
        "accepted_as_backup": reject_bare_copy(destination)["accepted"],
        "why_rejected": [
            "a main-file copy taken while the WAL has uncheckpointed frames can omit committed data",
            "the copy has no manifest, so its revision, schema, index and content facts are unverified",
            "the library refuses to treat an existing file as a backup target",
        ],
    }
