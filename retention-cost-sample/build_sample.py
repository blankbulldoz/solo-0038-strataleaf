"""生成「保留历史的代价」离线样本材料。

本脚本不向 strataleaf 添加任何产品功能，只调用其公开 API（Database、
retention、feed、archives、diagnostics）制造一个小规模、可复核的数据集，
把每个回收阶段的真实读取结果、提交留存情况与文件大小采集为 JSON 证据，
并生成同名目录下的 Markdown 报告。所有数字均来自实际运行，不做估算。

复跑方式（无需联网）：
    python3 -I retention-cost-sample/build_sample.py
"""
import json
import platform
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from strataleaf import Database  # noqa: E402
from strataleaf.errors import SnapshotTooOld  # noqa: E402

SAMPLE_DIR = Path(__file__).resolve().parent
EVIDENCE = SAMPLE_DIR / "evidence"
LIVE_DIR = EVIDENCE / "live"
SNAP_DIR = EVIDENCE / "snapshots"
QUERY_DIR = EVIDENCE / "queries"
DB_PATH = LIVE_DIR / "sample.db"

COLLECTION = "notices"
SCHEMA = {
    "fields": {"status": "string", "priority": "integer", "owner": "string"},
    "required": ["status"],
}
KEYS = ["notice_a", "notice_b", "notice_c", "notice_d"]
PROBE_REVISIONS = [1, 2, 3, 4, 5, 6, 7]


def reset_outputs():
    for folder in (LIVE_DIR, SNAP_DIR, QUERY_DIR):
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)


def dump(name, payload):
    path = QUERY_DIR / name
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return path


def ro_connect():
    conn = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def physical_state():
    """通过只读 SQLite 连接采集物理表事实（仅用于证据，不属于产品调用）。"""
    with ro_connect() as conn:
        versions = [dict(row) for row in conn.execute(
            "SELECT collection,key,rev,body IS NULL AS is_tombstone,expires FROM versions ORDER BY collection,key,rev")]
        commits = [{"rev": row["rev"], "request_key": row["request_key"],
                    "payload_bytes": len(row["payload"])}
                   for row in conn.execute("SELECT rev,payload,request_key FROM commits ORDER BY rev")]
        receipts = [dict(row) for row in conn.execute(
            "SELECT request_key,rev,request_hash,count FROM receipts ORDER BY rev")]
        pins = [dict(row) for row in conn.execute("SELECT name,rev FROM pins ORDER BY name")]
        consumers = [dict(row) for row in conn.execute("SELECT name,ack FROM consumers ORDER BY name")]
        meta = dict(conn.execute("SELECT key,value FROM meta").fetchall())
        pragmas = {name: conn.execute(f"PRAGMA {name}").fetchone()[0]
                   for name in ("page_size", "page_count", "freelist_count")}
    return {"head": int(meta["head"]), "floor": int(meta["floor"]),
            "database_id": meta["database_id"], "pragmas": pragmas,
            "pins": pins, "consumers": consumers,
            "versions": versions, "commits": commits, "receipts": receipts}


def file_sizes(label):
    sizes = {}
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(DB_PATH) + suffix)
        sizes[path.name] = path.stat().st_size if path.exists() else None
    with ro_connect() as conn:
        pragmas = {name: conn.execute(f"PRAGMA {name}").fetchone()[0]
                   for name in ("page_size", "page_count", "freelist_count")}
    return {"stage": label, "bytes": sizes, "pragmas": pragmas}


def snapshot(db, label):
    target = SNAP_DIR / f"{label}.db"
    receipt = db.archives.backup(target)
    # 备份 API 在目标目录留下 SQLite 临时侧车文件（库本身不负责清理），样本目录保持干净
    for leftover in SNAP_DIR.glob(".strataleaf-*"):
        leftover.unlink()
    return {"stage": label, "file": target.name, "bytes": target.stat().st_size,
            "sha256": receipt["sha256"], "head": receipt["revision"], "floor": receipt["floor"]}


