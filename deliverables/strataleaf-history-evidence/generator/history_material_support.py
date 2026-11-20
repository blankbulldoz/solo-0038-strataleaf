"""Shared helpers for the StrataLeaf history evidence material builder.

This module is a material-preparation utility. It is not part of the
``strataleaf`` product package and must not be imported by product code.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

COLLECTION = "segments"
SELECTED_VIEWS = [1, 3, 5, 7]
KEYS = ["seg_center_06", "seg_east_01", "seg_legacy_05", "seg_north_03",
        "seg_south_04", "seg_west_02"]

SCHEMA = {
    "fields": {"region": "string", "sequence": "integer", "label": "string"},
    "required": ["region", "sequence", "label"],
    "additional": False,
}

EAST_BASELINE = {"region": "east", "sequence": 1, "label": "baseline"}
EAST_INTERIM = {"region": "east", "sequence": 11, "label": "interim"}
WEST_BASELINE = {"region": "west", "sequence": 2, "label": "baseline"}
NORTH_BASELINE = {"region": "north", "sequence": 3, "label": "baseline"}
NORTH_REVISED = {"region": "north", "sequence": 33, "label": "revised"}
SOUTH_BASELINE = {"region": "south", "sequence": 4, "label": "baseline"}
LEGACY_BASELINE = {"region": "east", "sequence": 5, "label": "baseline"}
LEGACY_REVISED = {"region": "east", "sequence": 55, "label": "revised"}
CENTER_NEW = {"region": "center", "sequence": 6, "label": "new"}
CENTER_UPDATED = {"region": "center", "sequence": 66, "label": "updated"}


def put_op(key, document, expected):
    return {"action": "put", "collection": COLLECTION, "key": key,
            "document": document, "expected": expected, "expires_at": None}


def delete_op(key, expected):
    return {"action": "delete", "collection": COLLECTION, "key": key,
            "document": None, "expected": expected, "expires_at": None}


# Seven commits: create, revise, restore-with-new-value, delete, recreate with
# the original content, retire/add, final touch. Revisions 1..7 are returned by
# the real commit API and recorded in observations.jsonl.
COMMIT_PLAN = [
    {"request_key": "seed_baseline", "operations": [
        put_op("seg_east_01", EAST_BASELINE, 0),
        put_op("seg_west_02", WEST_BASELINE, 0),
        put_op("seg_north_03", NORTH_BASELINE, 0),
        put_op("seg_south_04", SOUTH_BASELINE, 0),
        put_op("seg_legacy_05", LEGACY_BASELINE, 0),
    ]},
    {"request_key": "revise_alpha_north", "operations": [
        put_op("seg_east_01", EAST_INTERIM, 1),
        put_op("seg_north_03", NORTH_REVISED, 1),
    ]},
    {"request_key": "restore_alpha_revise_legacy", "operations": [
        put_op("seg_east_01", EAST_BASELINE, 2),
        put_op("seg_legacy_05", LEGACY_REVISED, 1),
    ]},
    {"request_key": "retire_west", "operations": [
        delete_op("seg_west_02", 1),
    ]},
    {"request_key": "recreate_west", "operations": [
        put_op("seg_west_02", WEST_BASELINE, 4),
    ]},
    {"request_key": "retire_south_add_center", "operations": [
        delete_op("seg_south_04", 1),
        put_op("seg_center_06", CENTER_NEW, 0),
    ]},
    {"request_key": "final_touch", "operations": [
        delete_op("seg_north_03", 2),
        put_op("seg_center_06", CENTER_UPDATED, 6),
    ]},
]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_sha(value):
    if value is None:
        return None
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def jsonable(value):
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def summarize_commit(result):
    return jsonable(result)


def summarize_page(page):
    return {"revision": page.revision, "document_count": len(page.records),
            "keys": [record.key for record in page.records], "plan": page.plan}


def summarize_record(record):
    return None if record is None else jsonable(record)


def summarize_feed(commits):
    return [{"revision": commit["revision"], "operation_count": len(commit["operations"])}
            for commit in commits]


class Observer:
    """Record every local API observation used to build the material."""

    def __init__(self):
        self.entries = []
        self._seq = 0

    @property
    def last_seq(self):
        return self._seq

    def _append(self, api, arguments, outcome, result=None, error=None, note_zh=None):
        self._seq += 1
        entry = {"seq": self._seq, "recorded_at_utc": utc_now(),
                 "observation_source": "local-strataleaf-api", "api": api,
                 "arguments": jsonable(arguments), "outcome": outcome,
                 "note_zh": note_zh}
        if outcome == "ok":
            entry["result"] = jsonable(result)
        else:
            entry["error"] = error
        self.entries.append(entry)
        return entry

    def call(self, api, arguments, function, note_zh=None, summarize=None):
        try:
            value = function()
        except Exception as exc:
            self._append(api, arguments, "error",
                         error={"type": type(exc).__name__, "message": str(exc)},
                         note_zh=note_zh)
            raise
        result = summarize(value) if summarize else value
        self._append(api, arguments, "ok", result=result, note_zh=note_zh)
        return value

    def attempt(self, api, arguments, function, note_zh=None, summarize=None):
        try:
            return True, self.call(api, arguments, function, note_zh=note_zh, summarize=summarize)
        except Exception as exc:  # expected probe failure, already recorded
            return False, exc


def execute_plan(db, observer):
    commits = []
    for entry in COMMIT_PLAN:
        batch = db.batch(entry["request_key"])
        for operation in entry["operations"]:
            if operation["action"] == "put":
                batch.put(operation["collection"], operation["key"], operation["document"],
                          expected=operation["expected"], expires_at=operation["expires_at"])
            else:
                batch.delete(operation["collection"], operation["key"],
                             expected=operation["expected"])
        result = observer.call(
            "Database.batch(request_key).commit",
            {"request_key": entry["request_key"], "operations": entry["operations"]},
            batch.commit,
            note_zh="真实提交；revision 来自 CommitResult，操作序列原样记录。",
            summarize=summarize_commit,
        )
        commits.append({"revision": result.revision, "request_key": entry["request_key"],
                        "operation_count": len(entry["operations"])})
    return commits


def capture_change_process(db, observer, commit_results):
    commits = observer.call(
        "Database.feed.read",
        {"since": 0, "limit": 1000},
        lambda: db.feed.read(0, limit=1000),
        note_zh="读取完整提交记录，作为变更过程的原始来源。",
        summarize=summarize_feed,
    )
    by_revision = {item["revision"]: item["request_key"] for item in commit_results}
    output = []
    for commit in commits:
        operations = []
        for index, operation in enumerate(commit["operations"]):
            operations.append({
                "operation_index": index,
                "action": "delete" if operation["document"] is None else "put",
                "collection": operation["collection"],
                "key": operation["key"],
                "document": operation["document"],
                "expected": operation["expected"],
                "expires_at": operation["expires_at"],
                "content_sha256": content_sha(operation["document"]),
            })
        output.append({
            "revision": commit["revision"],
            "commit_request_key": by_revision.get(commit["revision"]),
            "operation_count": len(operations),
            "operations": operations,
        })
    return {
        "format": "strataleaf-history-evidence/1",
        "record_kind": "change_process",
        "record_kind_zh": "变更过程记录（不是历史读视图）",
        "description_zh": "按提交修订列出每个操作；回答“哪个提交对哪个键做了什么”，不回答“某个修订可见什么”。",
        "source_api": "Database.feed.read(since=0, limit=1000)",
        "commit_request_key_source_zh": "提交记录本身不携带 request_key；此处由本材料生成时的 Database.batch(request_key).commit() 回执补充映射。",
        "commits": output,
    }


def capture_views(db, observer):
    views = []
    for revision in SELECTED_VIEWS:
        page = observer.call(
            "Database.query",
            {"collection": COLLECTION, "predicate": None, "limit": 1000, "revision": revision},
            lambda revision=revision: db.query(COLLECTION, revision=revision, limit=1000),
            note_zh="固定读修订导出当前可见文档；游标未保留。",
            summarize=summarize_page,
        )
        fingerprint = observer.call(
            "Database.diagnostics.fingerprint",
            {"revision": revision},
            lambda revision=revision: db.diagnostics.fingerprint(revision),
            note_zh="同一修订的逻辑内容指纹，用于复核视图导出。",
        )
        records = [{
            "key": record.key,
            "actual_version": record.revision,
            "document": record.document,
            "expires_at": record.expires_at,
            "content_sha256": content_sha(record.document),
        } for record in page.records]
        present = {record["key"] for record in records}
        absent = []
        for key in KEYS:
            if key in present or key in absent:
                continue
            absent.append(key)
        views.append({
            "format": "strataleaf-history-evidence/1",
            "record_kind": "historical_read_view",
            "record_kind_zh": "历史读视图（固定修订的可见文档快照）",
            "description_zh": "该文件只描述指定读修订下的可见文档；actual_version 是该文档在版本链中的实际修订号。",
            "collection": COLLECTION,
            "read_revision": revision,
            "source_api": "Database.query(collection, revision=read_revision, limit=1000)",
            "predicate": None,
            "fingerprint": fingerprint,
            "document_count": len(records),
            "records": records,
            "absent_keys": absent,
            "absence_note_zh": "absent_keys 可能是尚未创建或已被墓碑隐藏；逐键原因见 data/document-inventory.json。",
        })
    return views


def confirm_versions(db, observer, change_process):
    confirmations = {}
    for commit in change_process["commits"]:
        revision = commit["revision"]
        for operation in commit["operations"]:
            key = operation["key"]
            observed = observer.call(
                "Database.get",
                {"collection": COLLECTION, "key": key, "revision": revision},
                lambda key=key, revision=revision: db.get(COLLECTION, key, revision=revision),
                note_zh="在该提交修订读取同一个键，复核实际版本或墓碑可见性。",
                summarize=summarize_record,
            )
            confirmations[(key, revision)] = {
                "outcome": "tombstone_hides_document" if observed is None else "record_returned",
                "observed": summarize_record(observed),
            }
    return confirmations


def build_inventory(change_process, views, confirmations):
    keys = []
    by_key = {}
    ordered_keys = []
    for commit in change_process["commits"]:
        for operation in commit["operations"]:
            key = operation["key"]
            if key not in by_key:
                by_key[key] = []
                ordered_keys.append(key)
            by_key[key].append((commit, operation))

    view_records = {}
    for view in views:
        view_records[view["read_revision"]] = {record["key"]: record for record in view["records"]}

    for key in ordered_keys:
        versions = []
        previous_document = "unset"
        for commit, operation in by_key[key]:
            document = operation["document"]
            if operation["action"] == "delete":
                status = "deleted"
            elif previous_document == "unset":
                status = "created"
            elif previous_document is None:
                status = "recreated"
            else:
                status = "rewritten"
            confirmation = confirmations[(key, commit["revision"])]
            versions.append({
                "actual_version": commit["revision"],
                "commit_revision": commit["revision"],
                "commit_request_key": commit["commit_request_key"],
                "operation_index": operation["operation_index"],
                "operation": operation["action"],
                "status": status,
                "document": document,
                "expires_at": operation["expires_at"],
                "content_sha256": operation["content_sha256"],
                "api_confirmation": confirmation,
            })
            previous_document = document

        visibility = {}
        for view in views:
            revision = view["read_revision"]
            record = view_records[revision].get(key)
            if record is not None:
                visibility[str(revision)] = {
                    "visible": True,
                    "actual_version": record["actual_version"],
                    "source": "historical_read_view",
                }
                continue
            prior = [item for item in versions if item["actual_version"] <= revision]
            if not prior:
                reason = "not_yet_created"
            elif prior[-1]["document"] is None:
                reason = "tombstone_hides"
            else:
                reason = "unexpected_absent"
            visibility[str(revision)] = {"visible": False, "reason": reason,
                                         "source": "derived_from_versions"}

        last = versions[-1]
        keys.append({
            "key": key,
            "first_actual_version": versions[0]["actual_version"],
            "last_actual_version": last["actual_version"],
            "final_state": "deleted" if last["document"] is None else "present",
            "version_count": len(versions),
            "versions": versions,
            "visibility_in_selected_views": visibility,
        })

    return {
        "format": "strataleaf-history-evidence/1",
        "record_kind": "document_inventory",
        "record_kind_zh": "文档键—实际版本—所属提交清单（派生索引，不是独立事实源）",
        "description_zh": "每个键的每个实际版本、所属提交修订与 request_key、操作类型、内容哈希，以及在所选读视图中的可见版本。",
        "derivation_zh": "版本顺序与操作来自 data/change-process.json；读视图可见性来自 data/views/*.json；每个版本另附 Database.get 在提交修订上的复核结果。",
        "tombstone_note_zh": "公开 Database.get 对墓碑返回空值，不能返回墓碑版本号；墓碑的 actual_version 以变更过程中的提交修订标识，并在 api_confirmation 中记录该限制。",
        "keys": keys,
    }


def build_key_explanations(inventory, views):
    view_by_revision = {view["read_revision"]: {record["key"]: record for record in view["records"]}
                        for view in views}
    explanations = []
    for entry in inventory["keys"]:
        key = entry["key"]
        visible = []
        for revision in SELECTED_VIEWS:
            record = view_by_revision[revision].get(key)
            if record is not None:
                visible.append((revision, record))
        if len(visible) < 2:
            continue
        first_view, first_record = visible[0]
        last_view, last_record = visible[-1]
        if first_record["content_sha256"] != last_record["content_sha256"]:
            continue
        if first_record["actual_version"] == last_record["actual_version"]:
            continue
        intermediate = [version for version in entry["versions"]
                        if first_record["actual_version"] < version["actual_version"] < last_record["actual_version"]]
        if not intermediate:
            continue
        explanations.append({
            "key": key,
            "explanation_type": "same_endpoint_content_different_actual_version",
            "endpoint_definition_zh": "端点为该键在所选读视图中的首次可见记录与末次可见记录。",
            "first_endpoint": {"view_revision": first_view,
                               "actual_version": first_record["actual_version"],
                               "content_sha256": first_record["content_sha256"]},
            "last_endpoint": {"view_revision": last_view,
                              "actual_version": last_record["actual_version"],
                              "content_sha256": last_record["content_sha256"]},
            "same_content_but_different_version": True,
            "intermediate_versions": intermediate,
            "why_zh": "首末端点内容哈希相同，但 actual_version 不同；中间版本发生过变化。因此内容哈希只能说明“内容相同”，不能替代版本号或提交号。",
            "recipient_rule_zh": "引用该键时必须同时给出 actual_version 与 commit_revision；若只说“内容相同”，会掩盖中间删除或改写。",
        })
    return {
        "format": "strataleaf-history-evidence/1",
        "record_kind": "key_explanation",
        "record_kind_zh": "同内容异版本解释（派生说明）",
        "description_zh": "列出端点内容相同但实际版本不同、且中间发生过变化的键，并给出各中间版本与所属提交。",
        "derivation_zh": "端点取自 data/views/*.json 的可见记录；中间版本取自 data/document-inventory.json。",
        "explanations": explanations,
    }
