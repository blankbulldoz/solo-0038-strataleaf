# 保留历史的实际代价——StrataLeaf 小规模样本材料

> 面向非开发同事的离线说明材料。本材料不引入任何新产品功能，
> 所有数字都由 `build_sample.py` 在本机真实运行 StrataLeaf 后采集，
> 生成时间（UTC）：2026-11-20T10:03:43+00:00，Python 3.11.2，SQLite 3.40.1。

## 1. 这份材料能说明什么、不能说明什么

- **能说明**：在这个具体样本里，释放历史保护前后，哪些旧修订还能读、哪些变更提交还能重放、
  物理上实际保留了哪些版本、数据库文件字节数如何变化。
- **不能说明**：任何吞吐量、延迟、压缩率或容量规划结论。样本只有 1 个集合、4 个业务文档、
  7 个业务提交，文档均为几十字节的小 JSON，且无并发负载。数字只证明本库语义，**不得外推为通用性能指标**。
- 复核方式：在仓库根目录运行 `python3 -I retention-cost-sample/build_sample.py`，无需联网，
  约一秒内重新生成全部证据与本报告；`evidence/snapshots/` 下是四个阶段的真实数据库快照，可用 SQLite 直接打开。

## 2. 背景：三个关键事实（非开发版解释）

1. **每次修改都是一条不可变修订**，删除也会留下一条「墓碑」修订，而不是把旧数据从磁盘抹掉。
2. **回收水位 floor**：系统保证 floor 及以后的读视图完整；早于 floor 的修订号会被明确拒绝读取。
   floor 能推进到哪里，由两类保护者的最靠前位置决定：
   - **读保留点 pin**：某个长读任务钉住的修订号；
   - **变更流消费者确认位置 ack**：下游同步程序声明「我已经消费到这里」。
3. **到期（TTL）不是定时自动生效**：只有显式调用 `sweep(时间点)` 才会产生删除提交；
   墙钟时间走过本身不改变任何读取结果。

## 3. 样本时间线（改写、删除、到期各有代表）

| 提交 | 请求键（幂等身份） | 动作 | 说明 |
|---:|---|---|---|
| r1 | `seed_notices_r1` | 原子批量建 3 个文档 | 批次内 3 个操作 |
| r2 | `revise_a_r2` | 改写 | 批次内 1 个操作 |
| r3 | `remove_b_r3` | 显式删除（墓碑） | 批次内 1 个操作 |
| r4 | `add_c_r4` | 写入并带到期时间 | 批次内 1 个操作 |
| — | — | 安装保护者 | pin `audit_pin` 钉在 r2；消费者 `mirror_feed` 确认位置 r4 |
| r5 | `archive_a_r5` | 改写 | 批次内 1 个操作 |
| r6 | （系统 sweep 批次，无请求键） | 到期删除 | sweep(500) 到期 1 个：notice_d |
| r7 | `renew_c_r7` | 改写并续期到期时间 | 批次内 1 个操作 |
| — | — | 到期扫描为空 | sweep(1000) 到期 0 个，notice_c 已在 r7 续期到 2000，旧到期时间不会误删新值 |
| r8 | `post_compaction_add_e_r8` | 回收之后的新写入 | 批次内 1 个操作 |

文档生命周期：

- **notice_a**：r1 草稿 → r2 发布 → r5 归档（两次改写，三个修订）。
- **notice_b**：r1 创建 → r3 显式删除（墓碑仍在版本链上）。
- **notice_c**：r4 写入（到期时间 1000）→ r7 改写续期（到期时间 2000）。
- **notice_d**：r1 写入时带到期时间 500 → r6 被 sweep 到期删除；删除前其历史修订一直可读。

## 4. 三个回收阶段：保护释放与水位推进一一对应

每次都请求把水位回收到当前 head（r7），实际水位由保护者最靠前位置截断：

| 阶段 | 当时保护者 | compact 实际结果 | 物理版本行数 | commits 行数 | receipts 行数 |
|---|---|---|---:|---:|---:|
| stage0 回收前 | audit_pin=r2; mirror_feed=r4 | 尚未回收，floor=0 | 9 | 7 | 6 |
| stage1 pin+消费者 | audit_pin=r2; mirror_feed=r4 | r=7 floor=2 删版本=1 删提交=2 blocked=true | 8 | 5 | 6 |
| stage2 仅消费者 | mirror_feed=r4 | r=7 floor=4 删版本=1 删提交=2 blocked=true | 7 | 3 | 6 |
| stage3 保护全释放 | mirror_feed=r7 | r=7 floor=7 删版本=3 删提交=3 blocked=false | 4 | 0 | 6 |