def space_probe():
    """独立探针库：用足量行让整页被释放，观察 DELETE/回收后文件不缩、空闲页被复用。

    与主样本库完全分离；这里的数字只说明 SQLite 空间机制，不作为性能结论。
    """
    probe_path = LIVE_DIR / "space_probe.db"
    rows = 1500
    observations = []
    with Database(probe_path) as probe_db:
        probe_db.catalog.create("items", {"fields": {"n": "integer"}, "required": ["n"]})

        def measure(label):
            probe_db.checkpoint()
            conn = sqlite3.connect(probe_path)
            pragmas = {k: conn.execute(f"PRAGMA {k}").fetchone()[0]
                       for k in ("page_size", "page_count", "freelist_count", "auto_vacuum")}
            conn.close()
            wal = Path(str(probe_path) + "-wal")
            entry = {"stage": label, "main_bytes": probe_path.stat().st_size,
                     "wal_bytes": wal.stat().st_size if wal.exists() else None, **pragmas}
            observations.append(entry)
            return entry

        measure("probe_empty_after_schema")
        seed = probe_db.batch("probe_bulk_seed")
        for index in range(rows):
            seed.put("items", f"k{index:04}", {"n": index, "pad": "x" * 40})
        seed.commit()
        measure("probe_after_seed_1500")

        delete = probe_db.batch("probe_bulk_delete")
        for index in range(rows):
            delete.delete("items", f"k{index:04}")
        delete.commit()
        compact = probe_db.retention.compact(probe_db.revision)
        measure("probe_after_delete_and_compact")

        reseed = probe_db.batch("probe_bulk_reseed")
        for index in range(rows):
            reseed.put("items", f"j{index:04}", {"n": index, "pad": "y" * 40})
        reseed.commit()
        measure("probe_after_reseed_1500")
    # 探针库的 WAL/SHM 作为活动库文件保留，其字节数已计入 observations
    return {"rows": rows, "compact_result": compact, "observations": observations,
            "db_file": str(probe_path.relative_to(REPO_ROOT))}


def probe_get(db, key, revision):
    try:
        record = db.get(COLLECTION, key, revision=revision)
    except SnapshotTooOld as exc:
        return {"outcome": "rejected_below_floor", "detail": str(exc)}
    if record is None:
        return {"outcome": "tombstone_hidden", "visible_revision": None}
    return {"outcome": "readable", "visible_revision": record.revision, "document": record.document,
            "expires_at": record.expires_at}


def read_matrix(db, stage, floor):
    matrix = {}
    for key in KEYS:
        matrix[key] = {}
        for revision in PROBE_REVISIONS:
            if revision > db.revision:
                matrix[key][str(revision)] = {"outcome": "not_probed_future"}
                continue
            result = probe_get(db, key, revision)
            matrix[key][str(revision)] = result
        matrix[key]["head"] = probe_get(db, key, db.revision)
    return {"stage": stage, "floor": floor, "head": db.revision, "matrix": matrix}


def feed_probe(db, since):
    try:
        commits = db.feed.read(since, limit=100)
        return {"since": since, "outcome": "readable", "commit_revisions": [c["revision"] for c in commits]}
    except SnapshotTooOld as exc:
        return {"since": since, "outcome": "rejected_below_floor", "detail": str(exc)}


