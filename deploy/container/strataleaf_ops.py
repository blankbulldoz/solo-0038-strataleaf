#!/usr/bin/env python3
"""Operator/supervisor tool for a StrataLeaf service in a constrained container.

This module is the executable form of deploy/container/service.conf. It is
standard-library only and never imports the service code it manages: the
operator must be able to inspect and recover the database even when the
application is broken.

Commands
--------
  preflight     Validate volumes, ownership, permissions, free space, and the
                filesystem type before the service is allowed to start.
  probe         Orchestrator health check. Returns 0 only when *commit
                durability and writer availability* are observable, not merely
                when a process or file exists.
  space         Report capacity with the same reserve floors as preflight.
  verify        Recovery verification: integrity, commit continuity, retained
                floor, index projection, and an optional logical fingerprint.
  supervise     Run the reference service workload under the documented
                shutdown/exit ordering. Used by the entrypoint and the drills.
  drill         Run the reproducible normal-stop and SIGKILL drills end-to-end
                and emit JSON + logs + index report artifacts.
  stop          Send SIGTERM to a supervised run described by its state dir.

Exit codes (kept in sync with service.conf):
  0  ok
  64 usage error
  70 internal error
  75 drain/stop timeout (data is still recoverable; caller may SIGKILL)
  78 preflight/health gate failed
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import stat
import sys
import time
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_INTERNAL = 70
EXIT_DRAIN = 75
EXIT_PRECHECK = 78

NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "fuse.sshfs", "ceph", "glusterfs", "afs"}
PSEUDO_FS = {"tmpfs", "devtmpfs", "overlay", "ramfs", "proc", "sysfs", "autofs"}
# Device-backed filesystems that are local but not safe for SQLite durability.
UNSUPPORTED_LOCAL_FS = {"vfat", "msdos", "exfat", "ntfs3", "iso9660"}
# FUSE/virtio transports. A bind mount of a local disk may show up as virtiofs
# (e.g. Docker Desktop / Colima / OrbStack); it is treated as local, while
# sshfs/FUSE network transports are caught by name before this check.
FUSE_FS = {"fuse", "fuse.virtiofs"}


class GateError(Exception):
    """A startup or health gate failed; message is operator-facing."""


# Explicit opt-in for a *reviewed* local FUSE bind mount (Docker Desktop,
# Colima, OrbStack, or a virtiofs share of a local disk). Default off: an
# unrecognised FUSE transport may be sshfs/ceph and is then a network share.
ALLOW_FUSE = os.environ.get("STRATALEAF_ALLOW_FUSE", "") == "1"
# Drill-only escape hatch. When the host offers no persistent local volume
# (CI containers, the review sandbox), the drills run on overlay/tmpfs and
# record the actual filesystem class in their report. This flag must never be
# set for a real service; the shipped unit/compose/k8s definitions do not.
ALLOW_PSEUDO = os.environ.get("STRATALEAF_DRILL_ALLOW_PSEUDO", "") == "1"


# --------------------------------------------------------------------------
# configuration and filesystem facts
# --------------------------------------------------------------------------

def load_config(path: str | os.PathLike) -> dict:
    values: dict[str, str] = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise GateError(f"{path}:{number}: expected KEY=value")
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    required = {
        "STRATALEAF_UID", "STRATALEAF_GID", "STRATALEAF_USER",
        "STRATALEAF_DATA_DIR", "STRATALEAF_STATE_DIR", "STRATALEAF_DB_NAME",
        "STRATALEAF_MIN_FREE_BYTES", "STRATALEAF_MIN_FREE_PERCENT",
        "STRATALEAF_MAX_WAL_PERCENT", "STRATALEAF_BUSY_TIMEOUT",
        "STRATALEAF_DRAIN_SECONDS", "STRATALEAF_STOP_TIMEOUT_SECONDS",
        "STRATALEAF_KILL_GRACE_SECONDS",
    }
    missing = sorted(required - values.keys())
    if missing:
        raise GateError("missing keys in " + str(path) + ": " + ",".join(missing))
    config = dict(values)
    for key in ("STRATALEAF_UID", "STRATALEAF_GID"):
        config[key] = int(values[key])
    for key in ("STRATALEAF_MIN_FREE_BYTES", "STRATALEAF_MIN_FREE_PERCENT", "STRATALEAF_MAX_WAL_PERCENT"):
        config[key] = int(values[key])
    for key in ("STRATALEAF_BUSY_TIMEOUT", "STRATALEAF_DRAIN_SECONDS",
                "STRATALEAF_STOP_TIMEOUT_SECONDS", "STRATALEAF_KILL_GRACE_SECONDS"):
        config[key] = float(values[key])
    return config


def config_paths(config: dict) -> dict[str, Path]:
    data = Path(config["STRATALEAF_DATA_DIR"])
    state = Path(config["STRATALEAF_STATE_DIR"])
    db = data / config["STRATALEAF_DB_NAME"]
    return {
        "data_dir": data,
        "state_dir": state,
        "db": db,
        "wal": Path(str(db) + "-wal"),
        "shm": Path(str(db) + "-shm"),
    }


def _decode_mount(path: str) -> str:
    # /proc/self/mountinfo escapes space, tab, newline and backslash as \OOO.
    return path.encode("utf-8").decode("unicode_escape")


def mount_for(path: str | os.PathLike) -> dict:
    """Return the mount entry whose mount point contains *path*.

    Longest prefix wins, matching kernel lookup order. This is used only to
    *classify* the filesystem; it is not a security boundary.
    """
    target = os.path.realpath(path)
    best = None
    with open("/proc/self/mountinfo", "r", encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            if len(fields) < 10 or "-" not in fields:
                continue
            separator = fields.index("-")
            mount_point = os.path.realpath(_decode_mount(fields[4]))
            if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
                if best is None or len(mount_point) > len(best["mount_point"]):
                    best = {
                        "mount_point": mount_point,
                        "fstype": fields[separator + 1],
                        "source": fields[separator + 2],
                        "options": fields[separator + 3] if len(fields) > separator + 3 else "",
                        "device": fields[2],
                    }
    if best is None:
        raise GateError(f"cannot determine the mount for {target}")
    best["fstype"] = _refine_fuse_type(best)
    return best


def _refine_fuse_type(mount: dict) -> str:
    """Normalise the uninformative `fuse` fstype using the mount source/opts.

    /proc/mounts keeps the subtype in the device string (`host:/path`, `user@host:/`)
    while mountinfo often reports plain `fuse` with `subtype=` in super options.
    Without this, an sshfs mount could be mistaken for a local virtiofs share.
    """
    if mount["fstype"] != "fuse":
        return mount["fstype"]
    options = mount.get("options", "")
    source = mount.get("source", "")
    for marker, kind in (("subtype=sshfs", "fuse.sshfs"), ("subtype=ceph", "ceph"),
                         ("subtype=glusterfs", "glusterfs")):
        if marker in options:
            return kind
    if ":" in source and "/" in source:
        return "fuse.sshfs"
    return "fuse.virtiofs"


def classify_fs(fstype: str) -> tuple[str, str | None]:
    """Return (class, reason) for a filesystem type.

    class is one of: persistent-local, network, pseudo, unsupported-local,
    fuse-unknown. The last is refused by default because an unidentified FUSE
    transport cannot be proven to be a local block device; an operator with a
    known-good bind mount opts in via STRATALEAF_ALLOW_FUSE=1.
    """
    if fstype in NETWORK_FS:
        return "network", "network filesystems with multi-writer semantics are not supported"
    if fstype.startswith("fuse") and fstype not in FUSE_FS:
        return "network", "unrecognised FUSE transport; only local bind mounts are supported"
    if fstype in PSEUDO_FS:
        return "pseudo", "not a persistent volume; contents are lost and/or layered"
    if fstype in UNSUPPORTED_LOCAL_FS:
        return "unsupported-local", "lacks the POSIX locking/atomicity SQLite WAL requires"
    if fstype in FUSE_FS:
        return "fuse-unknown", ("FUSE transport: cannot prove a local block device; "
                                "set STRATALEAF_ALLOW_FUSE=1 only for a reviewed local bind mount")
    return "persistent-local", None


def free_space(path: str | os.PathLike) -> dict:
    info = os.statvfs(path)
    total = info.f_frsize * info.f_blocks
    available = info.f_frsize * info.f_bavail
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": total - available,
        "available_percent": (available / total * 100.0) if total else 0.0,
        "files_total": info.f_files,
        "files_available": info.f_favail,
    }


def space_floor_violations(config: dict, facts: dict) -> list[str]:
    problems = []
    if facts["available_bytes"] < config["STRATALEAF_MIN_FREE_BYTES"]:
        problems.append(
            f"available {facts['available_bytes']} bytes below reserve "
            f"{config['STRATALEAF_MIN_FREE_BYTES']}"
        )
    if facts["available_percent"] < config["STRATALEAF_MIN_FREE_PERCENT"]:
        problems.append(
            f"available {facts['available_percent']:.2f}% below reserve "
            f"{config['STRATALEAF_MIN_FREE_PERCENT']}%"
        )
    return problems


# --------------------------------------------------------------------------
# preflight gate
# --------------------------------------------------------------------------

def check_directory(config: dict, role: str, path: Path) -> dict:
    st = os.stat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise GateError(f"{role} {path} is not a directory")
    if st.st_uid != config["STRATALEAF_UID"] or st.st_gid != config["STRATALEAF_GID"]:
        raise GateError(
            f"{role} {path} is owned by {st.st_uid}:{st.st_gid}, "
            f"expected {config['STRATALEAF_UID']}:{config['STRATALEAF_GID']}"
        )
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o002:
        raise GateError(f"{role} {path} is world-writable ({mode:o})")
    if not mode & stat.S_IWUSR:
        raise GateError(f"{role} {path} is not writable by its owner ({mode:o})")
    mount = mount_for(path)
    kind, reason = classify_fs(mount["fstype"])
    if kind == "fuse-unknown" and ALLOW_FUSE:
        kind, reason = "persistent-local", None
    if kind in {"pseudo", "fuse-unknown"} and ALLOW_PSEUDO:
        kind, reason = "persistent-local", None
    if kind != "persistent-local":
        raise GateError(f"{role} {path} on {mount['fstype']} ({mount['mount_point']}): {reason}")
    return {
        "path": str(path), "role": role, "uid": st.st_uid, "gid": st.st_gid, "mode": f"{mode:o}",
        "fstype": mount["fstype"], "mount_point": mount["mount_point"],
        "fs_class": classify_fs(mount["fstype"])[0], "fs_policy": "enforced",
        "space": free_space(path),
    }


def preflight(config: dict, running_uid: int | None = None, running_gid: int | None = None) -> dict:
    """Validate the environment. Raises GateError with all problems joined.

    *running_uid*/*running_gid* default to the current process; the drills pass
    explicit values so they can simulate a misconfigured image.
    """
    running_uid = os.getuid() if running_uid is None else running_uid
    running_gid = os.getgid() if running_gid is None else running_gid
    problems: list[str] = []
    if running_uid == 0:
        problems.append("refusing to run as uid 0")
    if running_uid != config["STRATALEAF_UID"] or running_gid != config["STRATALEAF_GID"]:
        problems.append(
            f"process runs as {running_uid}:{running_gid}, expected "
            f"{config['STRATALEAF_UID']}:{config['STRATALEAF_GID']}"
        )
    paths = config_paths(config)
    directories = []
    for role, key in (("data volume", "data_dir"), ("state volume", "state_dir")):
        try:
            directories.append(check_directory(config, role, paths[key]))
        except (GateError, OSError) as exc:
            problems.append(str(exc))
    if paths["data_dir"] == paths["state_dir"]:
        problems.append("data and state directories must be distinct volumes")
    elif paths["data_dir"].resolve() == paths["state_dir"].resolve():
        problems.append("data and state directories resolve to the same path")
    for entry in directories:
        problems.extend(f"{entry['role']}: {problem}"
                        for problem in space_floor_violations(config, entry["space"]))
    db = paths["db"]
    if db.exists():
        if not db.is_file():
            problems.append(f"{db} exists but is not a regular file")
        else:
            st = os.stat(db)
            if st.st_uid != config["STRATALEAF_UID"]:
                problems.append(f"{db} is owned by uid {st.st_uid}, expected {config['STRATALEAF_UID']}")
            if stat.S_IMODE(st.st_mode) & 0o002:
                problems.append(f"{db} is world-writable")
    # Stale shm is harmless (SQLite recreates it), but a stray wal on a volume
    # where the main file is missing means someone copied files by hand.
    if paths["wal"].exists() and not db.exists():
        problems.append(f"{paths['wal']} exists without the main database file")
    if problems:
        raise GateError("; ".join(problems))
    return {
        "ok": True,
        "uid": running_uid, "gid": running_gid,
        "directories": directories,
        "db": str(db),
        "db_exists": db.exists(),
        "wal_exists": paths["wal"].exists(),
        "shm_exists": paths["shm"].exists(),
    }


# --------------------------------------------------------------------------
# health probe
# --------------------------------------------------------------------------

def read_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        return {"corrupt": True}


def probe(config: dict, kind: str = "readiness") -> dict:
    """Return a detailed health report; raise GateError when the check fails.

    Two distinct questions, because conflating them causes either restart
    loops or silent capacity leaks:

    * ``kind="liveness"`` — is this process able to make progress at all?
      Requires: volumes pass preflight, the database opens, committed metadata
      and content are visible, and the single-writer lock can be acquired
      within the busy timeout. A long reader does NOT fail liveness: WAL
      readers and the writer coexist by design.

    * ``kind="readiness"`` — is it safe to keep sending work here?
      Everything above, plus: the WAL is within ``STRATALEAF_MAX_WAL_PERCENT``
      of the volume, and a PASSIVE checkpoint either completed or left the WAL
      below that bound. When a long read pins the snapshot, the checkpoint
      cannot advance. The returned triple is ``(busy, log_frames,
      checkpointed_frames)`` and PASSIVE reports ``busy=0`` with a lagging
      checkpointed count — i.e. **no exception is raised**. Treating that
      silence as health is precisely the mistake this gate exists to prevent.

    Deliberately NOT sufficient on their own for either kind: pid alive, file
    present, open handle, or quick_check ok.
    """
    details: dict = {"checked_at": time.time(), "kind": kind}
    paths = config_paths(config)
    details["preflight"] = preflight(config)
    timeout = config["STRATALEAF_BUSY_TIMEOUT"]
    db = paths["db"]
    if not db.exists():
        raise GateError("database file does not exist")
    details["files"] = {
        name: ({"size": os.stat(path).st_size} if path.exists() else None)
        for name, path in (("db", paths["db"]), ("wal", paths["wal"]), ("shm", paths["shm"]))
    }
    uri = db.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)
        try:
            connection.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            # quick_check reads through the WAL, so it is safe to run while the
            # service is live; wal_checkpoint is NOT (it needs a write lock).
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            details["quick_check"] = integrity
            if integrity != "ok":
                raise GateError("SQLite quick_check failed: " + str(integrity))
            meta = dict(connection.execute("SELECT key,value FROM meta"))
            for key in ("head", "floor", "format", "database_id", "mode", "upstream"):
                if key not in meta:
                    raise GateError("missing storage metadata: " + key)
            if meta["format"] != "1":
                raise GateError("unsupported on-disk format " + meta["format"])
            head, floor = int(meta["head"]), int(meta["floor"])
            if not 0 <= floor <= head:
                raise GateError(f"invalid watermarks floor={floor} head={head}")
            details["watermarks"] = {"head": head, "floor": floor, "mode": meta["mode"]}
            # A reader must observe a consistent document count at head. This
            # catches a file that exists but whose schema/data did not survive.
            collections = connection.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
            latest = connection.execute("SELECT COUNT(*) FROM versions WHERE rev<=?", (head,)).fetchone()[0]
            details["visibility"] = {"collections": collections, "versions": latest}
            # Short probe transaction: proves a reader can start and finish
            # while the service is writing, without holding the read lock.
            connection.execute("BEGIN")
            try:
                connection.execute("SELECT COUNT(*) FROM commits WHERE rev>?", (floor,)).fetchone()
            finally:
                connection.execute("ROLLBACK")
        finally:
            connection.close()
    except GateError:
        raise
    except (sqlite3.Error, ValueError) as exc:
        raise GateError(f"read-only probe failed: {exc}") from exc

    # A replica rejects primary writes by design; asking for the write lock
    # there would report the intended state as an outage.
    if details["watermarks"]["mode"] != "primary":
        details["write_lock"] = "skipped (mode=" + details["watermarks"]["mode"] + ")"
        details["status"] = "healthy"
        return details

    # The writer lock is the real "can this service make progress?" question.
    # Opening read-write can create files; that is acceptable and matches what
    # the service itself does.
    writable = sqlite3.connect(str(db), timeout=timeout, isolation_level=None)
    started = time.monotonic()
    try:
        writable.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        writable.execute("BEGIN IMMEDIATE")
        writable.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        raise GateError(f"writer lock unavailable within {timeout}s: {exc}") from exc
    finally:
        writable.close()
    details["write_lock_ms"] = round((time.monotonic() - started) * 1000, 3)
    details["write_lock"] = "acquired"

    # Checkpoint liveness runs on a throwaway connection. -wal/-shm removal is
    # the service's own close, not the probe's business.
    passive = checkpoint(paths["db"], "PASSIVE", timeout)
    details["checkpoint"] = passive
    capacity = free_space(paths["data_dir"])
    wal_bytes = paths["wal"].stat().st_size if paths["wal"].exists() else 0
    wal_percent = (wal_bytes / capacity["total_bytes"] * 100.0) if capacity["total_bytes"] else 0.0
    details["wal"] = {"bytes": wal_bytes, "percent_of_volume": round(wal_percent, 4),
                      "limit_percent": config["STRATALEAF_MAX_WAL_PERCENT"]}
    lagging = passive["busy"] != 0 or passive["log_frames"] > passive["checkpointed_frames"]
    details["checkpoint_lag"] = lagging
    if kind == "liveness":
        details["status"] = "healthy"
        return details
    if kind != "readiness":
        raise GateError("unknown probe kind: " + kind)
    if wal_percent > config["STRATALEAF_MAX_WAL_PERCENT"]:
        details["status"] = "not-ready"
        raise GateError(
            f"WAL is {wal_bytes} bytes ({wal_percent:.2f}% of volume), above the "
            f"{config['STRATALEAF_MAX_WAL_PERCENT']}% bound; checkpoint cannot advance "
            f"while a reader or writer holds the snapshot")
    if lagging:
        # Under the hard bound, but progress is blocked: report, don't kill.
        # Callers that route work here should back off; the orchestrator's
        # readiness endpoint is the right place to reflect this, not liveness.
        details["status"] = "degraded"
        details["reason"] = (f"checkpoint incomplete: busy={passive['busy']} "
                             f"log={passive['log_frames']} checkpointed={passive['checkpointed_frames']}")
        return details
    details["status"] = "healthy"
    return details


def checkpoint(db: Path, mode: str, timeout: float) -> dict:
    """Run a checkpoint and interpret its triple honestly.

    SQLite returns (busy, log_frames, checkpointed_frames). A non-zero busy or
    a lagging checkpointed count means progress was blocked, usually by a long
    reader; this is not a database error, but it is also not "healthy".
    """
    connection = sqlite3.connect(str(db), timeout=timeout, isolation_level=None)
    try:
        connection.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        row = connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    finally:
        connection.close()
    busy, log_frames, checkpointed = (int(value) for value in row)
    return {
        "mode": mode, "busy": busy, "log_frames": log_frames,
        "checkpointed_frames": checkpointed, "complete": busy == 0 and log_frames == checkpointed,
    }


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def _read_meta(connection: sqlite3.Connection) -> dict:
    return dict(connection.execute("SELECT key,value FROM meta"))


def verify(config: dict, deep: bool = True, expect_head: int | None = None) -> dict:
    """Recovery/inspection gate over a database at rest or in service.

    Runs on a read-only connection so it can never "fix" a problem by writing.
    Reimplements only the checks needed to judge recoverability; the service's
    own `diagnostics.inspect` remains authoritative for content fingerprints.
    """
    paths = config_paths(config)
    db = paths["db"]
    if not db.exists():
        raise GateError("database file does not exist")
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        # A zero-length WAL left by a clean stop is normal; a non-empty one is
        # also fine (SQLite recovers it). Record both so operators can tell the
        # difference between "pending recovery" and "already consolidated".
        wal_bytes = db.with_name(db.name + "-wal")
        wal_size = wal_bytes.stat().st_size if wal_bytes.exists() else None
        meta = _read_meta(connection)
        head, floor = int(meta["head"]), int(meta["floor"])
        issues: list[str] = []
        integrity = [row[0] for row in connection.execute(
            "PRAGMA integrity_check" if deep else "PRAGMA quick_check")]
        if integrity != ["ok"]:
            issues.append("SQLite physical integrity failure")
        references = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        if references:
            issues.append("broken foreign keys")
        retained = [row[0] for row in connection.execute(
            "SELECT rev FROM commits WHERE rev>? ORDER BY rev", (floor,))]
        if retained != list(range(floor + 1, head + 1)):
            issues.append("retained commit range is not contiguous")
        if not 0 <= floor <= head:
            issues.append("invalid revision watermarks")
        pins = {row["name"]: row["rev"] for row in connection.execute("SELECT name,rev FROM pins")}
        for name, revision in pins.items():
            if not floor <= revision <= head:
                issues.append("read pin outside retained history: " + name)
        consumers = {row["name"]: row["ack"] for row in connection.execute("SELECT name,ack FROM consumers")}
        for name, revision in consumers.items():
            if not floor <= revision <= head:
                issues.append("consumer position outside retained history: " + name)
        # Recompute the current index projection from visible documents.
        mismatches = 0
        if deep:
            from_index = {(row["collection"], row["name"], row["token"], row["key"]
                          ) for row in connection.execute(
                "SELECT collection,name,token,key FROM index_values")}
            expected = set()
            for definition in connection.execute("SELECT * FROM index_defs"):
                for row in _visible_rows(connection, definition["collection"], head):
                    token = _scalar_token(_field(json.loads(row["body"]), definition["field"]))
                    if token is not None:
                        expected.add((definition["collection"], definition["name"], token, row["key"]))
            mismatches = len(expected ^ from_index)
            if mismatches:
                issues.append(f"index projection differs in {mismatches} entries")
        counts = {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                  for table in ("collections", "versions", "commits", "index_defs",
                                "index_values", "pins", "consumers")}
        fingerprint = _fingerprint(connection, head)
        if expect_head is not None and head != expect_head:
            issues.append(f"head is {head}, expected {expect_head}")
        result = {
            "ok": not issues, "issues": issues,
            "head": head, "floor": floor, "mode": meta["mode"],
            "database_id": meta["database_id"], "counts": counts,
            "sqlite": integrity, "foreign_keys": references,
            "pins": pins, "consumers": consumers,
            "index_mismatches": mismatches, "fingerprint": fingerprint,
            "wal_bytes_at_verify": wal_size,
        }
        if issues:
            raise GateError("verification failed: " + "; ".join(issues))
        return result
    finally:
        connection.close()


def _visible_rows(connection, collection, revision):
    return connection.execute("""SELECT v.* FROM versions v JOIN
      (SELECT key,MAX(rev) AS r FROM versions WHERE collection=? AND rev<=? GROUP BY key) latest
      ON v.collection=? AND v.key=latest.key AND v.rev=latest.r
      WHERE v.body IS NOT NULL ORDER BY v.key""", (collection, revision, collection)).fetchall()


def _field(document, path):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


class _Missing:
    pass


_MISSING = _Missing()


def _scalar_token(value):
    if value is _MISSING or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "b:" + str(int(value))
    if isinstance(value, (int, float)):
        return "n:" + str(value) if isinstance(value, int) else (
            "n:" + str(int(value)) if value.is_integer() else "n:" + repr(value))
    return "v:" + json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fingerprint(connection, revision) -> dict:
    import hashlib
    entries = []
    for collection in connection.execute("SELECT name FROM collections ORDER BY name"):
        for row in _visible_rows(connection, collection[0], revision):
            entries.append({"collection": collection[0], "key": row["key"], "revision": row["rev"],
                            "document": json.loads(row["body"]), "expires_at": row["expires"]})
    raw = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return {"revision": revision, "documents": len(entries),
            "sha256": hashlib.sha256(raw.encode()).hexdigest()}


# --------------------------------------------------------------------------
# reference supervised workload
# --------------------------------------------------------------------------

def _atomic_write(path: Path, payload: dict) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True))
    os.replace(temporary, path)


def _append_event(state_dir: Path, event: dict) -> None:
    event = {"at": time.time(), "pid": os.getpid(), **event}
    with open(state_dir / "service.log", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _seed_from_environment():
    """Import the real library lazily so the operator tool works without it."""
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from strataleaf import Database  # noqa: WPS433 (deliberate lazy import)
    return Database


SCHEMA = {"fields": {"worker": "string", "seq": "integer"}, "required": ["worker", "seq"]}


def _prepare(database, request_key: str, worker: str, seq: int) -> dict:
    """Idempotent bootstrap, then one conditional write per request key.

    The schema and index are created once; both steps tolerate a prior crash
    having committed them. The index is what lets the recovery drill prove
    that the projection was rebuilt/retained, not just that rows exist.
    """
    if not database.catalog.list():
        try:
            database.catalog.create("events", SCHEMA)
        except Exception as exc:  # already created by a prior crash is fine
            if "already exists" not in str(exc):
                raise
    existing = {item["name"] for item in database.indexes.list("events")}
    if "by_seq" not in existing:
        try:
            database.indexes.create("events", "by_seq", "seq")
        except Exception as exc:
            if "already exists" not in str(exc):
                raise
    result = database.batch(request_key).put(
        "events", f"w{worker}", {"worker": worker, "seq": seq}, expected=None).commit()
    return {"request_key": request_key, "revision": result.revision,
            "count": result.count, "replayed": result.replayed}


def supervise(config: dict, *, run_seconds: float | None = None,
              batch_interval: float = 0.05) -> int:
    """Run the reference service workload with the documented exit ordering.

    1. preflight
    2. open the database (creates -wal/-shm)
    3. publish pid/revision state for the operator
    4. append commits on an interval, each with an idempotency key
    5. on SIGTERM/SIGINT: stop accepting, finish the in-flight batch, run a
       checkpoint (bounded), release nothing implicitly, close, and write a
       clean-stop marker *after* close returns.
    6. on timeout: exit EXIT_DRAIN so the supervisor can SIGKILL.

    Deliberately does NOT hold a pin or consumer across the run beyond what
    the caller asked for; this workload exists to prove durability, not to
    exercise retention.
    """
    # SQLite creates -wal/-shm and any new file with mode 0644 before umask.
    # Pin a restrictive umask regardless of how the process was launched, so
    # the database is never group/world-writable by default.
    previous_umask = os.umask(0o027)
    try:
        return _supervise(config, run_seconds=run_seconds, batch_interval=batch_interval)
    finally:
        os.umask(previous_umask)


def _supervise(config: dict, *, run_seconds: float | None, batch_interval: float) -> int:
    paths = config_paths(config)
    data, state = paths["data_dir"], paths["state_dir"]
    state.mkdir(parents=True, exist_ok=True)
    Database = _seed_from_environment()
    result = preflight(config)
    _atomic_write(state / "preflight.json", result)
    (state / "clean-stop.json").unlink(missing_ok=True)
    (state / "running.json").unlink(missing_ok=True)
    _append_event(state, {"event": "preflight-ok", "fstype": result["directories"][0]["fstype"]})

    starting = {"requested": False, "deadline": None}
    stopping = {"requested": False, "deadline": None}
    signals_seen: list[int] = []

    def request_stop(signum, _frame):
        signals_seen.append(signum)
        if not stopping["requested"]:
            stopping["requested"] = True
            stopping["deadline"] = time.monotonic() + config["STRATALEAF_DRAIN_SECONDS"]

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    database = Database(str(paths["db"]), timeout=config["STRATALEAF_BUSY_TIMEOUT"])
    _append_event(state, {"event": "opened", "db": str(paths["db"]), "head": database.revision})
    worker = f"{socket.gethostname()[-12:]}-{os.getpid()}"
    seq = 0
    in_flight = 0
    commits = 0
    started = time.monotonic()
    exit_code = EXIT_OK
    revision = database.revision
    checkpoint_result = None
    try:
        while not stopping["requested"]:
            if run_seconds is not None and time.monotonic() - started >= run_seconds:
                break
            # A stop requested between batches must not start a new batch.
            if stopping["requested"]:
                break
            in_flight = 1
            committed = _prepare(database, f"{worker}-{seq}", worker, seq)
            in_flight = 0
            commits += 1
            revision = committed["revision"]
            _atomic_write(state / "running.json", {
                "pid": os.getpid(), "worker": worker, "seq": seq,
                "revision": revision, "commits": commits,
                "in_flight": in_flight, "phase": "serving",
            })
            seq += 1
            # Sleep in short slices so a signal is honoured promptly.
            deadline = time.monotonic() + batch_interval
            while time.monotonic() < deadline and not stopping["requested"]:
                time.sleep(min(0.01, batch_interval))
        # Drain budget: the reference workload awaits each commit synchronously,
        # so there is never a batch to wait for. If the budget has already been
        # exceeded (e.g. the signal arrived during a slow commit), do NOT claim
        # a clean stop; exit EXIT_DRAIN so the supervisor escalates to SIGKILL.
        if stopping["requested"] and time.monotonic() > stopping["deadline"]:
            exit_code = EXIT_DRAIN
            _append_event(state, {"event": "drain-timeout"})
        else:
            # Checkpoint on this handle, while it is still the only writer, so
            # the final close actually removes -wal/-shm. Doing it in the same
            # `Database` transaction as the last commit is not needed: commit
            # already fsynced; this only bounds the WAL for the next start.
            checkpoint_result = database.checkpoint()
            checkpoint_result = {"mode": "PASSIVE", "busy": checkpoint_result[0],
                                 "log_frames": checkpoint_result[1],
                                 "checkpointed_frames": checkpoint_result[2],
                                 "complete": checkpoint_result[0] == 0
                                 and checkpoint_result[1] == checkpoint_result[2]}
            _append_event(state, {"event": "checkpoint-before-close", **checkpoint_result})
            revision = database.revision
    finally:
        database.close()
    head_after = _head_on_disk(paths["db"])
    if exit_code == EXIT_OK:
        _atomic_write(state / "clean-stop.json", {
            "pid": os.getpid(), "worker": worker, "commits": commits,
            "revision_before_close": revision, "head_on_disk": head_after,
            "signals": signals_seen, "closed_at": time.time(),
            "checkpoint": checkpoint_result,
        })
        _append_event(state, {"event": "clean-close", "head": head_after})
    (state / "running.json").unlink(missing_ok=True)
    return exit_code


def _head_on_disk(db: Path) -> int | None:
    try:
        connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return int(dict(connection.execute("SELECT key,value FROM meta"))["head"])
        finally:
            connection.close()
    except sqlite3.Error:
        return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _config(args) -> dict:
    here = Path(__file__).resolve().parent
    return load_config(args.config or here / "service.conf")


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="strataleaf_ops", description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="path to service.conf")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="validate volumes/ownership/space before start")
    probe_parser = sub.add_parser("probe", help="orchestrator health check")
    probe_parser.add_argument("--kind", choices=["liveness", "readiness"], default="readiness")
    probe_parser.add_argument("--json", action="store_true")
    verify_parser = sub.add_parser("verify", help="recovery verification")
    verify_parser.add_argument("--quick", action="store_true")
    verify_parser.add_argument("--expect-head", type=int)
    verify_parser.add_argument("--json", action="store_true",
                               help="JSON output (the default; accepted for symmetry)")
    space_parser = sub.add_parser("space", help="report capacity with reserves")
    space_parser.add_argument("--json", action="store_true")

    supervise_parser = sub.add_parser("supervise", help="run the reference workload")
    supervise_parser.add_argument("--seconds", type=float)
    supervise_parser.add_argument("--batch-interval", type=float, default=0.05)

    drill_parser = sub.add_parser("drill", help="run lifecycle and boundary drills")
    drill_parser.add_argument("--workspace", required=True)
    drill_parser.add_argument("--mode", default="all",
                              choices=["normal", "sigkill", "torn", "reader", "conflict",
                                       "space", "netfs", "identity", "http", "lifecycle", "all"])
    drill_parser.add_argument("--run-seconds", type=float, default=1.5)
    drill_parser.add_argument("--json", action="store_true")

    stop_parser = sub.add_parser("stop", help="SIGTERM the supervised process")
    stop_parser.add_argument("--state-dir")
    stop_parser.add_argument("--timeout", type=float)

    args = parser.parse_args(argv)
    try:
        config = _config(args)
        if args.command == "preflight":
            print(json.dumps(preflight(config), sort_keys=True))
            return EXIT_OK
        if args.command == "probe":
            report = probe(config, kind=args.kind)
            if args.json:
                print(json.dumps(report, sort_keys=True, default=str))
            if report["status"] == "degraded":
                # Under the hard WAL bound but checkpoint-blocked: a readiness
                # endpoint should stop sending new work here. Liveness stays 0.
                return EXIT_PRECHECK if args.kind == "readiness" else EXIT_OK
            return EXIT_OK
        if args.command == "verify":
            report = verify(config, deep=not args.quick, expect_head=args.expect_head)
            print(json.dumps(report, sort_keys=True, default=str))
            return EXIT_OK
        if args.command == "space":
            paths = config_paths(config)
            report = {"data": free_space(paths["data_dir"]), "state": free_space(paths["state_dir"])}
            problems = (space_floor_violations(config, report["data"])
                        + space_floor_violations(config, report["state"]))
            report.update({"ok": not problems, "problems": problems})
            print(json.dumps(report, sort_keys=True, default=str))
            return EXIT_OK if not problems else EXIT_PRECHECK
        if args.command == "supervise":
            return supervise(config, run_seconds=args.seconds,
                             batch_interval=args.batch_interval)
        if args.command == "drill":
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "smoke"))
            import drills  # noqa: WPS433 (diagnostic harness, shipped alongside)
            report = drills.run_drills(config, Path(args.workspace), args.mode, args.run_seconds)
            if args.json:
                print(json.dumps(report, sort_keys=True, default=str))
            return EXIT_OK if report["ok"] else EXIT_PRECHECK
        if args.command == "stop":
            return _stop(config, args.state_dir, args.timeout)
    except GateError as exc:
        print(f"strataleaf_ops: {exc}", file=sys.stderr)
        return EXIT_PRECHECK
    except sqlite3.Error as exc:
        print(f"strataleaf_ops: sqlite error: {exc}", file=sys.stderr)
        return EXIT_PRECHECK
    return EXIT_USAGE


def _stop(config: dict, state_dir: str | None, timeout: float | None) -> int:
    """Send SIGTERM to the supervised process and wait for the clean marker."""
    paths = config_paths(config)
    state = Path(state_dir) if state_dir else paths["state_dir"]
    running = read_state(state / "running.json")
    if not running or "pid" not in running:
        print("strataleaf_ops: no running.json; nothing to stop", file=sys.stderr)
        return EXIT_USAGE
    timeout = config["STRATALEAF_STOP_TIMEOUT_SECONDS"] if timeout is None else timeout
    pid = int(running["pid"])
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (state / "clean-stop.json").exists() or not Path(f"/proc/{pid}").exists():
            return EXIT_OK
        time.sleep(0.05)
    return EXIT_DRAIN


if __name__ == "__main__":
    sys.exit(main())
