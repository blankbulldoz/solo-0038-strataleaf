#!/usr/bin/env python3
"""Build the recipient-facing StrataLeaf history evidence package.

This is a material-preparation tool, not product code. It uses only the public
StrataLeaf API against a temporary local database, then writes a self-describing
package under ``deliverables/strataleaf-history-evidence``.

The package deliberately separates:
  * historical read views (what was visible at fixed read revisions),
  * change process records (which committed operations created which versions),
  * a derived key/version/commit inventory and explanation of equal-content keys,
  * provenance for every local API observation,
  * explicit gap markers for what the material does not contain.
"""
from __future__ import annotations

import csv
import json
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from strataleaf import Database  # noqa: E402

import history_material_support as support  # noqa: E402

PACKAGE_ID = "strataleaf-history-evidence"
PACKAGE_DIR = ROOT / "deliverables" / PACKAGE_ID
DATA_DIR = PACKAGE_DIR / "data"
VIEW_DIR = DATA_DIR / "views"


def dump_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dump_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
                    encoding="utf-8")


def git(*arguments):
    try:
        completed = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True,
                                   text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def build_gaps(compaction, probe_records, view_revisions, change_process):
    commit_revisions = [commit["revision"] for commit in change_process["commits"]]
    unexported = [revision for revision in commit_revisions if revision not in view_revisions]
    return {
        "format": "strataleaf-history-evidence/1",
        "record_kind": "gap_register",
        "record_kind_zh": "缺口登记（明确标记材料缺失或不可再取的部分）",
        "description_zh": "凡材料未包含、或来源库不再提供的片段，均在此登记原因、影响与可用替代。接收者不得把未登记的空缺当作“无变化”。",
        "gaps": [
            {
                "gap_id": "GAP-001",
                "kind": "views_not_exported_for_every_revision",
                "summary_zh": "只导出了所选修订的历史读视图；其余提交修订没有单独的快照文件。",
                "missing": {"unexported_view_revisions": unexported,
                            "exported_view_revisions": list(view_revisions)},
                "impact_zh": "不能把“没有某修订的视图文件”理解成该修订没有提交或没有变化。",
                "recovery_zh": "这些修订的逐操作内容仍完整保存在 data/change-process.json；任意键在该修订的状态可由 data/document-inventory.json 的 versions 推出。",
                "completeness": "recoverable_from_other_parts",
            },
            {
                "gap_id": "GAP-002",
                "kind": "source_history_reclaimed_after_export",
                "summary_zh": "材料导出后，临时源库执行了显式回收；源库不再能重读所选视图对应的旧修订和旧提交。",
                "cause": {"api": "Database.retention.compact", "arguments": {"target": compaction["requested"]},
                          "result": compaction},
                "missing": {"readable_floor_after_compaction": compaction["floor"],
                            "removed_versions": compaction["removed_versions"],
                            "removed_commits": compaction["removed_commits"]},
                "probe_results": probe_records,
                "impact_zh": "材料中的历史读视图和变更过程是这些片段的留存副本；不能用导出后的源库再次核对它们。",
                "recovery_zh": "以 data/views/*.json 与 data/change-process.json 及其 sha256 为准，并运行 verify.py 做内部交叉校验。",
                "completeness": "retained_copy_in_material",
            },
            {
                "gap_id": "GAP-003",
                "kind": "tombstone_version_not_readable_through_get",
                "summary_zh": "公开 Database.get 对墓碑返回空值，不能通过它取得“删除版本号”。",
                "missing": {"api_return_for_deleted_key": None},
                "impact_zh": "若只靠 Database.get，会看不出键是“从未创建”还是“已删除”。",
                "recovery_zh": "删除的 actual_version 由 data/change-process.json 的操作修订给出；data/document-inventory.json 的 tombstone_note_zh 与 api_confirmation 记录了这一限制。",
                "completeness": "recoverable_from_other_parts",
            },
            {
                "gap_id": "GAP-004",
                "kind": "internal_projection_not_exported",
                "summary_zh": "材料不包含 SQLite 内部表、索引投影、WAL 文件或游标签名。",
                "missing": {"internal_tables": ["versions", "commits", "receipts", "index_values", "pins", "consumers"],
                            "wal_and_cursor_tokens": "not_exported"},
                "impact_zh": "接收者不能据此复核物理存储细节或索引物化，只能复核公开 API 语义。",
                "recovery_zh": "如需物理层复核，应在源环境用 Database.diagnostics.inspect(deep=True) 单独取证；本材料只声明公开读视图与提交事实。",
                "completeness": "out_of_scope",
            },
        ],
    }