def main():
    reset_outputs()
    timeline = []

    with Database(DB_PATH) as db:
        # ---- 样本数据：4 个文档，经历改写、删除、到期 ----
        schema = db.catalog.create(COLLECTION, SCHEMA)
        timeline.append({"step": "create_collection", "schema_generation": schema["generation"]})

        r1 = db.batch("seed_notices_r1") \
            .put(COLLECTION, "notice_a", {"status": "draft", "priority": 1, "owner": "lin"}, expected=0) \
            .put(COLLECTION, "notice_b", {"status": "draft", "priority": 1, "owner": "chen"}, expected=0) \
            .put(COLLECTION, "notice_d", {"status": "draft", "priority": 2, "owner": "he"},
                 expected=0, expires_at=500) \
            .commit()
        timeline.append({"commit": r1.revision, "request_key": "seed_notices_r1", "kind": "atomic_seed",
                         "operations": r1.count, "replayed": r1.replayed})

        r2 = db.batch("revise_a_r2").put(
            COLLECTION, "notice_a", {"status": "published", "priority": 1, "owner": "lin"},
            expected=r1.revision).commit()
        timeline.append({"commit": r2.revision, "request_key": "revise_a_r2", "kind": "rewrite",
                         "operations": r2.count, "replayed": r2.replayed})

        r3 = db.batch("remove_b_r3").delete(COLLECTION, "notice_b", expected=r1.revision).commit()
        timeline.append({"commit": r3.revision, "request_key": "remove_b_r3", "kind": "explicit_delete",
                         "operations": r3.count, "replayed": r3.replayed})

        r4 = db.batch("add_c_r4").put(
            COLLECTION, "notice_c", {"status": "draft", "priority": 3, "owner": "wu"},
            expected=0, expires_at=1000).commit()
        timeline.append({"commit": r4.revision, "request_key": "add_c_r4", "kind": "insert_with_ttl",
                         "operations": r4.count, "replayed": r4.replayed})

        # ---- 两个不同位置的历史保护者：pin 钉在 r2，消费者确认在 r4 ----
        pin_rev = db.retention.pin("audit_pin", r2.revision)
        consumer_start = db.feed.register("mirror_feed", since=r4.revision)
        timeline.append({"step": "protectors_installed", "pin": {"name": "audit_pin", "revision": pin_rev},
                         "consumer": {"name": "mirror_feed", "ack": consumer_start}})

        r5 = db.batch("archive_a_r5").put(
            COLLECTION, "notice_a", {"status": "archived", "priority": 1, "owner": "lin"},
            expected=r2.revision).commit()
        timeline.append({"commit": r5.revision, "request_key": "archive_a_r5", "kind": "rewrite",
                         "operations": r5.count, "replayed": r5.replayed})

        # 时间经过本身不改变读取：sweep 之前 notice_d 仍可当前读取
        read_before_sweep = db.get(COLLECTION, "notice_d")
        sweep_one = db.retention.sweep(now=500)
        timeline.append({"step": "sweep_500", "result": sweep_one,
                         "notice_d_current_read_before_sweep": read_before_sweep.document,
                         "notice_d_history_after_sweep": db.get(
                             COLLECTION, "notice_d", revision=r5.revision).document})

        r7 = db.batch("renew_c_r7").put(
            COLLECTION, "notice_c", {"status": "published", "priority": 2, "owner": "wu"},
            expected=r4.revision, expires_at=2000).commit()
        timeline.append({"commit": r7.revision, "request_key": "renew_c_r7",
                         "kind": "rewrite_renews_ttl", "operations": r7.count, "replayed": r7.replayed})

        # 已被更新的值不会被旧到期时间误删：这次 sweep 应为空
        sweep_idle = db.retention.sweep(now=1000)
        timeline.append({"step": "sweep_1000_after_renewal", "result": sweep_idle})

        head_after_build = db.revision
        db.checkpoint()
        size_log = [file_sizes("stage0_before_compaction")]
        snapshots = [snapshot(db, "stage0")]
        stages = []
        matrices = []
        feed_log = []

        state0 = physical_state()
        dump("physical_stage0.json", state0)
        matrices.append(read_matrix(db, "stage0", state0["floor"]))

        # ---- 阶段 1：pin(r2) 与消费者 ack(r4) 同时存在，请求回收到 head ----
        p1 = db.retention.compact(head_after_build)
        db.checkpoint()
        state1 = physical_state()
        stages.append({"stage": "stage1_pin_and_consumer_hold", "compact_result": p1,
                       "protectors": {"pins": state1["pins"], "consumers": state1["consumers"]}})
        dump("physical_stage1.json", state1)
        matrices.append(read_matrix(db, "stage1", state1["floor"]))
        feed_log.append({"stage": "stage1", "floor": state1["floor"],
                         "probes": [feed_probe(db, s) for s in (0, 1, 2, 4, 7)]})
        size_log.append(file_sizes("stage1_floor_2"))
        snapshots.append(snapshot(db, "stage1"))

        # ---- 阶段 2：释放 pin，消费者仍停在 r4 ----
        db.retention.unpin("audit_pin")
        p2 = db.retention.compact(head_after_build)
        db.checkpoint()
        state2 = physical_state()
        stages.append({"stage": "stage2_only_consumer_holds", "compact_result": p2,
                       "protectors": {"pins": state2["pins"], "consumers": state2["consumers"]}})
        dump("physical_stage2.json", state2)
        matrices.append(read_matrix(db, "stage2", state2["floor"]))
        size_log.append(file_sizes("stage2_floor_4"))
        snapshots.append(snapshot(db, "stage2"))

        # ---- 阶段 3：消费者追完 r5..r7 并确认到 head，最后一道保护释放 ----
        feed_log.append({"stage": "stage3_pre_ack", "floor": state2["floor"],
                         "consumer_reads_tail": [c["revision"] for c in db.feed.read(4, limit=100)]})
        db.feed.acknowledge("mirror_feed", head_after_build)
        p3 = db.retention.compact(head_after_build)
        db.checkpoint()
        state3 = physical_state()
        stages.append({"stage": "stage3_all_protection_released", "compact_result": p3,
                       "protectors": {"pins": state3["pins"], "consumers": state3["consumers"]}})
        dump("physical_stage3.json", state3)
        matrices.append(read_matrix(db, "stage3", state3["floor"]))
        feed_log.append({"stage": "stage3", "floor": state3["floor"],
                         "probes": [feed_probe(db, s) for s in (2, 4, 7)]})
        size_before_r8 = file_sizes("stage3_floor_7")
        size_log.append(size_before_r8)
        snapshots.append(snapshot(db, "stage3"))

        # ---- 回收后再写入：观察空闲页复用与文件字节数 ----
        r8 = db.batch("post_compaction_add_e_r8").put(
            COLLECTION, "notice_e", {"status": "draft", "priority": 1, "owner": "zhao"}, expected=0).commit()
        db.checkpoint()
        size_after_r8 = file_sizes("stage3_after_new_write_r8")
        size_log.append(size_after_r8)
        timeline.append({"commit": r8.revision, "request_key": "post_compaction_add_e_r8",
                         "kind": "write_after_compaction", "operations": r8.count})

        # ---- 提交载荷已被删除，但幂等回执仍可回放 ----
        def exact_replay(request_key):
            batch = db.batch(request_key)
            if request_key == "seed_notices_r1":
                batch.put(COLLECTION, "notice_a", {"status": "draft", "priority": 1, "owner": "lin"}, expected=0)
                batch.put(COLLECTION, "notice_b", {"status": "draft", "priority": 1, "owner": "chen"}, expected=0)
                batch.put(COLLECTION, "notice_d", {"status": "draft", "priority": 2, "owner": "he"},
                          expected=0, expires_at=500)
            elif request_key == "revise_a_r2":
                batch.put(COLLECTION, "notice_a", {"status": "published", "priority": 1, "owner": "lin"}, expected=1)
            elif request_key == "remove_b_r3":
                batch.delete(COLLECTION, "notice_b", expected=1)
            elif request_key == "add_c_r4":
                batch.put(COLLECTION, "notice_c", {"status": "draft", "priority": 3, "owner": "wu"},
                          expected=0, expires_at=1000)
            elif request_key == "archive_a_r5":
                batch.put(COLLECTION, "notice_a", {"status": "archived", "priority": 1, "owner": "lin"}, expected=2)
            elif request_key == "renew_c_r7":
                batch.put(COLLECTION, "notice_c", {"status": "published", "priority": 2, "owner": "wu"},
                          expected=4, expires_at=2000)
            result = batch.commit()
            return {"request_key": request_key, "replayed": result.replayed,
                    "revision": result.revision, "count": result.count}

        replay_order = ["seed_notices_r1", "revise_a_r2", "remove_b_r3",
                        "add_c_r4", "archive_a_r5", "renew_c_r7"]
        replays = [exact_replay(key) for key in replay_order]
        # 同键不同操作仍被回执哈希拒绝
        try:
            db.batch("revise_a_r2").put(
                COLLECTION, "notice_a", {"status": "tampered", "priority": 9, "owner": "x"}).commit()
            tamper = {"outcome": "unexpected_success"}
        except Exception as exc:  # noqa: BLE001 - 证据需要记录任意失败类型
            tamper = {"outcome": "rejected", "error": type(exc).__name__, "detail": str(exc)}

        inspections = [db.diagnostics.inspect(deep=True)]

    dump("timeline.json", timeline)
    dump("compaction_stages.json", stages)
    dump("read_matrix.json", matrices)
    dump("feed_and_replay.json", {"feed": feed_log, "replays_after_compaction": replays,
                                  "same_key_different_payload": tamper})
    probe = space_probe()
    dump("file_sizes.json", {"main_db_measurements": size_log, "snapshots": snapshots,
                             "space_probe": probe})
    dump("inspection.json", inspections)

    env = {"generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
           "platform": platform.platform(), "strataleaf_db": str(DB_PATH.relative_to(REPO_ROOT))}
    dump("environment.json", env)

    write_report(env, timeline, stages, matrices, feed_log, replays, tamper,
                 size_log, snapshots, probe, state0, state1, state2, state3)
    print(f"样本材料已生成：{SAMPLE_DIR}")
    print(f"报告：{SAMPLE_DIR / 'report.md'}")


