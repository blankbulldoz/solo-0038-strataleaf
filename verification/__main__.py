"""Command line entry point for the verification harness.

    python3 -m verification run [--scenarios N] [--ops N] [--seed S]
                                [--out FILE] [--no-shrink] [--repro FILE]
    python3 -m verification replay FILE [--no-shrink]
    python3 -m verification shrink FILE [--out FILE]

`run` executes `--scenarios` generated scenarios (seeds S, S+1, ...) against a
fresh SQLite file each, comparing the real database with the independent model
operation by operation. On the first divergence it writes a replayable repro
file, shrinks the sequence to a 1-minimal failing segment and exits 1. When
all scenarios pass it prints a coverage summary and exits 0.

`replay` re-executes a repro file and reports the divergence (exit 1) or that
the sequence now passes (exit 0). `shrink` minimizes a repro file further.
"""

import argparse
import json
import sys
import time

from .gen import generate
from .runner import Stats, run_scenario
from .shrink import render, shrink_failure

COVERAGE_REQUIRED = [
    "cross_collection_commits", "tombstone_revivals", "stale_expected_conflicts",
    "unique_conflicts", "commits_replayed", "request_key_conflicts",
    "pages_via_index", "pages_via_scan", "continuations", "cursor_rejections",
    "foreign_cursor_rejections", "snapshot_too_old", "compactions_blocked",
    "compactions_advanced", "sweeps_expired",
    "schema_rejections", "reopens", "patches", "patch_conflicts",
    "feed_reads", "inspects_ok", "get_hits", "get_misses", "evolves",
]


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")


def _report_divergence(divergence, ops, args, seed=None):
    print("DIVERGENCE found" + (" (seed %s)" % seed if seed is not None else ""))
    print("failing operation #%d of %d:" % (divergence.index, len(ops)))
    print(render([divergence.op], divergence))
    repro_path = getattr(args, "repro", None) or "verification-repro.json"
    _write_json(repro_path, {"seed": seed, "ops": ops})
    print("full sequence written to " + repro_path)
    if getattr(args, "no_shrink", False):
        return 1
    minimal, divergence2, attempts = shrink_failure(ops, divergence)
    print("shrunk to %d operations (%d replays):" % (len(minimal), attempts))
    print(render(minimal, divergence2))
    minimal_path = repro_path.replace(".json", "") + "-minimal.json"
    _write_json(minimal_path, {"seed": seed, "ops": minimal})
    print("minimal sequence written to " + minimal_path)
    return 1


def command_run(args):
    stats = Stats()
    started = time.time()
    for offset in range(args.scenarios):
        seed = args.seed + offset
        ops = generate(seed, args.ops)
        divergence = run_scenario(ops, stats)
        if divergence is not None:
            return _report_divergence(divergence, ops, args, seed)
        if (offset + 1) % 10 == 0 or offset + 1 == args.scenarios:
            print("... %d/%d scenarios, %d ops, %.1fs"
                  % (offset + 1, args.scenarios, stats.counts["ops"], time.time() - started),
                  flush=True)
    counts = stats.as_dict()
    gaps = [name for name in COVERAGE_REQUIRED if counts[name] == 0]
    summary = {"scenarios": args.scenarios, "ops_per_scenario": args.ops,
               "seed_start": args.seed, "seconds": round(time.time() - started, 2),
               "coverage": counts, "coverage_gaps": gaps, "divergences": 0}
    print(json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True))
    if args.out:
        _write_json(args.out, summary)
        print("summary written to " + args.out)
    if gaps:
        print("COVERAGE GAPS: " + ", ".join(gaps))
        return 2
    print("ALL SCENARIOS PASSED")
    return 0


def command_replay(args):
    with open(args.file, encoding="utf-8") as handle:
        payload = json.load(handle)
    ops = payload["ops"]
    divergence = run_scenario(ops)
    if divergence is None:
        print("no divergence: sequence passes")
        return 0
    return _report_divergence(divergence, ops, args, payload.get("seed"))


def command_shrink(args):
    with open(args.file, encoding="utf-8") as handle:
        payload = json.load(handle)
    ops = payload["ops"]
    divergence = run_scenario(ops)
    if divergence is None:
        print("no divergence: sequence passes, nothing to shrink")
        return 0
    minimal, divergence2, attempts = shrink_failure(ops, divergence)
    print("shrunk %d -> %d operations (%d replays):" % (len(ops), len(minimal), attempts))
    print(render(minimal, divergence2))
    if args.out:
        _write_json(args.out, {"seed": payload.get("seed"), "ops": minimal})
        print("minimal sequence written to " + args.out)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="verification")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--scenarios", type=int, default=60)
    run.add_argument("--ops", type=int, default=220)
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--out")
    run.add_argument("--repro")
    run.add_argument("--no-shrink", action="store_true")
    run.set_defaults(func=command_run)
    replay = sub.add_parser("replay")
    replay.add_argument("file")
    replay.add_argument("--repro")
    replay.add_argument("--no-shrink", action="store_true")
    replay.set_defaults(func=command_replay)
    shrink = sub.add_parser("shrink")
    shrink.add_argument("file")
    shrink.add_argument("--out")
    shrink.set_defaults(func=command_shrink)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
