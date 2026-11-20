"""Independent verification: re-derive expectations from the input timeline and
check the delivered materials and the isolated target library against them.

Reads (data dir):
  timeline.json       - the input timeline (ground truth, written at build time)
  requests.json       - receiver requests
  recovery/*.json(l)  - delivered recovery materials (artifacts under test)
  isolated/target.db  - the isolated imported library
  source/source.db    - must be unchanged vs recovery/source_state_before.json

The expectation model below is computed from timeline.json alone; it does not
reuse any intermediate state from the recovery script.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")

from strataleaf import Database

CHECKS = []


def check(name, ok, detail=None):
    CHECKS.append({"check": name, "ok": bool(ok), "detail": detail})


def model(timeline, requests):
    """Expected outcomes derived only from the input timeline."""
    events = timeline["events"]
    floor = timeline["floor"]
    by_rev = {event["rev"]: event for event in events}

    def puts_before(key, rev):
        return [e for e in events if e["collection"] == "docs" and e["key"] == key
                and e["op"] == "put" and e["rev"] < rev]

    def deletes_before(key, rev):
        return [e for e in events if e["collection"] == "docs" and e["key"] == key
                and e["op"] == "delete" and e["rev"] < rev]

    expected_events, expected_refs = {}, {}
    for request in requests:
        key, delete_rev = request["key"], request["delete_revision"]
        rid = request["id"]
        if delete_rev <= floor:
            expected_events[rid] = {"status": "unretrievable"}
            continue
        event = by_rev.get(delete_rev)
        if event is None or not (event["collection"] == "docs" and event["key"] == key
                                 and event["op"] == "delete"):
            expected_events[rid] = {"status": "not_found"}
            continue
        prior = puts_before(key, delete_rev)
        if not prior:
            expected_events[rid] = {"status": "no_prior_content"}
            continue
        last = prior[-1]
        expected_events[rid] = {"status": "recovered", "key": key, "delete_rev": delete_rev,
                                "content_rev": last["rev"], "body": last["body"],
                                "lifecycle_ordinal": len(deletes_before(key, delete_rev)) + 1,
                                "target_key": f"{key}#life{len(deletes_before(key, delete_rev)) + 1}"}

    recovered_by_key = {}
    for item in expected_events.values():
        if item.get("status") == "recovered":
            recovered_by_key[(item["key"], item["delete_rev"])] = item

    for event in events:
        if event["collection"] != "refs" or event["op"] != "put":
            continue
        write_rev, target = event["rev"], event["body"]["target"]
        if write_rev < floor:
            expected_refs[event["key"]] = {"resolution": "ambiguous", "imported": False}
            continue
        history = [e for e in events if e["collection"] == "docs" and e["key"] == target
                   and e["rev"] <= write_rev]
        if not history or history[-1]["op"] == "delete":
            expected_refs[event["key"]] = {"resolution": "no_live_target", "imported": False}
            continue
        ordinal = len([e for e in history if e["op"] == "delete"]) + 1
        closing = next((e for e in events if e["collection"] == "docs" and e["key"] == target
                        and e["op"] == "delete" and e["rev"] > write_rev), None)
        if closing is None:
            expected_refs[event["key"]] = {"resolution": "resolved_live_at_head", "imported": False}
        elif (target, closing["rev"]) in recovered_by_key:
            expected_refs[event["key"]] = {"resolution": "resolved_recovered", "imported": True,
                                           "target_key": f"{target}#life{ordinal}"}
        else:
            expected_refs[event["key"]] = {"resolution": "resolved_unrecovered", "imported": False}
    return expected_events, expected_refs


def main(data_dir):
    data = Path(data_dir)
    timeline = json.loads((data / "timeline.json").read_text())
    requests = json.loads((data / "requests.json").read_text())["requests"]
    mapping = json.loads((data / "recovery" / "mapping.json").read_text())
    unretrieved = json.loads((data / "recovery" / "unretrieved.json").read_text())
    before = json.loads((data / "recovery" / "source_state_before.json").read_text())
    after = json.loads((data / "recovery" / "source_state_after.json").read_text())
    docs_lines = [json.loads(line) for line in
                  (data / "recovery" / "importable_docs.jsonl").read_text().splitlines() if line]
    refs_lines = [json.loads(line) for line in
                  (data / "recovery" / "importable_refs.jsonl").read_text().splitlines() if line]

    expected_events, expected_refs = model(timeline, requests)
    expected_recovered = {rid: item for rid, item in expected_events.items()
                          if item["status"] == "recovered"}

    # --- source library must be unchanged ---
    check("source_state_stable_during_recovery", before == after)
    with Database(data / "source" / "source.db") as source:
        now = {"inspect": source.diagnostics.inspect(deep=True),
               "fingerprint": source.diagnostics.fingerprint()}
    check("source_library_unchanged", now == before,
          {"head": now["inspect"]["head"], "floor": now["inspect"]["floor"]})

    # --- delivered mapping matches the timeline-derived model ---
    delivered = {item["request_id"]: item for item in mapping["lifecycles"]}
    check("recovered_request_set",
          set(delivered) == set(expected_recovered),
          {"delivered": sorted(delivered), "expected": sorted(expected_recovered)})
    for rid, expect in expected_recovered.items():
        item = delivered.get(rid)
        if item is None:
            check(f"mapping_present:{rid}", False)
            continue
        source = item["source"]
        check(f"identity:{rid}",
              source["key"] == expect["key"]
              and source["delete_rev"] == expect["delete_rev"]
              and source["content_rev"] == expect["content_rev"]
              and source["lifecycle_ordinal"] == expect["lifecycle_ordinal"],
              {"delivered": {k: source.get(k) for k in ("key", "delete_rev", "content_rev", "lifecycle_ordinal")},
               "expected": {k: expect[k] for k in ("key", "delete_rev", "content_rev", "lifecycle_ordinal")}})
        check(f"target_key:{rid}", item["target"]["key"] == expect["target_key"],
              item["target"]["key"])
        check(f"body:{rid}", item["document"] == expect["body"], item["document"])

    # --- unretrieved deletion events match the model ---
    delivered_unretrieved = {item["request_id"]: item for item in unretrieved["deletion_events"]}
    expected_unretrieved = {rid: item for rid, item in expected_events.items()
                            if item["status"] != "recovered"}
    check("unretrieved_request_set", set(delivered_unretrieved) == set(expected_unretrieved),
          {"delivered": sorted(delivered_unretrieved), "expected": sorted(expected_unretrieved)})
    for rid, expect in expected_unretrieved.items():
        item = delivered_unretrieved.get(rid)
        check(f"unretrieved_status:{rid}", item is not None and item["status"] == expect["status"],
              None if item is None else item["status"])
    check("evidence_boundary_floor",
          unretrieved["evidence_boundary"]["floor"] == timeline["floor"]
          and unretrieved["evidence_boundary"]["head"] == timeline["head"],
          unretrieved["evidence_boundary"])

    # --- reference correspondence matches the model ---
    delivered_refs = {ref["ref_key"]: ref for ref in mapping["references"]}
    check("reference_set", set(delivered_refs) == set(expected_refs),
          {"delivered": sorted(delivered_refs), "expected": sorted(expected_refs)})
    for ref_key, expect in expected_refs.items():
        ref = delivered_refs.get(ref_key)
        if ref is None:
            check(f"reference_present:{ref_key}", False)
            continue
        ok = ref["resolution"] == expect["resolution"] and bool(ref.get("imported")) == expect["imported"]
        if expect["imported"]:
            ok = ok and ref.get("target_key") == expect["target_key"]
        check(f"reference_resolution:{ref_key}", ok,
              {"delivered": {k: ref.get(k) for k in ("resolution", "imported", "target_key")},
               "expected": expect})

    # --- importable content matches the model ---
    check("docs_line_count", len(docs_lines) == len(expected_recovered), len(docs_lines))
    check("refs_line_count",
          len(refs_lines) == len([r for r in expected_refs.values() if r["imported"]]),
          len(refs_lines))
    docs_by_key = {line["key"]: line["document"] for line in docs_lines}
    for expect in expected_recovered.values():
        check(f"importable_body:{expect['target_key']}",
              docs_by_key.get(expect["target_key"]) == expect["body"],
              docs_by_key.get(expect["target_key"]))

    # occurrence preservation: identical bodies across lifecycles must both survive
    twin_body = next(expect["body"] for expect in expected_recovered.values()
                     if [e["body"] for e in expected_recovered.values()].count(expect["body"]) > 1)
    twins = [key for key, document in docs_by_key.items() if document == twin_body]
    check("identical_bodies_keep_occurrences", len(twins) == 2 and len(set(twins)) == 2, twins)

    # content changed back to the original value: life 3 body == first-ever A body
    original = next(e["body"] for e in timeline["events"]
                    if e["collection"] == "docs" and e["key"] == "A" and e["op"] == "put")
    check("content_back_to_original_value", docs_by_key.get("A#life3") == original,
          docs_by_key.get("A#life3"))

    # --- isolated target library: content, references, counts ---
    with Database(data / "isolated" / "target.db") as target:
        docs_keys = [row.key for row in target.query("docs", limit=1000).records]
        refs_keys = [row.key for row in target.query("refs", limit=1000).records]
        check("target_docs_key_set", set(docs_keys) == set(docs_by_key), docs_keys)
        check("target_refs_key_set",
              set(refs_keys) == {key for key, expect in expected_refs.items() if expect["imported"]},
              refs_keys)
        for key, document in docs_by_key.items():
            record = target.get("docs", key)
            check(f"target_body:{key}", record is not None and record.document == document)
        for ref_key, expect in expected_refs.items():
            record = target.get("refs", ref_key)
            if expect["imported"]:
                check(f"target_ref_wired:{ref_key}",
                      record is not None and record.document.get("target") == expect["target_key"],
                      None if record is None else record.document)
            else:
                check(f"target_ref_absent:{ref_key}", record is None)
        # no reference may point at the current same-named document of the source
        check("no_wiring_to_current_document",
              all(target.get("refs", key) is None or
                  target.get("refs", key).document.get("target") != "A" for key in refs_keys))
        check("target_head", target.revision == 2, target.revision)

    ok = all(item["ok"] for item in CHECKS)
    report = {"ok": ok, "checks": CHECKS,
              "totals": {"checks": len(CHECKS), "failed": sum(1 for item in CHECKS if not item["ok"])}}
    (data / "verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/strata_recovery/data")