- **stage1_pin_and_consumer_hold**：请求目标 r7，实际 floor=2，删除物理版本 1 行、提交载荷 2 行，blocked=true（true 表示水位被保护者截停，未达到请求目标）。
- **stage2_only_consumer_holds**：请求目标 r7，实际 floor=4，删除物理版本 1 行、提交载荷 2 行，blocked=true（true 表示水位被保护者截停，未达到请求目标）。
- **stage3_all_protection_released**：请求目标 r7，实际 floor=7，删除物理版本 3 行、提交载荷 3 行，blocked=false（true 表示水位被保护者截停，未达到请求目标）。

对应关系：stage1 由 pin(r2) 截停 → 释放 pin 后 stage2 被消费者 ack(r4) 截停 → 消费者读完尾部并确认到 r7 后，stage3 才推进到 r7。**每一步释放的是什么保护，水位就推进到哪里，可逐行核对。**

## 5. 各阶段仍可读取的修订（实测矩阵）

单元格含义：`rN` = 该读版本下实际可见的文档修订；`墓碑` = 读得到删除事实、当前值被隐藏；
`拒绝` = 修订号低于 floor，抛出 SnapshotTooOld。完整 JSON 见 `evidence/queries/read_matrix.json`。

### stage0（head=r7，floor=r0）

| 文档 | r1 | r2 | r3 | r4 | r5 | r6 | r7 | 当前 head 读 |
|---|---|---|---|---|---|---|---|---|
| notice_a | r1 | r2 | r2 | r2 | r5 | r5 | r5 | r5 |
| notice_b | r1 | r1 | 墓碑 | 墓碑 | 墓碑 | 墓碑 | 墓碑 | 墓碑 |
| notice_c | 墓碑 | 墓碑 | 墓碑 | r4 | r4 | r4 | r7 | r7 |
| notice_d | r1 | r1 | r1 | r1 | r1 | 墓碑 | 墓碑 | 墓碑 |

### stage1（head=r7，floor=r2）

| 文档 | r1 | r2 | r3 | r4 | r5 | r6 | r7 | 当前 head 读 |
|---|---|---|---|---|---|---|---|---|
| notice_a | 拒绝 | r2 | r2 | r2 | r5 | r5 | r5 | r5 |
| notice_b | 拒绝 | r1 | 墓碑 | 墓碑 | 墓碑 | 墓碑 | 墓碑 | 墓碑 |
| notice_c | 拒绝 | 墓碑 | 墓碑 | r4 | r4 | r4 | r7 | r7 |
| notice_d | 拒绝 | r1 | r1 | r1 | r1 | 墓碑 | 墓碑 | 墓碑 |

### stage2（head=r7，floor=r4）

| 文档 | r1 | r2 | r3 | r4 | r5 | r6 | r7 | 当前 head 读 |
|---|---|---|---|---|---|---|---|---|
| notice_a | 拒绝 | 拒绝 | 拒绝 | r2 | r5 | r5 | r5 | r5 |
| notice_b | 拒绝 | 拒绝 | 拒绝 | 墓碑 | 墓碑 | 墓碑 | 墓碑 | 墓碑 |
| notice_c | 拒绝 | 拒绝 | 拒绝 | r4 | r4 | r4 | r7 | r7 |
| notice_d | 拒绝 | 拒绝 | 拒绝 | r1 | r1 | 墓碑 | 墓碑 | 墓碑 |

### stage3（head=r7，floor=r7）

| 文档 | r1 | r2 | r3 | r4 | r5 | r6 | r7 | 当前 head 读 |
|---|---|---|---|---|---|---|---|---|
| notice_a | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | r5 | r5 |
| notice_b | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 墓碑 | 墓碑 |
| notice_c | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | r7 | r7 |
| notice_d | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 墓碑 | 墓碑 |

几个可以直接指给同事看的事实：

- r1 的 notice_a 草稿在 stage1（floor=r2）之后**永久不可读**，即使它曾是 pin 之前的真实数据。
- notice_b 的 r1 原始值在 stage2 被删除，但 r3 墓碑作为 floor 读视图的基准**必须保留**，
  否则系统无法解释「这个键当前为什么查不到」。
