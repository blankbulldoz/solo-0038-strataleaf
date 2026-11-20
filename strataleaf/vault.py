"""Verified backup registry with leases, reopen handling and retention pruning.

The vault owns a single directory. Only facts recorded in its JSON registry are
treated as managed backups; files left behind by interrupted runs are reopened
by ``reconcile`` instead of being guessed at from filename timestamps. A backup
can be deleted only when enough other *published* (verified, same-source)
backups remain and no restore lease still references it.
"""
import contextlib
import datetime
import fcntl
import json
import threading
import uuid
from pathlib import Path

from .archive import Archives, inspect_archive
from .errors import Conflict, NotFound, VaultError

REGISTRY_NAME = "vault-registry.json"
LOCK_NAME = ".vault.lock"
STAGED_PREFIX = ".staged-"
QUARANTINE_PREFIX = "quarantine-"


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class BackupVault:
    """Registry of verified, single-source backup files on the local filesystem."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / REGISTRY_NAME
        self._lock_path = self.root / LOCK_NAME
        self._thread_lock = threading.RLock()
        self._registry = {"format": 1, "source_id": None, "entries": []}

    # ------------------------------------------------------------------ locks
    @contextlib.contextmanager
    def _locked(self):
        # RLock-style re-entry for the same handle plus an OS lock for other
        # processes that share the vault directory.
        with self._thread_lock:
            with open(self._lock_path, "a+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load(self):
        if self.registry_path.exists():
            try:
                data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise VaultError("backup registry is unreadable") from exc
            if not isinstance(data, dict) or data.get("format") != 1 or not isinstance(data.get("entries"), list):
                raise VaultError("backup registry uses an unsupported format")
            self._registry = data
        return self._registry

    def _store(self):
        temporary = self.root / (".registry-" + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(self._registry, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temporary.replace(self.registry_path)

    def _entries(self):
        return self._registry["entries"]

    def _find(self, entry_id):
        for entry in self._entries():
            if entry["id"] == entry_id:
                return entry
        raise NotFound("unknown backup entry: " + entry_id)

    def _source_id(self):
        return self._registry.get("source_id")

    def _bind_source(self, source_id):
        current = self._source_id()
        if current is None:
            self._registry["source_id"] = source_id
        elif current != source_id:
            raise Conflict("vault is already bound to another database source")

    # -------------------------------------------------------------- lifecycle
    def prepare(self, database):
        """Start a backup: snapshot into a staged file and open a registry entry.

        The staged file is intentionally not verified yet. A crash between
        ``prepare`` and ``publish`` leaves both the file and a ``pending``
        entry, which ``reconcile`` can reopen later.
        """
        with self._locked():
            self._load()
            source_id = database._store.get("database_id")
            self._bind_source(source_id)
            entry_id = uuid.uuid4().hex
            staged_name = STAGED_PREFIX + entry_id + ".db"
            receipt = database.archives.backup(self.root / staged_name)
            entry = {
                "id": entry_id,
                "state": "pending",
                "source_id": source_id,
                "revision": receipt["revision"],
                "file": staged_name,
                "sha256": None,
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "lease_holder": None,
                "history": [{"at": _utc_now(), "event": "prepared", "detail": staged_name}],
            }
            self._entries().append(entry)
            self._store()
            return self._view(entry)

    def publish(self, entry_id):
        """Verify a pending backup and atomically promote it to published."""
        with self._locked():
            self._load()
            entry = self._find(entry_id)
            if entry["state"] != "pending":
                raise Conflict("only pending backups can be published")
            staged = self._managed_path(entry["file"])
            try:
                receipt = inspect_archive(staged)
            except Exception as exc:  # corrupted or truncated snapshots
                self._quarantine(entry, "verification failed: " + type(exc).__name__)
                self._store()
                raise VaultError("backup failed verification and was quarantined") from exc
            if receipt["database_id"] != entry["source_id"]:
                self._quarantine(entry, "database source mismatch")
                self._store()
                raise Conflict("backup does not belong to the registered source")
            final_name = self._next_name(receipt)
            final_path = self.root / final_name
            if final_path.exists():
                raise Conflict("published backup name appeared concurrently")
            staged.rename(final_path)
            entry.update(state="published", file=final_name, sha256=receipt["sha256"], updated_at=_utc_now())
            entry["history"].append({"at": _utc_now(), "event": "published", "detail": final_name})
            self._store()
            return self._view(entry)

    def capture(self, database):
        """Convenience wrapper: prepare then publish, returning the full receipt."""
        prepared = self.prepare(database)
        return self.publish(prepared["id"])

    def _next_name(self, receipt):
        while True:
            token = uuid.uuid4().hex[:8]
            name = f"{receipt['database_id']}-r{receipt['revision']}-{token}.db"
            if not (self.root / name).exists() and not any(e["file"] == name for e in self._entries()):
                return name

    def _quarantine(self, entry, reason):
        original = entry["file"]
        target = QUARANTINE_PREFIX + entry["id"] + ".db"
        source = self._managed_path(original)
        if source.exists():
            counter = 1
            candidate = target
            while (self.root / candidate).exists():
                candidate = f"{QUARANTINE_PREFIX}{entry['id']}-{counter}.db"
                counter += 1
            target = candidate
            source.rename(self.root / target)
        entry.update(state="quarantined", file=target, updated_at=_utc_now())
        entry["history"].append({"at": _utc_now(), "event": "quarantined", "detail": reason})

    def _managed_path(self, name):
        path = (self.root / name).resolve()
        if self.root not in path.parents and path != self.root:
            raise VaultError("registry entry points outside the vault")
        return path

    # --------------------------------------------------------------- reopening
    def reconcile(self):
        """Reopen interrupted work and re-verify every registered backup.

        Outcomes per entry/file:
        * pending entry whose staged file verifies -> stays pending (caller
          decides to publish or discard); a corrupt staged file is quarantined;
        * published entry whose checksum now fails -> quarantined (unless a
          restore lease is active, in which case the file is preserved and the
          failure is reported);
        * staged file with no registry entry (process died before the registry
          write) -> adopted as pending when intact, otherwise quarantined;
        * other unknown .db files are left untouched and reported as foreign.
        """
        with self._locked():
            self._load()
            report = {"reverified": [], "reopened": [], "quarantined": [], "leased_corrupt": [], "foreign_files": []}
            known = {entry["file"] for entry in self._entries()}
            for entry in list(self._entries()):
                path = self._managed_path(entry["file"])
                if not path.exists():
                    if entry["state"] == "pending":
                        reason = "staged file vanished before verification"
                        entry["history"].append({"at": _utc_now(), "event": "abandoned", "detail": reason})
                        view = self._view(entry)
                        self._entries().remove(entry)
                        report["quarantined"].append({**view, "reason": reason, "file": None})
                    elif entry.get("lease_holder"):
                        report["leased_corrupt"].append({"id": entry["id"], "reason": "file missing while leased"})
                    else:
                        reason = "published file missing"
                        entry.update(state="quarantined", updated_at=_utc_now())
                        entry["history"].append({"at": _utc_now(), "event": "quarantined", "detail": reason})
                        report["quarantined"].append({**self._view(entry), "reason": reason})
                    continue
                try:
                    receipt = inspect_archive(path)
                except Exception as exc:
                    if entry.get("lease_holder"):
                        report["leased_corrupt"].append({"id": entry["id"], "reason": type(exc).__name__})
                        continue
                    reason = "verification failed: " + type(exc).__name__
                    self._quarantine(entry, reason)
                    report["quarantined"].append(self._view(entry))
                    continue
                if entry["state"] == "pending":
                    entry["history"].append({"at": _utc_now(), "event": "reverified", "detail": "pending snapshot still intact"})
                    report["reopened"].append(self._view(entry))
                    continue
                if entry.get("sha256") and receipt["sha256"] != entry["sha256"]:
                    if entry.get("lease_holder"):
                        report["leased_corrupt"].append({"id": entry["id"], "reason": "checksum mismatch while leased"})
                        continue
                    self._quarantine(entry, "checksum mismatch")
                    report["quarantined"].append(self._view(entry))
                    continue
                if receipt["database_id"] != entry["source_id"]:
                    if entry.get("lease_holder"):
                        report["leased_corrupt"].append({"id": entry["id"], "reason": "source mismatch while leased"})
                        continue
                    self._quarantine(entry, "database source mismatch")
                    report["quarantined"].append(self._view(entry))
                    continue
                entry["history"].append({"at": _utc_now(), "event": "reverified", "detail": receipt["sha256"][:12]})
                report["reverified"].append(self._view(entry))
            for path in sorted(self.root.iterdir()):
                if not path.is_file() or path.name in (REGISTRY_NAME, LOCK_NAME) or path.name.startswith(".registry-"):
                    continue
                if path.name in known:
                    continue
                if path.name.endswith("-wal") or path.name.endswith("-shm"):
                    continue
                if path.name.startswith(STAGED_PREFIX):
                    self._adopt_orphan(path, report)
                elif path.suffix == ".db" and not path.name.startswith(QUARANTINE_PREFIX):
                    report["foreign_files"].append(path.name)
            self._store()
            return report

    def _adopt_orphan(self, path, report):
        entry_id = uuid.uuid4().hex
        try:
            receipt = inspect_archive(path)
        except Exception:
            target = QUARANTINE_PREFIX + entry_id + ".db"
            path.rename(self.root / target)
            report["quarantined"].append({"id": entry_id, "state": "quarantined", "file": target,
                                          "revision": None, "reason": "orphan staged file failed verification"})
            return
        if self._source_id() is None:
            self._registry["source_id"] = receipt["database_id"]
        if receipt["database_id"] != self._source_id():
            target = QUARANTINE_PREFIX + entry_id + ".db"
            path.rename(self.root / target)
            report["quarantined"].append({"id": entry_id, "state": "quarantined", "file": target,
                                          "revision": receipt["revision"], "reason": "foreign source orphan"})
            return
        entry = {
            "id": entry_id,
            "state": "pending",
            "source_id": receipt["database_id"],
            "revision": receipt["revision"],
            "file": path.name,
            "sha256": None,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "lease_holder": None,
            "history": [{"at": _utc_now(), "event": "reopened", "detail": "orphan staged file adopted"}],
        }
        self._entries().append(entry)
        report["reopened"].append(self._view(entry))

    # ------------------------------------------------------------------ leases
    @contextlib.contextmanager
    def lease(self, entry_id, holder):
        """Protect a published backup from pruning while a restore is running."""
        with self._locked():
            self._load()
            entry = self._find(entry_id)
            if entry["state"] != "published":
                raise Conflict("only published backups can be leased")
            if entry.get("lease_holder"):
                raise Conflict("backup is already being restored: " + entry["lease_holder"])
            entry["lease_holder"] = holder
            entry["history"].append({"at": _utc_now(), "event": "leased", "detail": holder})
            self._store()
        try:
            yield self._view(entry)
        except BaseException:
            self._release(entry_id, holder, released=False)
            raise
        else:
            self._release(entry_id, holder, released=True)

    def _release(self, entry_id, holder, released):
        with self._locked():
            self._load()
            entry = self._find(entry_id)
            if entry.get("lease_holder") == holder:
                entry["lease_holder"] = None
                entry["history"].append({"at": _utc_now(),
                                         "event": "released" if released else "released_after_failure",
                                         "detail": holder})
                self._store()

    def restore_verified(self, entry_id, destination, holder, follower=False):
        """Restore a registered backup, holding a lease for the whole operation.

        An existing lease held by the same caller name is reused, so callers
        that already opened a lease do not dead-lock their own restore.
        """
        with self._locked():
            self._load()
            entry = self._find(entry_id)
            active = entry.get("lease_holder")
            if active is not None and active != holder:
                raise Conflict("backup is already being restored: " + active)
            source = self._managed_path(entry["file"])
            expected = entry["sha256"]
        if active == holder:
            receipt = Archives.restore(source, destination, expected_sha256=expected, follower=follower)
        else:
            with self.lease(entry_id, holder):
                receipt = Archives.restore(source, destination, expected_sha256=expected, follower=follower)
        return receipt

    # --------------------------------------------------------------- retention
    def prune(self, retain):
        """Delete surplus published backups while preserving a verified minimum.

        Selection order, never based on filename age alone:
        1. quarantined entries are never counted as recoverable;
        2. when several published entries share a revision, all but the newest
           are duplicate candidates (the latest entry wins);
        3. after de-duplication, oldest verified backups are removed until
           ``retain`` published backups remain;
        4. leased backups are excluded from the guaranteed recoverable set.
           If honoring the lease would leave fewer than ``retain`` unleased
           verified backups, the whole prune is rejected instead of silently
           under-delivering the guarantee.
        """
        if isinstance(retain, bool) or not isinstance(retain, int) or retain < 1:
            raise VaultError("retain must be a positive integer")
        with self._locked():
            self._load()
            published = [e for e in self._entries() if e["state"] == "published"]
            leased = [e for e in published if e.get("lease_holder")]
            unleased_published = [e for e in published if not e.get("lease_holder")]
            # The guaranteed recoverable set excludes leased backups (a restore
            # is actively reading them) and quarantined files. Below the target
            # the vault must raise rather than pretend the guarantee still holds.
            if len(unleased_published) < retain:
                if leased:
                    raise Conflict("prune blocked by active restore leases")
                raise Conflict("vault holds fewer verified backups than the required retain count")
            order = sorted(published, key=lambda e: (e["revision"], e["updated_at"], e["id"]))
            latest_by_revision = {}
            for entry in order:
                latest_by_revision[entry["revision"]] = entry
            duplicate_losers = [e for e in order if latest_by_revision[e["revision"]] is not e]
            unique_kept = [e for e in order if latest_by_revision[e["revision"]] is e]
            surplus = max(0, len(unique_kept) - retain)
            age_candidates = unique_kept[:surplus]
            planned = duplicate_losers + age_candidates
            blocked_leases = [self._view(e) for e in planned if e.get("lease_holder")]
            unleased_survivors = len(published) - len(planned) - len(leased)
            if blocked_leases or unleased_survivors < retain:
                raise Conflict("prune blocked by active restore leases")
            deleted = []
            for entry in planned:
                reason = "same-revision duplicate" if entry in duplicate_losers else "retention age prune"
                deleted.append(self._discard_locked(entry, reason))
            survivors = [self._view(e) for e in self._entries() if e["state"] == "published"]
            if deleted and len(survivors) < retain:
                raise VaultError("prune would leave fewer verified backups than required")
            self._store()
            return {
                "retain": retain,
                "deleted": deleted,
                "survivors": survivors,
                "duplicate_revisions_removed": len(duplicate_losers),
                "age_pruned": len(age_candidates),
                "leased_protected": [self._view(e) for e in leased],
                "quarantined_present": [self._view(e) for e in self._entries() if e["state"] == "quarantined"],
            }

    def discard(self, entry_id, reason="manual discard"):
        """Remove a quarantined or still-pending entry that cannot be published."""
        with self._locked():
            self._load()
            entry = self._find(entry_id)
            if entry["state"] == "published":
                raise Conflict("published backups are removed only through prune")
            if entry.get("lease_holder"):
                raise Conflict("leased backups cannot be discarded")
            view = self._discard_locked(entry, reason)
            self._store()
            return view

    def forget_pending(self, entry_id, reason="simulated crash before registry durable write"):
        """Drop a pending registry entry while keeping its staged file.

        Models a process that died between writing the staged snapshot and the
        durable registry update. Only pending entries are accepted, so a
        verified backup can never be silently detached from the registry.
        """
        with self._locked():
            self._load()
            entry = self._find(entry_id)
            if entry["state"] != "pending":
                raise Conflict("only pending entries can be forgotten")
            if entry.get("lease_holder"):
                raise Conflict("leased backups cannot be forgotten")
            view = self._view(entry)
            entry["history"].append({"at": _utc_now(), "event": "registry_forgot", "detail": reason})
            self._entries().remove(entry)
            self._store()
            return view

    def _discard_locked(self, entry, reason):
        path = self._managed_path(entry["file"])
        path.unlink(missing_ok=True)
        entry["history"].append({"at": _utc_now(), "event": "discarded", "detail": reason})
        view = self._view(entry)
        self._entries().remove(entry)
        return view

    # ----------------------------------------------------------------- queries
    def list_backups(self, state=None):
        with self._locked():
            self._load()
            entries = self._entries()
            if state is not None:
                entries = [e for e in entries if e["state"] == state]
            return [self._view(e) for e in entries]

    def verify(self, entry_id):
        with self._locked():
            self._load()
            entry = self._find(entry_id)
            receipt = inspect_archive(self._managed_path(entry["file"]))
            if entry["state"] == "published" and entry["sha256"] != receipt["sha256"]:
                raise VaultError("stored checksum no longer matches the file")
            return {**self._view(entry), "inspection": receipt}

    @staticmethod
    def _view(entry):
        return {
            "id": entry["id"],
            "state": entry["state"],
            "source_id": entry["source_id"],
            "revision": entry["revision"],
            "file": entry["file"],
            "sha256": entry["sha256"],
            "created_at": entry["created_at"],
            "updated_at": entry["updated_at"],
            "lease_holder": entry.get("lease_holder"),
        }
