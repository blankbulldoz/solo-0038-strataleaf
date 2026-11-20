"""Command-line entry points for the StrataLeaf disaster-recovery drill.

Usage::

    python3 -m ops.dr drill --root /var/tmp/strataleaf-drill
    python3 -m ops.dr backup --source /srv/app/service.db --store /srv/backups
    python3 -m ops.dr verify --archive /srv/backups/backup-...db
    python3 -m ops.dr restore --archive /srv/backups/backup-...db --destination /srv/recovery/new.db
    python3 -m ops.dr reject-copy --path /srv/backups/suspect.db
    python3 -m ops.dr catch-up --source /srv/app/service.db --replica /srv/recovery/replica.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import TOOL_VERSION


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ops.dr",
                                     description="StrataLeaf single-host disaster-recovery drill tooling")
    parser.add_argument("--version", action="version", version="%(prog)s " + TOOL_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    drill = sub.add_parser("drill", help="run the full end-to-end drill")
    drill.add_argument("--root", required=True, help="fresh directory that will hold all drill state")
    drill.add_argument("--writer-seconds", type=float, default=2.5)
    drill.add_argument("--interval", type=float, default=0.005)

    backup = sub.add_parser("backup", help="produce and verify an online backup")
    backup.add_argument("--source", required=True)
    backup.add_argument("--store", required=True)
    backup.add_argument("--label", default="backup")
    backup.add_argument("--attempts", type=int, default=3)

    verify = sub.add_parser("verify", help="re-verify an archived backup from its manifest")
    verify.add_argument("--archive", required=True)

    restore = sub.add_parser("restore", help="restore a backup to a new path and verify facts")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--destination", required=True)
    restore.add_argument("--mode", choices=["primary", "follower"], default="primary")

    reject = sub.add_parser("reject-copy", help="show why a bare file is not an accepted backup")
    reject.add_argument("--path", required=True)

    catch = sub.add_parser("catch-up", help="apply contiguous packets from source to replica")
    catch.add_argument("--source", required=True)
    catch.add_argument("--replica", required=True)
    catch.add_argument("--limit", type=int, default=100)

    inspect = sub.add_parser("inspect", help="print the fact set of a database file")
    inspect.add_argument("--path", required=True)
    inspect.add_argument("--revision", type=int, default=None)

    evidence = sub.add_parser("verify-evidence", help="independently re-verify a drill evidence bundle")
    evidence.add_argument("--root", required=True)

    prune = sub.add_parser("prune-temporaries", help="report or remove orphaned .strataleaf-* temp sidecars")
    prune.add_argument("--directory", required=True)
    prune.add_argument("--apply", action="store_true")

    args = parser.parse_args(argv)
    code = 0
    try:
        if args.command == "drill":
            from .drill import Drill
            report = Drill(args.root, writer_seconds=args.writer_seconds, interval=args.interval).run()
            _print({"outcome": report["outcome"], "evidence": report.get("evidence"),
                    "evidence_written": report.get("evidence_written"),
                    "recovery_point": report.get("recovery_point"), "errors": report.get("errors"),
                    "report": (str(Path(args.root) / "evidence" / "drill-report.json")
                               if report.get("evidence_written") else None)})
            code = 0 if report["outcome"] == "success" else 1
        elif args.command == "backup":
            from .backup import produce_backup
            _print(produce_backup(args.source, args.store, args.label, attempts=args.attempts))
        elif args.command == "verify":
            from .backup import verify_manifest
            result = verify_manifest(args.archive)
            _print(result)
            code = 0 if result["ok"] else 1
        elif args.command == "restore":
            from .backup import restore_and_verify
            result = restore_and_verify(args.archive, args.destination, mode=args.mode)
            _print(result)
            code = 0 if result["ok"] else 1
        elif args.command == "reject-copy":
            from .backup import reject_bare_copy
            _print(reject_bare_copy(args.path))
        elif args.command == "catch-up":
            from .replication import catch_up
            result = catch_up(args.source, args.replica, packet_limit=args.limit)
            _print(result)
            code = 0 if result["status"] in ("caught_up", "interrupted") else 1
        elif args.command == "inspect":
            from .facts import read_facts, summary
            _print(summary(read_facts(args.path, revision=args.revision, hash_file=True)))
        elif args.command == "verify-evidence":
            from .evidence_check import check_bundle
            result = check_bundle(args.root)
            _print(result)
            code = 0 if result["ok"] else 1
        elif args.command == "prune-temporaries":
            from .backup import prune_stale_temporaries
            _print(prune_stale_temporaries(args.directory, apply=args.apply))
    except Exception as exc:  # noqa: BLE001 - operational CLI boundary
        _print({"error": type(exc).__name__, "message": str(exc)})
        code = 2
    return code


if __name__ == "__main__":
    sys.exit(main())