- notice_d 被到期删除后，r1 历史值在 stage1、stage2 仍可按旧修订号读取；直到 stage3 才只剩 r6 墓碑。
- 任何阶段，当前 head 的读取结果始终一致：a=归档、b=已删、c=已发布续期、d=已删。回收只动历史，不动当前视图。

## 6. 可重放的提交与幂等回执

变更流提交（commits 表）与幂等回执（receipts 表）是两类不同事实，回收时命运不同：

| 事实 | 回收前 stage0 | stage1 floor=2 | stage2 floor=4 | stage3 floor=7 |
|---|---:|---:|---:|---:|
| r1 提交载荷 | 保留 | 已删除 | 已删除 | 已删除 |
| r2 提交载荷 | 保留 | 已删除 | 已删除 | 已删除 |
| r3 提交载荷 | 保留 | 保留 | 已删除 | 已删除 |
| r4 提交载荷 | 保留 | 保留 | 已删除 | 已删除 |
| r5 提交载荷 | 保留 | 保留 | 保留 | 已删除 |
| r6 提交载荷 | 保留 | 保留 | 保留 | 已删除 |
| r7 提交载荷 | 保留 | 保留 | 保留 | 已删除 |
| 幂等回执（6 条请求键） | 6 | 6 | 6 | 6 |

变更流实测（`evidence/queries/feed_and_replay.json`）：

- stage1：`feed.read(since=r0)` 被拒绝（revision 0 is older than retained floor 2）。
- stage1：`feed.read(since=r1)` 被拒绝（revision 1 is older than retained floor 2）。
- stage1：`feed.read(since=r2)` 可读，返回提交 [3, 4, 5, 6, 7]。
- stage1：`feed.read(since=r4)` 可读，返回提交 [5, 6, 7]。
- stage1：`feed.read(since=r7)` 可读，返回提交 空列表。
- stage3_pre_ack：消费者在确认前读到尾部提交 [5, 6, 7]，随后才把 ack 推进到 r7。
- stage3：`feed.read(since=r2)` 被拒绝（revision 2 is older than retained floor 7）。
- stage3：`feed.read(since=r4)` 被拒绝（revision 4 is older than retained floor 7）。
- stage3：`feed.read(since=r7)` 可读，返回提交 空列表。

stage3 之后，commits 表已清空，但用原请求键重放六个写请求，**全部返回 replayed=true、原修订号与原操作数**，
系统并没有重新执行写入（notice_e 之外当前视图不变）：

| 请求键 | 回放结果 |
|---|---|
| `seed_notices_r1` | replayed=true，返回 r1，count=3 |
| `revise_a_r2` | replayed=true，返回 r2，count=1 |
| `remove_b_r3` | replayed=true，返回 r3，count=1 |
| `add_c_r4` | replayed=true，返回 r4，count=1 |
| `archive_a_r5` | replayed=true，返回 r5，count=1 |
| `renew_c_r7` | replayed=true，返回 r7，count=1 |

同一请求键换上不同操作则被拒绝：Conflict——request key was used with different operations。
回执保存的是请求摘要与结果，**不是提交内容本身**，所以它防重复提交，但不能帮你恢复历史读视图。

## 7. 物理上实际保留的版本清单

回收规则保留 floor 读视图所需的每个键基准版本，因此墓碑和「最后一个旧版本」可能比 floor 更老却仍然留库：

### stage0 回收前（floor=r0，共 9 行）

| 集合 | 键 | 修订 | 类型 | expires |
|---|---|---:|---|---|
| notices | notice_a | r1 | 文档体 | — |
| notices | notice_a | r2 | 文档体 | — |
| notices | notice_a | r5 | 文档体 | — |
| notices | notice_b | r1 | 文档体 | — |
| notices | notice_b | r3 | 墓碑 | — |
| notices | notice_c | r4 | 文档体 | 1000.0 |
| notices | notice_c | r7 | 文档体 | 2000.0 |
| notices | notice_d | r1 | 文档体 | 500.0 |
| notices | notice_d | r6 | 墓碑 | — |

### stage1 pin+消费者（floor=r2，共 8 行）