def compact_summary_line(stage):
    r = stage["compact_result"]
    return (f"r={r['requested']} floor={r['floor']} 删版本={r['removed_versions']} "
            f"删提交={r['removed_commits']} blocked={str(r['blocked']).lower()}")


def write_report(env, timeline, stages, matrices, feed_log, replays, tamper,
                 size_log, snapshots, probe, state0, state1, state2, state3):
    states = [state0, state1, state2, state3]
    lines = []
    add = lines.append

    add("# 保留历史的实际代价——StrataLeaf 小规模样本材料")
    add("")
    add("> 面向非开发同事的离线说明材料。本材料不引入任何新产品功能，")
    add("> 所有数字都由 `build_sample.py` 在本机真实运行 StrataLeaf 后采集，")
    add(f"> 生成时间（UTC）：{env['generated_at_utc']}，Python {env['python']}，SQLite {env['sqlite']}。")
    add("")
    add("## 1. 这份材料能说明什么、不能说明什么")
    add("")
    add("- **能说明**：在这个具体样本里，释放历史保护前后，哪些旧修订还能读、哪些变更提交还能重放、")
    add("  物理上实际保留了哪些版本、数据库文件字节数如何变化。")
    add("- **不能说明**：任何吞吐量、延迟、压缩率或容量规划结论。样本只有 1 个集合、4 个业务文档、")
    add("  7 个业务提交，文档均为几十字节的小 JSON，且无并发负载。数字只证明本库语义，**不得外推为通用性能指标**。")
    add("- 复核方式：在仓库根目录运行 `python3 -I retention-cost-sample/build_sample.py`，无需联网，")
    add("  约一秒内重新生成全部证据与本报告；`evidence/snapshots/` 下是四个阶段的真实数据库快照，可用 SQLite 直接打开。")
    add("")
    add("## 2. 背景：三个关键事实（非开发版解释）")
    add("")
    add("1. **每次修改都是一条不可变修订**，删除也会留下一条「墓碑」修订，而不是把旧数据从磁盘抹掉。")
    add("2. **回收水位 floor**：系统保证 floor 及以后的读视图完整；早于 floor 的修订号会被明确拒绝读取。")
    add("   floor 能推进到哪里，由两类保护者的最靠前位置决定：")
    add("   - **读保留点 pin**：某个长读任务钉住的修订号；")
    add("   - **变更流消费者确认位置 ack**：下游同步程序声明「我已经消费到这里」。")
    add("3. **到期（TTL）不是定时自动生效**：只有显式调用 `sweep(时间点)` 才会产生删除提交；")
    add("   墙钟时间走过本身不改变任何读取结果。")
    add("")
    add("## 3. 样本时间线（改写、删除、到期各有代表）")
    add("")
    add("| 提交 | 请求键（幂等身份） | 动作 | 说明 |")
    add("|---:|---|---|---|")
    kind_cn = {"atomic_seed": "原子批量建 3 个文档", "rewrite": "改写", "explicit_delete": "显式删除（墓碑）",
               "insert_with_ttl": "写入并带到期时间", "rewrite_renews_ttl": "改写并续期到期时间",
               "write_after_compaction": "回收之后的新写入"}
    for item in timeline:
        if "commit" in item:
            add(f"| r{item['commit']} | `{item['request_key']}` | {kind_cn.get(item['kind'], item['kind'])} | "
                f"批次内 {item['operations']} 个操作 |")
        elif item["step"] == "sweep_500":
            add(f"| r{item['result']['revision']} | （系统 sweep 批次，无请求键） | 到期删除 | "
                f"sweep(500) 到期 {item['result']['expired']} 个：notice_d |")
        elif item["step"] == "sweep_1000_after_renewal":
            add(f"| — | — | 到期扫描为空 | sweep(1000) 到期 {item['result']['expired']} 个，"
                "notice_c 已在 r7 续期到 2000，旧到期时间不会误删新值 |")
        elif item["step"] == "protectors_installed":
            add(f"| — | — | 安装保护者 | pin `{item['pin']['name']}` 钉在 r{item['pin']['revision']}；"
                f"消费者 `{item['consumer']['name']}` 确认位置 r{item['consumer']['ack']} |")
    add("")
    add("文档生命周期：")
    add("")
    add("- **notice_a**：r1 草稿 → r2 发布 → r5 归档（两次改写，三个修订）。")
    add("- **notice_b**：r1 创建 → r3 显式删除（墓碑仍在版本链上）。")
    add("- **notice_c**：r4 写入（到期时间 1000）→ r7 改写续期（到期时间 2000）。")
    add("- **notice_d**：r1 写入时带到期时间 500 → r6 被 sweep 到期删除；删除前其历史修订一直可读。")
    add("")
    add("## 4. 三个回收阶段：保护释放与水位推进一一对应")
    add("")
    add("每次都请求把水位回收到当前 head（r7），实际水位由保护者最靠前位置截断：")
    add("")
    add("| 阶段 | 当时保护者 | compact 实际结果 | 物理版本行数 | commits 行数 | receipts 行数 |")
    add("|---|---|---|---:|---:|---:|")
    stage_names = ["stage0 回收前", "stage1 pin+消费者", "stage2 仅消费者", "stage3 保护全释放"]
    for index, (name, state) in enumerate(zip(stage_names, states)):
        if index == 0:
            detail = "尚未回收，floor=0"
        else:
            detail = compact_summary_line(stages[index - 1])
        protectors = [f"{p['name']}=r{p['rev']}" for p in state["pins"]]
        protectors += [f"{c['name']}=r{c['ack']}" for c in state["consumers"]]
        add(f"| {name} | {'; '.join(protectors) or '无'} "
            f"| {detail} | {len(state['versions'])} | {len(state['commits'])} | {len(state['receipts'])} |")
    add("")
    for stage in stages:
        r = stage["compact_result"]
        add(f"- **{stage['stage']}**：请求目标 r{r['requested']}，实际 floor={r['floor']}，"
            f"删除物理版本 {r['removed_versions']} 行、提交载荷 {r['removed_commits']} 行，"
            f"blocked={str(r['blocked']).lower()}（true 表示水位被保护者截停，未达到请求目标）。")
    add("")
    add("对应关系：stage1 由 pin(r2) 截停 → 释放 pin 后 stage2 被消费者 ack(r4) 截停 → "
        "消费者读完尾部并确认到 r7 后，stage3 才推进到 r7。**每一步释放的是什么保护，水位就推进到哪里，可逐行核对。**")
    add("")
    add("## 5. 各阶段仍可读取的修订（实测矩阵）")
    add("")
    add("单元格含义：`rN` = 该读版本下实际可见的文档修订；`墓碑` = 读得到删除事实、当前值被隐藏；")
    add("`拒绝` = 修订号低于 floor，抛出 SnapshotTooOld。完整 JSON 见 `evidence/queries/read_matrix.json`。")
    add("")
    for matrix in matrices:
        add(f"### {matrix['stage']}（head=r{matrix['head']}，floor=r{matrix['floor']}）")
        add("")
        add("| 文档 | r1 | r2 | r3 | r4 | r5 | r6 | r7 | 当前 head 读 |")
        add("|---|---|---|---|---|---|---|---|---|")
        for key in KEYS:
            cells = [key]
            for revision in PROBE_REVISIONS + ["head"]:
                cell = matrix["matrix"][key][str(revision)]
                if cell["outcome"] == "readable":
                    cells.append(f"r{cell['visible_revision']}")
                elif cell["outcome"] == "tombstone_hidden":
                    cells.append("墓碑")
                elif cell["outcome"] == "rejected_below_floor":
                    cells.append("拒绝")
                else:
                    cells.append("—")
            add("| " + " | ".join(cells) + " |")
        add("")
    add("几个可以直接指给同事看的事实：")
    add("")
    add("- r1 的 notice_a 草稿在 stage1（floor=r2）之后**永久不可读**，即使它曾是 pin 之前的真实数据。")
    add("- notice_b 的 r1 原始值在 stage2 被删除，但 r3 墓碑作为 floor 读视图的基准**必须保留**，")
    add("  否则系统无法解释「这个键当前为什么查不到」。")
    add("- notice_d 被到期删除后，r1 历史值在 stage1、stage2 仍可按旧修订号读取；直到 stage3 才只剩 r6 墓碑。")
    add("- 任何阶段，当前 head 的读取结果始终一致：a=归档、b=已删、c=已发布续期、d=已删。回收只动历史，不动当前视图。")
    add("")
    add("## 6. 可重放的提交与幂等回执")
    add("")
    add("变更流提交（commits 表）与幂等回执（receipts 表）是两类不同事实，回收时命运不同：")
    add("")
    add("| 事实 | 回收前 stage0 | stage1 floor=2 | stage2 floor=4 | stage3 floor=7 |")
    add("|---|---:|---:|---:|---:|")
    for rev in PROBE_REVISIONS:
        row = [f"r{rev} 提交载荷"]
        for state in states:
            row.append("保留" if any(c["rev"] == rev for c in state["commits"]) else "已删除")
        add("| " + " | ".join(row) + " |")
    add("| 幂等回执（6 条请求键） | 6 | 6 | 6 | 6 |")
    add("")
    add("变更流实测（`evidence/queries/feed_and_replay.json`）：")
    add("")
    for entry in feed_log:
        for attempt in entry.get("probes", []):
            if attempt["outcome"] == "readable":
                add(f"- {entry['stage']}：`feed.read(since=r{attempt['since']})` 可读，"
                    f"返回提交 {attempt['commit_revisions'] or '空列表'}。")
            else:
                add(f"- {entry['stage']}：`feed.read(since=r{attempt['since']})` 被拒绝（{attempt['detail']}）。")
        if "consumer_reads_tail" in entry:
            add(f"- {entry['stage']}：消费者在确认前读到尾部提交 {entry['consumer_reads_tail']}，随后才把 ack 推进到 r7。")
    add("")
    add("stage3 之后，commits 表已清空，但用原请求键重放六个写请求，**全部返回 replayed=true、原修订号与原操作数**，")
    add("系统并没有重新执行写入（notice_e 之外当前视图不变）：")
    add("")
    add("| 请求键 | 回放结果 |")
    add("|---|---|")
    for replay in replays:
        add(f"| `{replay['request_key']}` | replayed={str(replay['replayed']).lower()}，"
            f"返回 r{replay['revision']}，count={replay['count']} |")
    add("")
    add(f"同一请求键换上不同操作则被拒绝：{tamper['error']}——{tamper['detail']}。")
    add("回执保存的是请求摘要与结果，**不是提交内容本身**，所以它防重复提交，但不能帮你恢复历史读视图。")
    add("")
    add("## 7. 物理上实际保留的版本清单")
    add("")
    add("回收规则保留 floor 读视图所需的每个键基准版本，因此墓碑和「最后一个旧版本」可能比 floor 更老却仍然留库：")
    add("")
    for index, state in enumerate(states):
        add(f"### {stage_names[index]}（floor=r{state['floor']}，共 {len(state['versions'])} 行）")
        add("")
        add("| 集合 | 键 | 修订 | 类型 | expires |")
        add("|---|---|---:|---|---|")
        for row in state["versions"]:
            kind = "墓碑" if row["is_tombstone"] else "文档体"
            expires = "—" if row["expires"] is None else str(row["expires"])
            add(f"| {row['collection']} | {row['key']} | r{row['rev']} | {kind} | {expires} |")
        add("")
    add("stage3 最终只剩 4 行：notice_a/r5、notice_c/r7 两个当前文档，加 notice_b/r3、notice_d/r6 两个墓碑基准。")
    add("9 个物理版本行中 5 行被删除；7 条提交载荷全部删除；6 条幂等回执全部保留。")
    add("")
    add("## 8. 到期（TTL）专门记录")
    add("")
    for item in timeline:
        if item.get("step") == "sweep_500":
            add("- sweep 之前，notice_d 虽带 expires_at=500，当前读取仍返回文档体："
                f"`{json.dumps(item['notice_d_current_read_before_sweep'], ensure_ascii=False)}`。")
            add(f"- 显式 `sweep(now=500)` 才产生 r6 删除提交（到期 {item['result']['expired']} 个）；")
            add(f"  删除后按 r5 旧修订号仍读到历史值 `{json.dumps(item['notice_d_history_after_sweep'], ensure_ascii=False)}`，")
            add("  当前读则返回墓碑隐藏。")
        if item.get("step") == "sweep_1000_after_renewal":
            add("- notice_c 在 r4 的到期时间是 1000，但 r7 已把它改写并续期到 2000；"
                f"`sweep(now=1000)` 到期 {item['result']['expired']} 个，**已更新的值不会被旧到期时间静默误删**。")
    add("- 含义：TTL 是一种由调用方时钟驱动的显式删除机制，不是后台定时器；不调用 sweep，时间走过也不改变任何读结果。")
    add("")
    add("## 9. 文件大小：删除不等于缩小（如实记录）")
    add("")
    add("### 9.1 主样本库逐点测量")
    add("")
    add("主库文件每次测量前都执行了 `wal_checkpoint(PASSIVE)`，同时记录 WAL/SHM 旁侧文件与 SQLite 页统计：")
    add("")
    add("| 测量点 | 主 .db 字节 | -wal 字节 | -shm 字节 | 页数 | 空闲页 | 页大小 |")
    add("|---|---:|---:|---:|---:|---:|---:|")
    for measure in size_log:
        b = measure["bytes"]
        p = measure["pragmas"]
        add(f"| {measure['stage']} | {b[DB_PATH.name]} | {b[DB_PATH.name + '-wal']} | "
            f"{b[DB_PATH.name + '-shm']} | {p['page_count']} | {p['freelist_count']} | {p['page_size']} |")
    add("")
    s3 = next(m for m in size_log if m["stage"] == "stage3_floor_7")
    r8m = next(m for m in size_log if m["stage"] == "stage3_after_new_write_r8")
    add("- 三次回收共删除 5 个版本行与 7 条提交载荷，但**主 .db 与 -wal 的字节数在五个测量点完全相同**，")
    add("  页数恒定 21 页、空闲页始终为 0。这是因为数据量太小，被删行与存活行共占同一批页，")
    add("  删除只在页内腾出可复用空间，没有整页被释放——「文件大小不变」在此是实测事实，而非估算。")
    add(f"- 回收后写入 r8（notice_e）同样不改变主文件字节数（{s3['bytes'][DB_PATH.name]} → "
        f"{r8m['bytes'][DB_PATH.name]} 字节），新写直接复用已有页内空间。")
    add("- PASSIVE checkpoint 只合并 WAL、不承诺截断，因此 -wal 字节数也保持不变；这与产品文档对 checkpoint 的说明一致。")
    add("- 本库不提供 VACUUM 类收缩接口，样本中未执行任何收缩操作。")
    add("")
    add("### 9.2 独立空间探针：把「空闲页复用」放大到可见")
    add("")
    add(f"为了避免小数据量掩盖机制，另建独立探针库 `{probe['db_file']}`，写入 {probe['rows']} 行后整体删除并回收，")
    add("再写入同规模的新行。它与主样本库完全分离，数字只解释 SQLite 空间机制，不代表业务容量：")
    add("")
    add("| 探针测量点 | 主文件字节 | -wal 字节 | 页数 | 空闲页 | 页大小 | auto_vacuum |")
    add("|---|---:|---:|---:|---:|---:|---:|")
    for observation in probe["observations"]:
        add(f"| {observation['stage']} | {observation['main_bytes']} | {observation['wal_bytes']} | "
            f"{observation['page_count']} | {observation['freelist_count']} | {observation['page_size']} | "
            f"{observation['auto_vacuum']} |")
    add("")
    seeded = next(o for o in probe["observations"] if o["stage"] == "probe_after_seed_1500")
    deleted = next(o for o in probe["observations"] if o["stage"] == "probe_after_delete_and_compact")
    reseeded = next(o for o in probe["observations"] if o["stage"] == "probe_after_reseed_1500")
    add(f"- 删除 1500 行并回收（compact 报告 removed_versions={probe['compact_result']['removed_versions']}）后，")
    add(f"  空闲页从 {seeded['freelist_count']} 升至 {deleted['freelist_count']} 页；删除动作本身还写入了墓碑，")
    add(f"  所以主文件反而从 {seeded['main_bytes']} 增长到 {deleted['main_bytes']} 字节——增长来自墓碑写入，不是回收失败。")
    add(f"- 再写入 1500 个新文档：主文件字节数保持 {deleted['main_bytes']} 不变，空闲页回落到 "
        f"{reseeded['freelist_count']} 页，证明释放出的页被新写入复用，而非把文件截断还给操作系统。")
    add(f"- auto_vacuum={seeded['auto_vacuum']}（NONE）：默认模式下空间以空闲页形式留在文件内，这是文件不缩小的根本原因。")
    add("")
    add("### 9.3 阶段快照（在线备份 API 产物）")
    add("")
    add("另有四个通过在线备份 API 生成的阶段快照（内容一致、独立成文件，可离线打开）：")
    add("")
    add("| 快照 | head | floor | 字节 | SHA-256（前 16 位） |")
    add("|---|---:|---:|---:|---|")
    for snap in snapshots:
        add(f"| `{snap['file']}` | r{snap['head']} | r{snap['floor']} | {snap['bytes']} | `{snap['sha256'][:16]}…` |")
    add("")
    add("快照是备份 API 新写出的文件，与活动主库的字节数口径不同（无 WAL、无空闲页包袱），")
    add("两组数字不可混用比较；完整哈希见 `evidence/queries/file_sizes.json`。")
    add("")
    add("## 10. 释放保护意味着放弃哪些能力")
    add("")
    add("结合本样本，stage3 保护全部释放后，确定放弃的能力是：")
    add("")
    add("1. **历史修订读取**：r1–r6 的任何按修订号读取都被 SnapshotTooOld 拒绝，旧草稿、删除前原文无法从本库找回。")
    add("2. **变更流重放**：commits 载荷清空，新注册的消费者只能从 floor=r7 之后开始，无法补读 r1–r7 的变更过程。")
    add("3. **基于历史版本的分页/聚合**：这些读路径同样受 floor 约束，低于水位的读视图不存在。")
    add("")
    add("仍然保留的能力（避免误以为回收等于删库）：")
    add("")
    add("1. **完整当前视图**：head 读取、查询、索引投影与回收前完全一致，墓碑继续正确隐藏已删文档。")
    add("2. **幂等防重放**：6 条请求回执不受历史回收影响，重试安全、异键同键篡改仍被拒绝。")
    add("3. **备份与恢复事实**：四个快照各自保留当时的 head/floor 与全部数据，可独立校验恢复。")
    add("4. **完整性检查**：回收后 `diagnostics.inspect(deep=True)` 仍为 ok，提交区间连续、外键与索引投影一致。")
    add("")
    add("## 11. 证据清单与复核入口")
    add("")
    add("- `build_sample.py`：本材料的唯一生成脚本，只调用 StrataLeaf 公开 API（另用只读 SQLite 连接盘点物理表）。")
    add("- `evidence/queries/`：时间线、四阶段物理表、读取矩阵、变更流与回放、文件大小与空间探针、环境信息、完整性检查。")
    add("- `evidence/snapshots/`：stage0–stage3 四个真实数据库快照，可用 `sqlite3` 离线打开核对。")
    add("- `evidence/live/sample.db`：复跑结束时的主样本活动库（含 r8 写入）。")
    add("- `evidence/live/space_probe.db`：9.2 节空间探针的独立活动库，可离线核对空闲页统计。")
    add("")
    add("## 12. 数字使用边界（再次提示）")
    add("")
    add("本材料中的字节数、行数、耗时感均来自上述微型数据集与单机环境，**仅用于解释保留/回收语义**。")
    add("不得引用本样本数字论证生产环境的存储开销、回收效率或性能表现；那些结论需要基于真实数据规模另行测量。")
    add("")

    report_path = SAMPLE_DIR / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
