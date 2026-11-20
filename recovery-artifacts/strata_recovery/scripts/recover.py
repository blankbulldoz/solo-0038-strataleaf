"""Derive recovery materials from the source library, read-only, using commit facts.

Inputs (data dir):
  source/source.db  - the source library (never written here)
  requests.json     - receiver requests listed by deletion event
                      (collection/key/delete commit revision; NO content versions)

This script never reads timeline.json: everything is derived from retained commit
facts (feed payloads) and pinned-revision reads through the public API.

Outputs (data dir / "recovery"):
  importable_docs.jsonl   - transfer lines for recovered lifecycles (new target keys)
  importable_refs.jsonl   - transfer lines for references with a unique recovered target
  mapping.json            - source -> target key mapping, lifecycle provenance,
                            reference correspondence and dispositions
  unretrieved.json        - unretrieved deletion events / references + evidence boundary
  source_state_before.json / source_state_after.json - proof the source is unchanged
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")

from strataleaf import Database
from strataleaf.codec import dumps


def read_all_commits(db, floor):
    commits = {}
    since = floor
    while True:
        page = db.feed.read(since, limit=1000)
        for payload in page:
            commits[payload["revision"]] = payload
        if len(page) < 1000:
            return commits
        since = page[-1]["revision"]


def key_operations(commits):
    """collection/key -> ordered [(rev, 'put'|'delete', expected)] from commit facts."""
    ops = {}
    for rev in sorted(commits):
        for operation in commits[rev]["operations"]:
            identity = (operation["collection"], operation["key"])
            kind = "put" if operation["document"] is not None else "delete"
            ops.setdefault(identity, []).append((rev, kind, operation["expected"]))
    return ops


def lifecycles_of(ops):
    """Split one key's retained operations into lifecycles (delete = boundary)."""
    lives, current = [], None
    for rev, kind, _expected in ops:
        if kind == "put":
            if current is None:
                current = {"create_rev": rev, "delete_rev": None, "origin": "commit"}
        else:
            if current is None:
                lives.append({"create_rev": None, "delete_rev": rev, "origin": "recycled"})
            else:
                current["delete_rev"] = rev
                lives.append(current)
            current = None
    if current is not None:
        lives.append(current)
    return lives


def body_sha(document):
    return hashlib.sha256(dumps(document).encode()).hexdigest()