| 集合 | 键 | 修订 | 类型 | expires |
|---|---|---:|---|---|
| notices | notice_a | r2 | 文档体 | — |
| notices | notice_a | r5 | 文档体 | — |
| notices | notice_b | r1 | 文档体 | — |
| notices | notice_b | r3 | 墓碑 | — |
| notices | notice_c | r4 | 文档体 | 1000.0 |
| notices | notice_c | r7 | 文档体 | 2000.0 |
| notices | notice_d | r1 | 文档体 | 500.0 |
| notices | notice_d | r6 | 墓碑 | — |

### stage2 仅消费者（floor=r4，共 7 行）

| 集合 | 键 | 修订 | 类型 | expires |
|---|---|---:|---|---|
| notices | notice_a | r2 | 文档体 | — |
| notices | notice_a | r5 | 文档体 | — |
| notices | notice_b | r3 | 墓碑 | — |
| notices | notice_c | r4 | 文档体 | 1000.0 |
| notices | notice_c | r7 | 文档体 | 2000.0 |
| notices | notice_d | r1 | 文档体 | 500.0 |
| notices | notice_d | r6 | 墓碑 | — |

### stage3 保护全释放（floor=r7，共 4 行）

| 集合 | 键 | 修订 | 类型 | expires |
|---|---|---:|---|---|
| notices | notice_a | r5 | 文档体 | — |
| notices | notice_b | r3 | 墓碑 | — |
| notices | notice_c | r7 | 文档体 | 2000.0 |
| notices | notice_d | r6 | 墓碑 | — |

stage3 最终只剩 4 行：notice_a/r5、notice_c/r7 两个当前文档，加 notice_b/r3、notice_d/r6 两个墓碑基准。
9 个物理版本行中 5 行被删除；7 条提交载荷全部删除；6 条幂等回执全部保留。

## 8. 到期（TTL）专门记录

- sweep 之前，notice_d 虽带 expires_at=500，当前读取仍返回文档体：`{"owner": "he", "priority": 2, "status": "draft"}`。
- 显式 `sweep(now=500)` 才产生 r6 删除提交（到期 1 个）；
  删除后按 r5 旧修订号仍读到历史值 `{"owner": "he", "priority": 2, "status": "draft"}`，
  当前读则返回墓碑隐藏。
- notice_c 在 r4 的到期时间是 1000，但 r7 已把它改写并续期到 2000；`sweep(now=1000)` 到期 0 个，**已更新的值不会被旧到期时间静默误删**。
- 含义：TTL 是一种由调用方时钟驱动的显式删除机制，不是后台定时器；不调用 sweep，时间走过也不改变任何读结果。

## 9. 文件大小：删除不等于缩小（如实记录）

### 9.1 主样本库逐点测量

主库文件每次测量前都执行了 `wal_checkpoint(PASSIVE)`，同时记录 WAL/SHM 旁侧文件与 SQLite 页统计：

| 测量点 | 主 .db 字节 | -wal 字节 | -shm 字节 | 页数 | 空闲页 | 页大小 |
|---|---:|---:|---:|---:|---:|---:|
| stage0_before_compaction | 86016 | 387312 | 32768 | 21 | 0 | 4096 |
| stage1_floor_2 | 86016 | 387312 | 32768 | 21 | 0 | 4096 |
| stage2_floor_4 | 86016 | 387312 | 32768 | 21 | 0 | 4096 |
| stage3_floor_7 | 86016 | 387312 | 32768 | 21 | 0 | 4096 |
| stage3_after_new_write_r8 | 86016 | 387312 | 32768 | 21 | 0 | 4096 |

- 三次回收共删除 5 个版本行与 7 条提交载荷，但**主 .db 与 -wal 的字节数在五个测量点完全相同**，
  页数恒定 21 页、空闲页始终为 0。这是因为数据量太小，被删行与存活行共占同一批页，
  删除只在页内腾出可复用空间，没有整页被释放——「文件大小不变」在此是实测事实，而非估算。
- 回收后写入 r8（notice_e）同样不改变主文件字节数（86016 → 86016 字节），新写直接复用已有页内空间。
- PASSIVE checkpoint 只合并 WAL、不承诺截断，因此 -wal 字节数也保持不变；这与产品文档对 checkpoint 的说明一致。
- 本库不提供 VACUUM 类收缩接口，样本中未执行任何收缩操作。

### 9.2 独立空间探针：把「空闲页复用」放大到可见

