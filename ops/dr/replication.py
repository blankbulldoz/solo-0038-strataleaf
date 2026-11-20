"""Incremental catch-up of a restored replica and source/replica verification.

The replica accepts only contiguous packets from the same upstream.  This
module drives ``Database.replication.export`` on the source and
``Database.replication.apply`` on the replica, records every packet, and
classifies the two operational failure modes that a drill must exercise:

* an interrupted receive (packets stop arriving) - the replica stays at its
  last applied revision and can resume;
* the source history was reclaimed past the replica's revision - catch-up is
  impossible and the replica must be re-bootstrapped from a fresh backup.

A packet that fails checksum or creates a revision gap is rejected by the
library; the drill verifies the replica is unchanged afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

from strataleaf import Conflict, Database, ReplicationError, SnapshotTooOld

from .facts import checks_ok, compare_snapshot, read_facts


class CatchUpError(Exception):
    pass


def _source_facts_at(source: str | Path, revision: int) -> dict:
    return read_facts(source, revision=revision)


def _replica_binding(replica: str | Path) -> dict:
    facts = read_facts(replica)
    identity = facts["identity"]
    if identity["mode"] != "replica":
        raise CatchUpError("target is not a replica (mode=%s)" % identity["mode"])
    if not identity["upstream"]:
        raise CatchUpError("replica has no upstream binding")
    return facts


def catch_up(source: str | Path, replica: str | Path, *, max_packets: int = 200,
             packet_limit: int = 100, stop_after_packets: int | None = None,
             corrupt_probe: bool = False) -> dict:
    """Apply contiguous packets from ``source`` to ``replica``.

    ``stop_after_packets`` simulates an interrupted receive: the loop returns
    after applying that many packets, leaving a resumable replica.
    ``corrupt_probe`` additionally attempts to apply a tampered packet and
    records that it was rejected without side effects.
    """
    source, replica = Path(source).resolve(), Path(replica).resolve()
    replica_facts = _replica_binding(replica)
    source_facts = read_facts(source)
    if replica_facts["identity"]["upstream"] != source_facts["identity"]["database_id"]:
        raise CatchUpError("replica upstream does not match source database_id")

    target_head = source_facts["head"]
    start_revision = replica_facts["head"]
    events = []
    corrupt_result = None

    with Database(source) as upstream, Database(replica) as follower:
        applied_packets = 0
        for _ in range(max_packets):
            current = follower.revision
            if current >= target_head:
                break
            try:
                envelope = upstream.replication.export(current, limit=packet_limit)
            except SnapshotTooOld as exc:
                events.append({"step": "export", "since": current, "result": "reclaimed", "detail": str(exc)})
                return _result("requires_rebootstrap", source, replica, start_revision, current,
                               target_head, events, corrupt_result,
                               reason="source history reclaimed before replica revision: " + str(exc))
            except ReplicationError as exc:
                events.append({"step": "export", "since": current, "result": "rejected", "detail": str(exc)})
                return _result("failed", source, replica, start_revision, current, target_head,
                               events, corrupt_result, reason=str(exc))

            if corrupt_probe and corrupt_result is None:
                corrupt_result = _corrupt_probe(follower, envelope)
                events.append({"step": "corrupt_probe", "since": current, **corrupt_result})

            before = follower.revision
            try:
                receipt = follower.replication.apply(envelope)
            except (ReplicationError, Conflict) as exc:
                events.append({"step": "apply", "since": current, "result": "rejected",
                               "detail": "%s: %s" % (type(exc).__name__, exc)})
                if follower.revision != before:
                    raise CatchUpError("replica advanced despite a rejected packet")
                return _result("failed", source, replica, start_revision, before, target_head,
                               events, corrupt_result, reason=str(exc))
            events.append({"step": "apply", "since": current, "through": envelope["body"]["through"],
                           "source_head": envelope["body"]["head"], "applied": receipt["applied"],
                           "replica_revision": receipt["revision"]})
            if receipt["applied"] == 0 and receipt["revision"] <= before:
                # No forward progress (e.g. already current, or epoch-only packet
                # that changed nothing).  Avoid an infinite loop.
                if envelope["body"]["through"] <= before:
                    events.append({"step": "stall", "since": current, "revision": before})
                    break
            applied_packets += 1
            if stop_after_packets is not None and applied_packets >= stop_after_packets:
                events.append({"step": "interrupted", "after_packets": applied_packets,
                               "replica_revision": follower.revision, "target_head": target_head})
                return _result("interrupted", source, replica, start_revision, follower.revision,
                               target_head, events, corrupt_result,
                               reason="receive stopped after %d packet(s)" % applied_packets)

        final_revision = follower.revision

    replica_after = read_facts(replica)
    source_at_final = _source_facts_at(source, final_revision) if final_revision <= read_facts(source)["head"] else None
    if source_at_final is None:
        return _result("failed", source, replica, start_revision, final_revision, target_head,
                       events, corrupt_result, reason="replica revision is ahead of the source")
    checks = compare_snapshot(
        {
            "head": final_revision,
            "floor": source_at_final["floor"],
            "catalog_epoch": source_at_final["catalog_epoch"],
            "schema_sha256": source_at_final["schema_sha256"],
            "index_definition_sha256": source_at_final["index_definition_sha256"],
            "documents_sha256": source_at_final["documents"]["sha256"],
            "documents_count": source_at_final["documents"]["count"],
        },
        replica_after, identity="follower",
        source_database_id=source_at_final["identity"]["database_id"],
        expected_sha256=None,
    )
    # The replica's floor is its own watermark and does not follow the source;
    # the backup head belongs to the bootstrap snapshot, not the live target.
    checks = [item for item in checks
              if item["check"] not in ("head_matches_backup", "floor_matches_backup", "archive_checksum")]
    status = "caught_up" if replica_after["head"] == target_head and checks_ok(checks) else "partial"
    return _result(status, source, replica, start_revision, replica_after["head"], target_head,
                   events, corrupt_result, checks=checks,
                   reason=None if status == "caught_up" else "replica did not reach target head or facts differ")


def _corrupt_probe(follower: Database, envelope: dict) -> dict:
    """Attempt a tampered packet; it must be rejected with no side effects."""
    tampered = json.loads(json.dumps(envelope))
    tampered["sha256"] = "0" * 64
    before = follower.revision
    try:
        follower.replication.apply(tampered)
    except ReplicationError as exc:
        if follower.revision != before:
            raise CatchUpError("replica advanced after a tampered packet")
        return {"result": "rejected", "revision_unchanged": True, "error": str(exc)}
    raise CatchUpError("tampered replication packet was accepted")


def _result(status, source, replica, start_revision, final_revision, target_head,
            events, corrupt_result, reason=None, checks=None) -> dict:
    return {
        "status": status,
        "source": str(source),
        "replica": str(replica),
        "start_revision": start_revision,
        "replica_revision": final_revision,
        "target_head": target_head,
        "lag": max(0, target_head - final_revision),
        "events": events,
        "corrupt_probe": corrupt_result,
        "checks": checks,
        "reason": reason,
    }


def protect_source(source: str | Path, pin_name: str, revision: int | None = None) -> dict:
    """Pin the source at a revision so catch-up history survives compaction."""
    with Database(source) as db:
        revision = db.retention.pin(pin_name, revision)
        return {"pin": pin_name, "revision": revision}


def release_source_pin(source: str | Path, pin_name: str) -> dict:
    with Database(source) as db:
        db.retention.unpin(pin_name)
        return {"pin": pin_name, "released": True}