def main(data_dir):
    data = Path(data_dir)
    out = data / "recovery"
    out.mkdir(parents=True, exist_ok=True)
    requests = json.loads((data / "requests.json").read_text())["requests"]

    with Database(data / "source" / "source.db") as db:
        before = {"inspect": db.diagnostics.inspect(deep=True),
                  "fingerprint": db.diagnostics.fingerprint()}
        head, floor = before["inspect"]["head"], before["inspect"]["floor"]
        commits = read_all_commits(db, floor)
        ops_by_key = key_operations(commits)
        lives_by_key = {identity: lifecycles_of(ops) for identity, ops in ops_by_key.items()}

        recovered, unretrieved_events = [], []
        for request in requests:
            collection, key, delete_rev = request["collection"], request["key"], request["delete_revision"]
            identity = (collection, key)
            base = {"request_id": request["id"], "collection": collection, "key": key,
                    "delete_revision": delete_rev}
            if delete_rev > head:
                unretrieved_events.append({**base, "status": "invalid_request",
                                           "reason": f"delete revision {delete_rev} is beyond source head {head}"})
                continue
            if delete_rev <= floor:
                unretrieved_events.append({**base, "status": "unretrievable",
                                           "reason": f"delete commit {delete_rev} <= retained floor {floor}: "
                                                     "commit facts were recycled; last-valid content is undeterminable"})
                continue
            commit = commits.get(delete_rev)
            deletes = [op for op in (commit["operations"] if commit else [])
                       if op["collection"] == collection and op["key"] == key and op["document"] is None]
            if not deletes:
                actual = ([{"collection": op["collection"], "key": op["key"],
                            "kind": "put" if op["document"] is not None else "delete"}
                           for op in commit["operations"]] if commit else [])
                unretrieved_events.append({**base, "status": "not_found",
                                           "reason": f"retained commit {delete_rev} contains no delete of "
                                                     f"{collection}/{key}", "commit_actual_operations": actual})
                continue
            delete_op = deletes[0]
            record = db.get(collection, key, revision=delete_rev - 1)
            if record is None:
                reason = (f"delete commit expected={delete_op['expected']}: key never had a live version"
                          if delete_op["expected"] == 0 else
                          "no live version exists before the delete commit")
                unretrieved_events.append({**base, "status": "no_prior_content", "reason": reason,
                                           "delete_expected": delete_op["expected"]})
                continue
            lives = lives_by_key.get(identity, [])
            position = next((i for i, life in enumerate(lives) if life["delete_rev"] == delete_rev), None)
            life = lives[position] if position is not None else None
            if delete_op["expected"] is not None and delete_op["expected"] != record.revision:
                raise SystemExit(f"commit fact mismatch at rev {delete_rev}: delete expected "
                                 f"{delete_op['expected']} but last valid version is {record.revision}")
            recovered.append({
                "request_id": request["id"],
                "source": {"collection": collection, "key": key,
                           "lifecycle_ordinal": (position + 1) if position is not None else None,
                           "lifecycle_origin": life["origin"] if life else "unknown",
                           "create_rev": life["create_rev"] if life else None,
                           "delete_rev": delete_rev, "content_rev": record.revision,
                           "delete_expected": delete_op["expected"],
                           "body_sha256": body_sha(record.document)},
                "document": record.document,
                "target": {"collection": collection,
                           "key": f"{key}#life{(position + 1) if position is not None else 'unknown'}"}})

        # references recorded only by key, resolved against record-time commit facts
        references = []
        cursor = None
        while True:
            page = db.query("refs", limit=1000, cursor=cursor)
            for item in page.records:
                write_rev, target = item.revision, item.document.get("target")
                entry = {"ref_key": item.key, "write_rev": write_rev, "original_target": target,
                         "original_document": item.document}
                if write_rev < floor:
                    entry.update(resolution="ambiguous", imported=False,
                                 reason=f"record revision {write_rev} < retained floor {floor}: "
                                        "target state at record time is unreadable; not wiring to any "
                                        "current or recovered document")
                else:
                    point = db.get("docs", target, revision=write_rev)
                    if point is None:
                        target_ops = [op for op in ops_by_key.get(("docs", target), []) if op[0] <= write_rev]
                        first = ops_by_key.get(("docs", target), [None])[0]
                        if not target_ops and first is not None and first[1] == "put" and first[2] == 0:
                            detail = f"target key did not exist before rev {first[0]} (create commit expected=0)"
                        elif not target_ops and first is not None and first[1] == "delete" and first[2] == 0:
                            detail = "target key never existed (only a delete with expected=0 is retained)"
                        elif target_ops:
                            detail = f"target was tombstoned at rev {target_ops[-1][0]} and not recreated"
                        else:
                            detail = "no retained operations for target key"
                        entry.update(resolution="no_live_target", imported=False,
                                     reason=f"target docs/{target} was not live at record revision "
                                            f"{write_rev}: {detail}")
                    else:
                        lives = lives_by_key.get(("docs", target), [])
                        position = next((i for i, life in enumerate(lives)
                                         if (life["create_rev"] or 0) <= point.revision
                                         and (life["delete_rev"] is None or point.revision < life["delete_rev"])),
                                        None)
                        life = lives[position] if position is not None else None
                        hit = next((r for r in recovered
                                    if r["source"]["collection"] == "docs"
                                    and r["source"]["key"] == target
                                    and life is not None and r["source"]["delete_rev"] == life["delete_rev"]), None)
                        if hit is not None:
                            entry.update(resolution="resolved_recovered", imported=True,
                                         lifecycle_ordinal=hit["source"]["lifecycle_ordinal"],
                                         target_key=hit["target"]["key"],
                                         reason=f"unique live lifecycle of docs/{target} at record revision "
                                                f"{write_rev} is recovered lifecycle "
                                                f"{hit['source']['lifecycle_ordinal']}")
                        elif life is not None and life["delete_rev"] is None:
                            entry.update(resolution="resolved_live_at_head", imported=False,
                                         lifecycle_ordinal=(position + 1) if position is not None else None,
                                         reason="unique target is the currently live lifecycle; recovery scope "
                                                "is deleted lifecycles and references must not be wired to the "
                                                "current same-named document")
                        else:
                            entry.update(resolution="resolved_unrecovered", imported=False,
                                         lifecycle_ordinal=(position + 1) if position is not None else None,
                                         reason="unique target lifecycle is not among the recovered lifecycles")
                references.append(entry)
            cursor = page.cursor
            if cursor is None:
                break

        after = {"inspect": db.diagnostics.inspect(deep=True),
                 "fingerprint": db.diagnostics.fingerprint()}
        schemas = {item["name"]: item["schema"] for item in db.catalog.list()}

    if after != before:
        raise SystemExit("source library changed during recovery")

    docs_lines = [json.dumps({"key": item["target"]["key"], "document": item["document"]},
                             ensure_ascii=False, sort_keys=True) for item in recovered]
    refs_lines = [json.dumps({"key": ref["ref_key"],
                              "document": {**ref["original_document"], "target": ref["target_key"]}},
                             ensure_ascii=False, sort_keys=True)
                  for ref in references if ref.get("imported")]

    mapping = {
        "source": {"database_id": before["inspect"]["database_id"], "head": head, "floor": floor},
        "collections": schemas,
        "lifecycles": [{**{k: item[k] for k in ("request_id", "source", "target")},
                        "document": item["document"]} for item in recovered],
        "references": references,
    }
    unretrieved = {
        "evidence_boundary": {
            "floor": floor, "head": head,
            "retained_commits": f"{floor + 1}..{head}",
            "note": "commits at or below the floor are recycled and reads below the floor are rejected; "
                    "facts in that range cannot be confirmed or derived"},
        "deletion_events": unretrieved_events,
        "references_not_imported": [
            {"ref_key": ref["ref_key"], "write_rev": ref["write_rev"], "original_target": ref["original_target"],
             "resolution": ref["resolution"], "reason": ref["reason"]}
            for ref in references if not ref.get("imported")],
    }
    (out / "importable_docs.jsonl").write_text("".join(line + "\n" for line in docs_lines))
    (out / "importable_refs.jsonl").write_text("".join(line + "\n" for line in refs_lines))
    (out / "mapping.json").write_text(json.dumps(mapping, indent=2, ensure_ascii=False) + "\n")
    (out / "unretrieved.json").write_text(json.dumps(unretrieved, indent=2, ensure_ascii=False) + "\n")
    (out / "source_state_before.json").write_text(json.dumps(before, indent=2, ensure_ascii=False) + "\n")
    (out / "source_state_after.json").write_text(json.dumps(after, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"recovered_lifecycles": len(recovered), "docs_lines": len(docs_lines),
                      "refs_lines": len(refs_lines), "unretrieved_events": len(unretrieved_events),
                      "references_not_imported": len(unretrieved["references_not_imported"]),
                      "source_unchanged": after == before}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/strata_recovery/data")