def render_view_markdown(view):
    lines = [f"# 历史读视图 · 读修订 {view['read_revision']}", "",
             "> 本文件是 `data/views/"
             f"revision-{view['read_revision']:04d}.json` 的可读渲染，不是独立事实源；"
             "如有差异以 JSON 与 `manifest.json` 摘要为准。", "",
             f"- `record_kind`: `{view['record_kind']}`（历史读视图）",
             f"- 集合：`{view['collection']}`",
             f"- 读修订：{view['read_revision']}",
             f"- 来源 API：`{view['source_api']}`",
             f"- 可见文档数：{view['document_count']}",
             f"- 内容指纹：`{view['fingerprint']['sha256']}`", "",
             "## 可见文档", "",
             "| 文档键 | actual_version | 内容哈希（前 12 位） | 文档 |",
             "| --- | --- | --- | --- |"]
    for record in view["records"]:
        document = json.dumps(record["document"], ensure_ascii=False, sort_keys=True)
        document = document.replace("|", "\\|")
        lines.append(f"| `{record['key']}` | {record['actual_version']} | "
                     f"`{record['content_sha256'][:12]}` | `{document}` |")
    lines.extend(["", "## 本修订不可见的键", "",
                  "这些键要么尚未创建，要么被墓碑隐藏；逐键原因见 "
                  "`data/document-inventory.json` 的 `visibility_in_selected_views`。", ""])
    if view["absent_keys"]:
        for key in view["absent_keys"]:
            lines.append(f"- `{key}`")
    else:
        lines.append("- （无）")
    lines.append("")
    return "\n".join(lines)