为了避免小数据量掩盖机制，另建独立探针库 `retention-cost-sample/evidence/live/space_probe.db`，写入 1500 行后整体删除并回收，
再写入同规模的新行。它与主样本库完全分离，数字只解释 SQLite 空间机制，不代表业务容量：

| 探针测量点 | 主文件字节 | -wal 字节 | 页数 | 空闲页 | 页大小 | auto_vacuum |
|---|---:|---:|---:|---:|---:|---:|
| probe_empty_after_schema | 86016 | 148352 | 21 | 0 | 4096 | 0 |
| probe_after_seed_1500 | 462848 | 416152 | 113 | 0 | 4096 | 0 |
| probe_after_delete_and_compact | 684032 | 902312 | 167 | 117 | 4096 | 0 |
| probe_after_reseed_1500 | 684032 | 902312 | 167 | 25 | 4096 | 0 |

- 删除 1500 行并回收（compact 报告 removed_versions=1500）后，
  空闲页从 0 升至 117 页；删除动作本身还写入了墓碑，
  所以主文件反而从 462848 增长到 684032 字节——增长来自墓碑写入，不是回收失败。
- 再写入 1500 个新文档：主文件字节数保持 684032 不变，空闲页回落到 25 页，证明释放出的页被新写入复用，而非把文件截断还给操作系统。
- auto_vacuum=0（NONE）：默认模式下空间以空闲页形式留在文件内，这是文件不缩小的根本原因。

### 9.3 阶段快照（在线备份 API 产物）

另有四个通过在线备份 API 生成的阶段快照（内容一致、独立成文件，可离线打开）：

| 快照 | head | floor | 字节 | SHA-256（前 16 位） |
|---|---:|---:|---:|---|
| `stage0.db` | r7 | r0 | 86016 | `1438f44537a7faa1…` |
| `stage1.db` | r7 | r2 | 86016 | `29201e65da9eeabd…` |
| `stage2.db` | r7 | r4 | 86016 | `475c689389c6eb06…` |
| `stage3.db` | r7 | r7 | 86016 | `4971cfbb151f7970…` |

快照是备份 API 新写出的文件，与活动主库的字节数口径不同（无 WAL、无空闲页包袱），
两组数字不可混用比较；完整哈希见 `evidence/queries/file_sizes.json`。

## 10. 释放保护意味着放弃哪些能力

结合本样本，stage3 保护全部释放后，确定放弃的能力是：

1. **历史修订读取**：r1–r6 的任何按修订号读取都被 SnapshotTooOld 拒绝，旧草稿、删除前原文无法从本库找回。
2. **变更流重放**：commits 载荷清空，新注册的消费者只能从 floor=r7 之后开始，无法补读 r1–r7 的变更过程。
3. **基于历史版本的分页/聚合**：这些读路径同样受 floor 约束，低于水位的读视图不存在。

仍然保留的能力（避免误以为回收等于删库）：

1. **完整当前视图**：head 读取、查询、索引投影与回收前完全一致，墓碑继续正确隐藏已删文档。
2. **幂等防重放**：6 条请求回执不受历史回收影响，重试安全、异键同键篡改仍被拒绝。
3. **备份与恢复事实**：四个快照各自保留当时的 head/floor 与全部数据，可独立校验恢复。
4. **完整性检查**：回收后 `diagnostics.inspect(deep=True)` 仍为 ok，提交区间连续、外键与索引投影一致。

## 11. 证据清单与复核入口

- `build_sample.py`：本材料的唯一生成脚本，只调用 StrataLeaf 公开 API（另用只读 SQLite 连接盘点物理表）。
- `evidence/queries/`：时间线、四阶段物理表、读取矩阵、变更流与回放、文件大小与空间探针、环境信息、完整性检查。
- `evidence/snapshots/`：stage0–stage3 四个真实数据库快照，可用 `sqlite3` 离线打开核对。
- `evidence/live/sample.db`：复跑结束时的主样本活动库（含 r8 写入）。
- `evidence/live/space_probe.db`：9.2 节空间探针的独立活动库，可离线核对空闲页统计。

## 12. 数字使用边界（再次提示）

本材料中的字节数、行数、耗时感均来自上述微型数据集与单机环境，**仅用于解释保留/回收语义**。
不得引用本样本数字论证生产环境的存储开销、回收效率或性能表现；那些结论需要基于真实数据规模另行测量。