def render_timeline_markdown(change_process):
    lines = ["# 变更过程时间线", "",
             "> 本文件是 `data/change-process.json` 的可读渲染，不是历史读视图；"
             "它按提交列出操作，不表示某个修订下的完整可见集合。", "",
             f"- `record_kind`: `{change_process['record_kind']}`（变更过程）",
             f"- 来源 API：`{change_process['source_api']}`", ""]
    for commit in change_process["commits"]:
        lines.append(f"## 提交修订 {commit['revision']} · `{commit['commit_request_key']}`")
        lines.append("")
        lines.append("| # | 操作 | 集合 | 键 | expected | 内容哈希（前 12 位） |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for operation in commit["operations"]:
            digest = (operation["content_sha256"] or "（删除）")[:12]
            lines.append(f"| {operation['operation_index']} | `{operation['action']}` | "
                         f"`{operation['collection']}` | `{operation['key']}` | "
                         f"{operation['expected']} | `{digest}` |")
        lines.append("")
    return "\n".join(lines)


def render_inventory_markdown(inventory):
    lines = ["# 文档键—实际版本—所属提交清单", "",
             "> 本文件是 `data/document-inventory.json` 的可读渲染，属于派生索引，"
             "不是独立事实源。", "",
             "`actual_version` 是该键的版本号；`commit` 是产生它的提交修订与 request_key。"
             "两者在本库中数值相同，但含义不同。", "",
             "| 文档键 | actual_version | commit 修订 | commit request_key | 操作 | 状态 | 内容哈希（前 12 位） |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for entry in inventory["keys"]:
        for version in entry["versions"]:
            digest = (version["content_sha256"] or "（删除）")[:12]
            lines.append(f"| `{entry['key']}` | {version['actual_version']} | "
                         f"{version['commit_revision']} | `{version['commit_request_key']}` | "
                         f"`{version['operation']}` | {version['status']} | `{digest}` |")
    lines.append("")
    return "\n".join(lines)


def render_readme(manifest, inventory, explanations, gaps):
    lines = []
    lines.append("# StrataLeaf 历史文档材料包")
    lines.append("")
    lines.append("本目录是一份**对外交付的历史材料**，由临时数据库上的公开 API 生成；不是产品功能，也未改动 `strataleaf/` 产品代码。")
    lines.append("")
    lines.append("## 先用一分钟分清两类记录")
    lines.append("")
    lines.append("| 类别 | 文件 | 回答的问题 | 不能回答的问题 |")
    lines.append("| --- | --- | --- | --- |")
    lines.append("| **历史读视图** | `data/views/revision-0001.json` 等（附 `.md` 可读渲染） | 在某个固定读修订下，哪些文档可见、实际版本是多少 | 中间发生过哪些提交 |")
    lines.append("| **变更过程** | `data/change-process.json`（附 `data/timeline.md`） | 哪个提交对哪个键做了什么 | 某个修订的完整可见集合 |")
    lines.append("| **派生清单** | `data/document-inventory.json`（附 `.csv` / `.md`） | 文档键、实际版本、所属提交如何一一对应 | 独立的新事实 |")
    lines.append("| **同内容解释** | `data/key-explanations.json` | 为什么端点内容相同却版本不同 | 其他键的语义 |")
    lines.append("| **缺口登记** | `data/gaps.json` | 材料缺了什么、为什么、如何补救 | 补齐材料之外的证据 |")
    lines.append("| **观察来源** | `data/observations.jsonl` | 每条事实来自哪次本地 API 调用 | 远程或第三方来源 |")
    lines.append("")
    lines.append("每个 JSON 文件都带 `record_kind` 与 `record_kind_zh`，可机器区分，不依赖文件名。")
    lines.append("")
    lines.append("## 这批材料是怎么产生的")
    lines.append("")
    lines.append("1. 在独立临时目录创建全新 SQLite 数据库，建一个 `segments` 集合。")
    lines.append("2. 用 `Database.batch(request_key).commit()` 连续提交 7 次，覆盖新增、改写、删除与重建。")
    lines.append("3. 用 `Database.query(..., revision=N)` 导出 4 个所选修订的读视图，并用 `Database.diagnostics.fingerprint(N)` 记录内容指纹。")
    lines.append("4. 用 `Database.feed.read(0)` 取得完整提交序列，形成变更过程。")
    lines.append("5. 用 `Database.get(..., revision=N)` 在每个提交修订复核对应键，形成版本清单中的 API 旁证。")
    lines.append("6. 导出后显式执行 `Database.retention.compact(head)`，再尝试重读旧修订和旧提交，把失败与被回收的片段登记为缺口。")
    lines.append("")
    lines.append("所有观察都是在本机临时库上通过公开库 API 取得的；临时库在结束时删除，不随材料交付。")
    lines.append("")
    lines.append("## 键与版本速览")
    lines.append("")
    lines.append(f"共 {len(inventory['keys'])} 个文档键、"
                 f"{sum(entry['version_count'] for entry in inventory['keys'])} 个实际版本；"
                 f"所选视图修订为 {', '.join(str(view) for view in support.SELECTED_VIEWS)}。")
    lines.append("")
    lines.append("### 内容相同、版本不同的键")
    lines.append("")
    if explanations["explanations"]:
        for item in explanations["explanations"]:
            first = item["first_endpoint"]
            last = item["last_endpoint"]
            intermediate = ", ".join(
                f"r{version['actual_version']}({version['status']})"
                for version in item["intermediate_versions"])
            lines.append(f"- `{item['key']}`：读视图 r{first['view_revision']} 与 r{last['view_revision']} "
                         f"内容哈希同为 `{first['content_sha256'][:12]}…`，但实际版本分别为 "
                         f"{first['actual_version']} 与 {last['actual_version']}；中间版本：{intermediate}。")
    else:
        lines.append("- 本批次未出现端点内容相同而实际版本不同的键。")
    lines.append("")
    lines.append("这说明**内容哈希相同不等于同一版本**：删除后重建、改写后改回，都会产生新的实际版本。")
    lines.append("")
    lines.append("## 缺口必须显式对待")
    lines.append("")
    for gap in gaps["gaps"]:
        lines.append(f"- `{gap['gap_id']}`（{gap['kind']}，{gap['completeness']}）：{gap['summary_zh']}")
        lines.append(f"  - 影响：{gap['impact_zh']}")
        lines.append(f"  - 补救：{gap['recovery_zh']}")
    lines.append("")
    lines.append("## 自校验")
    lines.append("")
    lines.append("在任意装有 Python 3.11+ 的机器上运行：")
    lines.append("")
    lines.append("```")
    lines.append("python3 deliverables/strataleaf-history-evidence/verify.py")
    lines.append("```")
    lines.append("")
    lines.append("校验会检查：文件摘要、清单与变更过程的一致性、视图记录与清单的一致性、可见性推导、同内容解释的前提，以及缺口登记的必要字段。")
    lines.append("")
    lines.append("## 边界")
    lines.append("")
    lines.append("- 材料只描述公开 API 可观察的事实，不声明物理存储或索引投影。")
    lines.append("- `actual_version` 是本库中该键的版本号；本库每次提交对每个键至多写一次，因此它等于产生该版本的提交修订，但两者含义不同，清单中分别列出。")
    lines.append("- 生成时间、`database_id` 与摘要每次重建都会不同；请以本包内 `manifest.json` 记录的摘要为准。")
    lines.append("")
    lines.append("## 重建")
    lines.append("")
    lines.append("```")
    lines.append("python3 tools/build_history_materials.py")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main():
    if PACKAGE_DIR.exists():
        shutil.rmtree(PACKAGE_DIR)
    DATA_DIR.mkdir(parents=True)
    VIEW_DIR.mkdir(parents=True)

    observer = support.Observer()
    with tempfile.TemporaryDirectory(prefix="strataleaf-evidence-") as directory:
        database_path = Path(directory) / "source.db"
        with Database(database_path) as db:
            observer.call(
                "Database.open",
                {"path": "<temporary>/source.db"},
                lambda: db.revision,
                note_zh="打开临时库；路径不交付，只保留观察来源。",
            )
            observer.call(
                "Database.catalog.create",
                {"collection": support.COLLECTION, "schema": support.SCHEMA},
                lambda: db.catalog.create(support.COLLECTION, support.SCHEMA),
                note_zh="创建集合与模式；集合创建不是幂等操作。",
            )
            commit_results = support.execute_plan(db, observer)
            views = support.capture_views(db, observer)
            change_process = support.capture_change_process(db, observer, commit_results)
            confirmations = support.confirm_versions(db, observer, change_process)
            inventory = support.build_inventory(change_process, views, confirmations)
            explanations = support.build_key_explanations(inventory, views)
            head = db.revision
            source_inspection = observer.call(
                "Database.diagnostics.inspect",
                {"deep": False},
                lambda: db.diagnostics.inspect(False),
                note_zh="导出时的头部/水位与数据库身份；不作为物理完整性证明。",
            )
            source_metadata = {
                "format": "strataleaf-history-evidence/1",
                "record_kind": "source_metadata",
                "record_kind_zh": "来源库元数据（不含数据库文件本身）",
                "observation_source": "local-strataleaf-api",
                "database_file": "temporary; deleted after export",
                "database_id": source_inspection["database_id"],
                "head_revision_at_export": source_inspection["head"],
                "floor_revision_at_export": source_inspection["floor"],
                "selected_view_revisions": support.SELECTED_VIEWS,
                "collection": support.COLLECTION,
                "schema": support.SCHEMA,
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "strataleaf_version": __import__("strataleaf").__version__,
                "git_head": git("rev-parse", "HEAD"),
                "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                "note_zh": "临时库已删除；本包是这些片段的留存副本。",
            }
            if source_inspection["head"] != head:
                raise RuntimeError("revision changed between material observations")

            compaction = observer.call(
                "Database.retention.compact",
                {"target": head},
                lambda: db.retention.compact(head),
                note_zh="导出后显式回收历史，用于演示“来源库不再可重读”的缺口。",
            )
            probe_records = []
            for name, api, arguments, function in (
                ("old_view_read", "Database.query",
                 {"collection": support.COLLECTION, "revision": support.SELECTED_VIEWS[0]},
                 lambda: db.query(support.COLLECTION, revision=support.SELECTED_VIEWS[0])),
                ("old_revision_get", "Database.get",
                 {"collection": support.COLLECTION, "key": inventory["keys"][0]["key"],
                  "revision": support.SELECTED_VIEWS[0]},
                 lambda: db.get(support.COLLECTION, inventory["keys"][0]["key"], support.SELECTED_VIEWS[0])),
                ("old_feed_read", "Database.feed.read", {"since": 0},
                 lambda: db.feed.read(0)),
            ):
                try:
                    observer.call(api, arguments, function,
                                  note_zh="回收后复查；预期失败，用于缺口登记。")
                except Exception as exc:
                    probe_records.append({"probe": name, "api": api, "arguments": arguments,
                                          "outcome": "rejected", "error_type": type(exc).__name__,
                                          "error_message": str(exc)})
                else:
                    probe_records.append({"probe": name, "api": api, "arguments": arguments,
                                          "outcome": "unexpectedly_succeeded"})

            post_compaction = observer.call(
                "Database.diagnostics.inspect",
                {"deep": True},
                lambda: db.diagnostics.inspect(True),
                note_zh="回收后的完整性检查，确认剩余库仍自洽。",
            )

    gaps = build_gaps(compaction, probe_records, support.SELECTED_VIEWS, change_process)
    gaps["post_compaction_inspection"] = post_compaction

    # Write data files.
    for view in views:
        dump_json(VIEW_DIR / f"revision-{view['read_revision']:04d}.json", view)
        (VIEW_DIR / f"revision-{view['read_revision']:04d}.md").write_text(
            render_view_markdown(view), encoding="utf-8")
    dump_json(DATA_DIR / "change-process.json", change_process)
    dump_json(DATA_DIR / "document-inventory.json", inventory)
    dump_json(DATA_DIR / "key-explanations.json", explanations)
    dump_json(DATA_DIR / "gaps.json", gaps)
    dump_json(DATA_DIR / "source-metadata.json", source_metadata)
    dump_jsonl(DATA_DIR / "observations.jsonl", observer.entries)
    (DATA_DIR / "timeline.md").write_text(render_timeline_markdown(change_process), encoding="utf-8")
    (DATA_DIR / "document-inventory.md").write_text(render_inventory_markdown(inventory), encoding="utf-8")

    inventory_csv = DATA_DIR / "document-inventory.csv"
    with inventory_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["key", "actual_version", "commit_revision", "commit_request_key",
                         "operation", "status", "content_sha256"])
        for entry in inventory["keys"]:
            for version in entry["versions"]:
                writer.writerow([entry["key"], version["actual_version"], version["commit_revision"],
                                 version["commit_request_key"], version["operation"],
                                 version["status"], version["content_sha256"] or ""])

    readme_text = render_readme({"package_id": PACKAGE_ID}, inventory, explanations, gaps)
    (PACKAGE_DIR / "README.md").write_text(readme_text, encoding="utf-8")
    verifier_path = ROOT / "tools" / "verify_history_materials.py"
    shutil.copyfile(verifier_path, PACKAGE_DIR / "verify.py")
    generator_dir = PACKAGE_DIR / "generator"
    generator_dir.mkdir()
    shutil.copyfile(Path(__file__), generator_dir / "build_history_materials.py")
    shutil.copyfile(ROOT / "tools" / "history_material_support.py",
                    generator_dir / "history_material_support.py")

    files = []
    for path in sorted(PACKAGE_DIR.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files.append({"path": str(path.relative_to(PACKAGE_DIR)), "bytes": path.stat().st_size,
                          "sha256": support.file_sha(path)})
    manifest = {
        "format": "strataleaf-history-evidence/1",
        "record_kind": "manifest",
        "record_kind_zh": "材料包清单与文件摘要",
        "package_id": PACKAGE_ID,
        "generated_at_utc": support.utc_now(),
        "generator": "tools/build_history_materials.py",
        "generator_sha256": support.file_sha(Path(__file__)),
        "support_sha256": support.file_sha(ROOT / "tools" / "history_material_support.py"),
        "product_code_modified": False,
        "source_metadata": "data/source-metadata.json",
        "record_kinds": {
            "historical_read_view": "data/views/revision-0001.json, 0003, 0005, 0007",
            "change_process": "data/change-process.json",
            "document_inventory": "data/document-inventory.json (+ .csv)",
            "key_explanation": "data/key-explanations.json",
            "gap_register": "data/gaps.json",
            "api_observations": "data/observations.jsonl",
        },
        "selected_view_revisions": support.SELECTED_VIEWS,
        "commit_revisions": [commit["revision"] for commit in change_process["commits"]],
        "document_keys": [entry["key"] for entry in inventory["keys"]],
        "files": files,
    }
    dump_json(PACKAGE_DIR / "manifest.json", manifest)
    print(json.dumps({"package": str(PACKAGE_DIR), "files": len(files) + 1,
                      "views": support.SELECTED_VIEWS,
                      "commits": manifest["commit_revisions"],
                      "keys": manifest["document_keys"],
                      "explanations": len(explanations["explanations"])},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
