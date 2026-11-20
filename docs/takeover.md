# 数据库接管处置手册：空间、消费者、历史读取与恢复接收

这份文档写给第一次接手 StrataLeaf 生产文件的人。它不重复 API 清单，只回答一个具体问题：

> 现场看起来都是“库变大 / 某个读取报错 / 消费者落后 / 接收端追不上”，为什么处置方向可能相反？

四个症状共用同一组水位事实，但**不是同一条因果链**。把它们混为一谈时，最常见的错误是：为了“腾空间”推进回收，结果先丢掉了历史读取和重放能力；或者为了“保住历史”拒绝回收，结果 WAL 被长读拖住一直涨。下面用当前生产入口复现每种状态，给出**足以推进确认或释放保护的事实**、**该保留现场的情形**，以及**做错会永久丢掉哪种数据库能力**。

本文只维护文档与示例材料，不改变任何生产语义。示例位于 `examples/takeover.py`，全部走公开 API，不预埋损坏；仓库测试仍为 deferred。

---

## 0. 三条最容易混淆的“大小 / 水位 / 区间”

先立三根轴（0.1–0.3），再补两组同样必须核对的事实（0.4）；后面所有判断都挂在它们上。

### 0.1 SQLite 物理容量

一个逻辑数据库由三个文件承载：`x.db`（主文件）、`x.db-wal`（预写日志）、`x.db-shm`（共享内存索引，非持久数据）。物理事实包括：

- 各文件字节数；
- `PRAGMA page_size`、`page_count`、`freelist_count`（主文件已分配的页、其中空闲可复用的页）；
- `PRAGMA wal_checkpoint(PASSIVE)` 返回的 `(busy, log, checkpointed)`。

`Database.checkpoint()` 只是 `PASSIVE`（`strataleaf/storage.py:84` 的 `PRAGMA wal_checkpoint(PASSIVE)`），**不承诺截断 WAL**。`busy=0` 也不代表没人挡路：外部只读快照存在时，本机观测到 `(0, 411, 44)`——没有忙等，但 411 帧里只有 44 帧被回写；读快照关闭后同一份库返回 `(0, 411, 411)`。判据是 `checkpointed < log`，不是 `busy`。

物理容量与逻辑历史没有一一对应关系：WAL 可以很大而 `floor=head`，也可以很小而历史全在。关闭所有句柄时 SQLite 会移除 WAL 文件；进程活着时删 `-wal` 或只拷 `-db` 都不安全。

### 0.2 逻辑历史回收

`retention.compact(target)`（`strataleaf/retention.py:26`）计算

```
boundary = min(target, 所有 pin.rev, 所有 consumer.ack)
```

然后删除 `rev < boundary` 且同键存在更新版本（`rev <= boundary`）的版本，删除 `rev <= boundary` 的提交日志，并把 `meta.floor` 设为 `boundary`。两个非直觉结果：

1. **`blocked: true` 不等于“什么都没做”。** 保护点只把水位卡在自己的修订上；它下面的历史本来就可回收。示例 `watermarks` 里 `requested=5`、pin 在 1：返回 `{blocked: true, floor: 1, removed_commits: 1}`，`floor` 已经从 0 推进到 1，读修订 0 随即报错。每个保护点只保护“从它自身往后”，不保护它之前。
2. **回收按“每键保留 floor 之前的最后一个版本”**，所以 `floor` 不是物理最小修订。示例 `watermarks` 推进到 `floor=5` 后，读修订 5 返回的记录 revision 是 3。`docs/storage-semantics.md` 里的这句话必须当作操作事实来用。

由此还有一个必须记住的区分：**读保留点保护的是读视图，不是它自己那条提交日志**。`watermarks` 里 pin 在 1、`compact(5)`：boundary 取 1，`removed_commits=1`，提交 1 被删除；但读修订 1 仍然成功（`versions` 里每键的基准版本被保留）。同一时刻 `feed.read(0)` 与 `replication.export(0)` 都因为 `0 < floor` 被拒绝。也就是说，“还能读修订 1”不等于“还能从修订 1 之前重放或导出”：副本追赶依赖 `commits`，而 `commits` 与 `versions` 按同一个 boundary 一起裁剪，只有读视图可以一直活到 floor 越过它的那一刻。要保住重放能力，必须让 floor 停在消费者真正 ack 的位置，而不是停在某个读者的 pin 上。

### 0.3 可读修订区间

`checked_revision`（`strataleaf/mvcc.py:15`）先判 `0 <= revision <= head`，再判 `revision >= floor`：

- 超前/负数 → `InvalidDocument("read revision must be between zero and current head")`；
- 低于 floor → `SnapshotTooOld("revision N is older than retained floor F")`；
- 在 `[floor, head]` 内 → 可读，返回值可能带着更小的 `revision`。

因此“某个修订读不到”可能意味着三种完全不同的情况：**还没发生**（超前）、**已经被回收**（低于 floor）、**存在但没有值**（墓碑/从未创建/键在旧修订之后才出现）。三者不能互相证明。

### 0.4 另外两组同样重要的事实

- **保护持有者**：`pins(name, rev)` 与 `consumers(name, ack)`。公开 API 只能按名字查询或增删，没有“列出全部持有者”；接管时必须只读地看一次 `pins` / `consumers` 表，才知道是谁卡住了水位（示例 `protections()` 就这么做，纯只读）。
- **身份与来源**：`meta.database_id`、`cursor_secret`、`mode`、`upstream`、`catalog_epoch`。恢复成主库会换 `database_id` 与 `cursor_secret`（`strataleaf/archive.py:84-86`），恢复成副本则保留来源身份；副本只接受 `upstream` 相同的包（`strataleaf/replica.py:35`）。身份不同，游标、回执、副本接受策略都会变。

---

## 1. 观测能证明什么、不能证明什么

接管时先采一轮只读证据，再决定动手。建议顺序：

1. `diagnostics.inspect(deep=True)`：`head/floor`、表计数（含 `pins`、`consumers`）、保留提交区间是否连续、外键、SQLite 完整性、当前索引与可见文档是否一致。它**不报告** WAL 大小、`freelist`、消费者确认值，也不逐个校验消费者；对 pin 只有“是否落在 `[floor, head]`”这一项检查（`strataleaf/diagnostics.py:26`），不判断 pin 是否仍然有人使用。
2. `checkpoint()` 三元组 + 三个文件大小 + `page_count/freelist_count`。
3. 只读打开同一文件，列出 `pins(name, rev)`、`consumers(name, ack)`、`meta`（用 `?mode=ro`，不写）。
4. 只有需要判断“历史是否可重放”时，才去读 `commits`/`feed`/`replication.export` 的具体范围。

不要用 `fingerprint()` 替代以上任何一步：它只比较一个读视图里的键、版本、内容与到期时间，不含模式、索引定义、pin、消费者或回执。两次指纹相同，仍然可能一个库卡在水位上、另一个没有。

---

## 2. 八种“看起来相似”的状态

每张卡片给出：触发时的观察、用当前入口复现的用例、**足以推进处置的事实**、处置、处置前后的可观察结果、适用条件、做错会失去什么。数字来自本机一次运行（页大小 4096），只用于说明关系，不要当作容量承诺。

### 2.1 WAL 顶着，但逻辑历史并不该回收

**观察**：`x.db` 很小（4 KiB），`x.db-wal` 上兆，`floor=0`，没有 pin，没有消费者，`inspect` 全绿。

**复现**：`python3 -m examples.takeover wal`。示例在外部连接里 `BEGIN` 并读一次 `versions`，制造一个 SQLite 读快照，然后正常写入 60 个修订。

**判据（足以推进的事实）**：`checkpoint()` 的 `checkpointed < log`，且 `floor` 远低于 `head`、没有保护持有者；同时外部存在读快照。此时 WAL 大是**checkpoint 被旧读快照挡住**，不是历史没回收。

**处置**：找到持锁的 SQLite 读快照（长事务、未关闭的只读连接、慢备份进程），等它结束或释放；再 `checkpoint()`。不要先 `compact`。

**前后可观察**：

| 时刻 | checkpoint | `x.db` | `x.db-wal` | floor |
| --- | --- | --- | --- | --- |
| 外部快照存活 | `(0, 411, 44)` | 86016 | 1693352 | 0 |
| 快照关闭后 | `(0, 411, 411)` | 102400 | 1693352 | 0 |
| 关闭全部句柄 | 不再需要 | 102400 | 已移除 | 0 |

注意第二行：WAL 字节数并没有因为 checkpoint 成功而变小，`PASSIVE` 不截断；主文件反而长大了。真正让 WAL 文件消失的是关闭最后一个句柄。

**适用条件**：逻辑水位的持有者清单干净（无 pin、无低位 consumer），问题只在 SQLite 层。若同时有 pin/consumer，先按 2.2 处理，再回到这里。

**做错的损失**：为了“清 WAL”去 `compact`，会推进 `floor`、删掉本来还能读的历史与还能重放的提交，却**不会**让 WAL 变小；同时把 2.2 里本该保留的证据毁掉。

### 2.2 floor 被保护点压住，历史在正常累积

**观察**：`versions` 与 `commits` 计数随写入线性增长，`floor` 停在某个很早的修订；`compact(head)` 返回 `blocked: true`；旧修订读取仍然成功。

**复现**：`python3 -m examples.takeover watermarks`；`python3 -m examples.takeover entangled`。后者更接近真实：pin `reporting@1` 与 consumer `downstream@1` 同时存在。

**判据**：只读看到 `pins`/`consumers` 的具体名字和修订，且 **min(它们) 等于当前 floor**。示例 `watermarks`：pin `audit@1`，`floor=0`，`compact(5)` 返回 `floor=1`、`removed_commits=1`、`blocked=true`；此时读修订 1 仍成功、`feed.read(1)` 仍返回 2..5，而读修订 0 已经是 `SnapshotTooOld`——被保护的修订本身还在，它**之前**的已经没了。`entangled`：`pins=1, consumers=1`，`compact(41)` 只推进到 1。

**处置**：先确认这些持有者是否还需要那段历史（见第 4 节）。要么让持有者自己完成并解除，要么由维护者明确决定放弃该保护；之后重新 `compact`。**不要**把“解除保护”和“保留现场”一起做：如果事后还要复盘，先按 4.1 用 `archives.backup` 留一份带当前 `floor` 的快照。

**前后可观察**（`watermarks`）：

| 阶段 | compact 返回 | 可读区间 | feed 从 1 读 | 旧修订读取 |
| --- | --- | --- | --- | --- |
| pin@1 存在 | `blocked=true, floor=1, removed_commits=1` | `[1, 5]` | 成功（2,3,4,5） | 成功 |
| pin 解除后 | `blocked=false, floor=5, removed_versions=4` | `[5, 5]` | `SnapshotTooOld` | `SnapshotTooOld` |
| 再执行一次 | `blocked=false, removed_* = 0` | `[5, 5]` | 同上 | 同上 |

同一个目标重复执行是幂等的（0 删除），不会继续前进；只有解除保护或提高 target 才会。

**适用条件**：保护点确实属于“还需要”的读者/消费者。若保护点已经无人认领，处置不是“等”，而是走 2.5 的确认流程后释放。

**做错的损失**：在没确认持有者用途时直接 `unpin`/`release`，把对方的历史读取能力和重放能力一次性删除；`floor` 之后不能再回退，任何试图重新 pin/注册到旧修订的调用都会得到 `SnapshotTooOld`，而不是恢复。

### 2.3 历史已经回收，但物理文件没有变小

**观察**：`compact` 返回大量 `removed_versions/removed_commits`，`floor=head`，`inspect` 全绿，但 `x.db` 大小不变，`freelist_count > 0`。

**复现**：`python3 -m examples.takeover reclaim`。

**判据**：`floor` 已达目标、`versions` 只剩每键基准、`page_count` 不变而 `freelist_count` 增加。回收解决的是**逻辑可读性与日志**，不是磁盘容量；SQLite 用空闲页复用，不自动缩小主文件。注意 `x.db` 字节数在 compact 后仍可能只有 4 KiB——数据还在 WAL 里，`page_count` 才是主文件的分配事实。

**处置**：如果只是“空间看起来还占着”，**不要再做回收**。继续写入会复用空闲页（示例中再写 20 个修订后 `freelist` 从 4 降到 2，`page_count` 仍为 25）。确实需要缩小文件时，属于 SQLite 运维动作（例如关停所有句柄后 `VACUUM`），不在本库 API 承诺内，必须单独评估停机与临时磁盘翻倍；不要把它包装成“回收历史”。

**前后可观察**：

| 阶段 | versions | commits | db 字节 | page_count | freelist |
| --- | --- | --- | --- | --- | --- |
| 回收前 | 64 | 61 | 4096 | 25 | 0 |
| compact 后 | 4 | 0 | 4096 | 25 | 4 |
| checkpoint 后 | 4 | 0 | 102400 | 25 | 4 |
| 再写 20 个修订 | 24 | 20 | 102400 | 25 | 2 |
| 关闭句柄 | 24 | 20 | 102400 | 25 | 2 |

**适用条件**：确认历史确实不再需要（已有保护清单与备份），且诉求是容量而不是水位。

**做错的损失**：把 `compact` 当成“缩小文件”的按钮，反复推进 `floor`，历史读取、`feed` 重放、副本从该点追赶全部失效；容量一点没降。

### 2.4 历史读取失败，和“本来就没有”

**观察**：业务侧报“读到空”“读不到老数据”，两种说法经常混在一起。

**复现**：`python3 -m examples.takeover watermarks`（`while_history_readable` 与 `after_floor_advanced` 两段）。同一个键 `seg_03` 在墓碑之后：

- `get(现在)` → `None`；
- `get(墓碑之前的修订)`（在 floor 内）→ 返回旧文档；
- floor 推进后 `get(旧修订)` → `SnapshotTooOld`；
- 从未存在的键，在当前与仍在区间内的旧修订 → `None`；
- 在旧修订之后才创建的键，用旧修订读 → `None`；
- 集合名不存在 → `NotFound`；
- 修订超过 head → `InvalidDocument`。

**判据**：`None` 不是“丢失”的证据，它同时覆盖墓碑、从未创建、以及在所请求修订时尚未出现。要区分，必须知道该修订是否 `>= floor`，并（若日志还在）从 `feed`/`commits` 看该键在该修订附近是否有操作。错误类型是第一手证据：`SnapshotTooOld` 是回收，`InvalidDocument` 是读到了未来，`NotFound` 是集合问题。同一批证据在 `after_floor_advanced` 里会翻转：墓碑与“从未存在”都变成 `SnapshotTooOld`，因为对已回收修订再问“它当时存不存在”已经不再有可回答的历史。

**处置**：

- 若报错是 `SnapshotTooOld`：历史已按 2.2/2.3 回收，不能“修复”，只能判断是否需要从更早的备份恢复（见 2.6）。
- 若是 `None` 且修订在区间内：按墓碑/未创建处理，不要回收任何东西。
- 若是 `InvalidDocument`：调用方拿着超前修订，属于调用方问题。

**适用条件**：任何历史读取工单。**这条应当先做**，否则后面的处置会建立在错误前提上。

**做错的损失**：把 `None` 当成数据损坏去“重建”或“导入”，可能覆盖当前 head；把 `SnapshotTooOld` 当成权限/查询问题反复重试，永远等不到结果，同时让水位问题继续累积。

### 2.5 消费者落后：推进确认、释放、还是只补数据

**观察**：`feed.position(name)` 很早，`compact` 被卡住；下游说“它还能补”。

这三件事含义完全不同：

- `acknowledge(name, revision)`：声明“已成功处理到该修订”，水位可推进；可重复调用同一值，不能回退。
- `release(name)`：删除消费者，**放弃它的历史保护**；之后无法以旧位置重新注册。
- 只补数据：不改水位，继续用 `feed.read(旧位置)`，前提是 `旧位置 >= floor`。

**复现**：`python3 -m examples.takeover consumer`。

**判据（足以推进的事实）**：

- 确认前进：需要消费者自己**持久记录的处理进度**，能证明到 N 为止的完整提交已经被应用；本系统只接受声明，不替调用方证明处理成功。仅有“它读到了”不等于“它处理完了”。
- 释放：需要明确有人决定放弃该消费者，或消费者已永久下线。示例 `released_without_ack`：位置 1 的消费者被释放后，`compact` 从 `floor=1` 直接到 9，删掉 8 个版本/8 个提交；之后从旧位置读和按旧位置重新注册都返回 `SnapshotTooOld`。
- 只补数据：`feed.read(position)` 成功（位置 >= floor）。一旦 floor 越过它，只能 `SnapshotTooOld`。

**前后可观察**（`consumer` 用例）：

| 动作 | compact | position | 从旧位置读 | 重新注册旧位置 |
| --- | --- | --- | --- | --- |
| pin+consumer 都在 1 | `floor=1, blocked=true` | 1 | 成功 | — |
| 只解除 pin | `floor=1, removed_*=0, blocked=true` | 1 | 成功 | — |
| ack 到 6 | `floor=6, blocked=true` | 6 | `SnapshotTooOld`（1<6） | — |
| ack 到 11（head） | `floor=11, blocked=false` | 11 | `SnapshotTooOld`（6<11） | `SnapshotTooOld` |
| release | 不适用 | 不存在 | `SnapshotTooOld` | 只有 `floor=11` 成功 |

“重新注册旧位置”与水位的检查是同一个 `checked_revision`（`strataleaf/mvcc.py:15`）：低于 floor 一律 `SnapshotTooOld`，与消费者是否还存在无关。上表只填了实际运行过的格子；`—` 表示示例未取该组合，不是“允许”。

**适用条件**：确认 / 释放只应作用于有名字、有归属的消费者；不要在不知道名字的情况下按“计数”决策（`inspect` 只给 `counts`）。

**做错的损失**：

- 替消费者 ack 到 head：它失去重放这段提交的能力，之前没有真正处理的数据无法通过 feed 补回；一旦 floor 越过，`release`/重新注册也救不回来。
- 直接 `release` 而不是 ack：同样丢历史保护，而且连“这个消费者存在”的事实都没有了；事后排查只能靠外部记录。
- 反向错误：让已下线的消费者永远卡住水位，`versions`/WAL 持续增长，最终触发 2.1 的连锁问题。

### 2.6 恢复成主库，和恢复成副本

**观察**：需要“用备份恢复”时，两个选项看起来只是模式差异，实际改变的是**身份、保护与重放**。

**复现**：`python3 -m examples.takeover restore`。示例先在有保护点时备份（`held`，floor=1），解除并回收后再备份（`compacted`，floor=6），再分别恢复为主库与 follower。

**判据**：

- 备份里的 `revision` 与 `floor` 都保留；恢复**不会**把 floor 退回。`held` 恢复后 `[1, 6]` 可读，`compacted` 恢复后只有 `[6, 6]`，即使请求的是更早的修订也仍然 `SnapshotTooOld`。
- 恢复为**主库**：换 `database_id` 和 `cursor_secret`，`upstream` 清空；旧游标一律 `InvalidCursor`。
- 恢复为**副本**：保留原 `database_id`，`upstream` 指向该身份；旧游标若修订仍在 `[floor, head]` 内可以继续用（示例中游标修订 6、floor 6，返回记录）；副本拒绝主写与 `compact`。
- 恢复为主库后，**原先绑定旧身份的 follower 拒收新主库的包**：`envelope does not match configured upstream`。此时旧 follower 仍然能读它自己的旧 head，但已经和新的写入分叉；正确做法是从新主库的备份重建 follower，而不是改 `upstream`。
- 两者都**删除 pin 与消费者**（`strataleaf/archive.py:87-88`），但**保留 receipts**。所以恢复后不能假设下游位置还在；需要按恢复后的 floor 重新注册。
- 恢复目标必须是不存在的路径（`Conflict`）；校验和不符或文件不是受支持的库会直接拒绝（`DatabaseError`）。

**处置**：

- 需要“新的可写主库”：`Archives.restore(..., follower=False)`，然后按恢复后的 `floor` 重建 pin/消费者与下游游标；通知所有持有旧 `database_id` 的副本，它们会因来源不匹配拒收（见 2.7）。
- 需要“只读对照/追赶源”：`follower=True`，不要尝试提升为主库（基线没有提升接口）。

**前后可观察**（同一次运行）：

| 属性 | 原库（回收后） | 恢复为主库 | 恢复为 follower |
| --- | --- | --- | --- |
| floor | 6 | 6 | 6 |
| database_id | A | 新值 | A |
| pins/consumers | 0/1 | 0/0 | 0/0 |
| 旧游标（源库签发） | —（示例未在原库复测） | `InvalidCursor` | 可用：修订 6 在 `[6, 6]` 内 |
| 回执重放 | 成功（见 `receipts`） | `replayed=true`（回执在备份中） | 拒绝：`ReadOnly` |
| 写入 | 可写 | 可写 | `ReadOnly` |

`—` 表示该格未实际取数；不要把它读成“不可用”。关键是同一份游标在主库与 follower 上的结果不同，原因是主库换了签名密钥与身份。

**适用条件**：备份的 `revision/floor` 与你要恢复的时间点匹配；已有更晚的、需要保留的历史时，恢复旧备份会主动放弃它。

**做错的损失**：

- 用**比现场更旧**的备份恢复：备份之后的修订、提交日志与回执一并消失。示例 `receipts` 显示，恢复早于某次请求的归档后，该请求的重试变成 `replayed=false`，以新修订再次执行——窗口内的“恰好一次”语义丢失，条件写入可能冲突，删除会再删一次。
- 恢复为主库后继续使用旧游标：`InvalidCursor`，分页现场作废。
- 把 follower 当主库用：`ReadOnly`；把旧身份 follower 接到新主库：来源不匹配。

### 2.7 副本追不上：缺口、校验、来源、超出源 floor

**观察**：`replication.apply` 报错，或副本停在旧 `head`、索引列表为空、`catalog_epoch=-1`。

**复现**：`python3 -m examples.takeover replica`。

四种错误对应四种处置，不能混：

| 现象/错误 | 含义 | 处置 |
| --- | --- | --- |
| `replication envelope checksum/format mismatch` | 包在传输中损坏或被改动 | 丢弃该包，重新导出；保留原包与摘要作证据 |
| `envelope does not match configured upstream` | 包来自另一身份（恢复出的新主库/另一库） | 确认来源；跟随新源必须重建 follower（新备份恢复），不能混流 |
| `missing preceding replication range` | `since > 本地 head`，中间有洞 | 找到覆盖 `head+1` 起的包；找不到就只能重建 |
| `SnapshotTooOld`（在**源端**导出时） | follower 的 head 早于源的 floor，源已无法生成该起点 | 重建 follower（从当前备份恢复） |

**分阶段追赶的正常现象**（不要误判为损坏）：只要包没有到源 head，副本会先清空索引定义、`catalog_epoch` 置 -1、查询回退版本扫描；追平后才安装最新索引。

**前后可观察**：

| 阶段 | head | indexes | 查询计划 | deep inspect |
| --- | --- | --- | --- | --- |
| 部分追赶（`limit=2`） | 4 | `[]` | `version_scan` | `ok=true` |
| 追平 | 6 | `by_region` | `equality_index` | `ok=true` |
| 重复同一包 | 6（不变） | 不变 | — | `applied=0` |
| 源端已回收后导出旧 head | 本地仍可读 | 保留 | — | 源端返回 `SnapshotTooOld` |
| 源端 floor 的空包（`since=through=floor`） | 2（不变） | 保留 | — | `missing preceding replication range` |

最后一行是关键区分：**空包不是桥接手段**。只有 `since = through = 源 head` 且 follower 也在同一 head 时，空包才会被接受：目录未前进时返回 `applied=0` 且不改动；目录 epoch 前进时同样返回 `applied=0`，但会更新 `collections` / `index_defs` 并重算索引。对落在 floor 之前、`since > 本地 head` 的 follower，空包不能补洞，只会得到 `missing preceding replication range`。

**目录/模式变更可以只靠空提交传递**：源端 `catalog.evolve` 不产生文档提交，但 `catalog_epoch` 前进；`since=through=head` 的导出仍能让副本更新模式（示例中 generation 1→2，`applied=0`，重复应用仍是 `applied=0`）。看到“追平了但模式还是旧的”时，应先发一次这种包，而不是重建。

**副本上的本地 pin / 消费者**：可以注册，会随 `apply` 保留，但副本拒绝 `compact`（`ReadOnly`），所以它们**不保护任何回收**，只是本地记账。不要把它们当成源端的保护点。

**适用条件**：副本是同一上游、连续提交；基线不提供提升、双向复制或网络传输。

**做错的损失**：对缺口包“跳过再试”，会让副本永久停在旧 head，后续所有包都因 `since > head` 失败；对来源不符的包强行改 `upstream`，会把两个分叉写进同一文件，索引与历史不再对应唯一源；用裸文件覆盖 follower 来“重建”，会丢失身份绑定并制造新的分叉（见 2.8）。

### 2.8 用主文件裸拷贝当备份

**观察**：`x.db` 只有 4 KiB，看起来“空的”；或者有人在 WAL 还有内容时只复制了 `x.db`。

**复现**：`python3 -m examples.takeover layout`。示例在 WAL 有 688072 字节时只复制 `x.db`。

**判据**：`archives.backup` 生成的文件包含全部表与 WAL 中的内容（示例备份 94208 字节、`revision=21`）；而只复制 `x.db` 得到的库能正常打开、`inspect` 也全绿，但 `revision=0`、所有计数为 0、`fingerprint` 与源不一致。**“文件能打开且自检通过”不是“备份正确”的证据**。

**处置**：一律使用 `Database.archives.backup`（SQLite 在线备份 API，含校验与原子发布）；恢复使用 `Archives.restore`。若现场只有疑似裸拷贝，**保留现场**：先记录来源路径、文件大小、`database_id`、`head/floor`，不要用它覆盖任何活动库，也不要先打开写入。

**适用条件**：任何备份/迁移/重建动作。

**做错的损失**：丢掉全部仍在 WAL 中的数据（示例中是全部数据）；更糟的是丢失过程不可见——自检通过、能查询、只是“空的”，很容易被当成“业务本来就没数据”。

---

## 3. 四类症状在同一条时间线上

`entangled` 用例把“空间增长、消费者落后、历史读取可用、恢复接收”放在一个过程里：

| 时刻 | head/floor | versions/commits | pins/consumers | x.db / wal | 旧修订读 |
| --- | --- | --- | --- | --- | --- |
| 初始症状 | 41 / 0 | 44 / 41 | 1 / 1（都在 1） | 4096 / 1207192 | 成功 |
| `compact(41)` | 41 / 1 | 44 / 40 | 1 / 1 | 未变 | 成功 |
| 只解除 pin | 41 / 1 | 44 / 40 | 0 / 1 | 未变 | 成功 |
| 消费者 ack 到 41 | 41 / 41 | 4 / 0 | 0 / 1 | 4096 / 1273112 | `SnapshotTooOld` |
| 用当前库备份再恢复 | 41 / 41 | 4 / 0 | 0 / 0（新身份） | 恢复文件 | 仍 `SnapshotTooOld` |

读法：

1. 空间增长的直接原因是**消费者停在 1、pin 停在 1**，不是 SQLite 本身；
2. `compact` 在“被卡住”的状态下已经推进了 floor，历史从那时起就开始不可逆地减少；
3. 解除 pin 不够，消费者仍然卡住；确认消费者位置才让 floor 到 head；
4. floor 到 head 后，**旧读取和旧重放能力才是真正丢失的时刻**；
5. WAL 在主文件仍是 4 KiB 时继续增长到约 1.27 MB——回收与物理容量是两条线；
6. 用“当前库”恢复不会把旧读取带回来：floor 被原样保留，旧请求只能靠回执重放（示例中 `replayed=true`），而不是靠历史读取。

---

## 4. 推进确认或释放保护：事实清单

只有在下列事实齐备时才动手；缺任何一项，先补证据。

### 4.1 释放 pin

1. 只读到名字与修订：`pins(name, rev)`，并确认它等于/低于当前 floor 附近的卡点。
2. 归属：哪个进程/任务、部署配置或 runbook 里能对上；无人认领也要有明确的责任人决定。
3. 用途：它是否还打算读该修订；如果只是“怕以后要”，先备份再释放。
4. 若可能需要事后复盘：先 `archives.backup`，记录返回的 `revision/floor/sha256`。备份**不会**把 floor 之前的历史带回来，但能保住释放时刻的完整状态。
5. 释放后重新 `compact`，确认 `blocked=false` 或卡点已转移到另一个具名持有者。

### 4.2 推进 consumer ack

1. 名字与当前位置：`consumers(name, ack)`。
2. 消费者自身的持久处理进度，能覆盖到目标修订；系统只接受声明。
3. 目标修订必须 `>= floor`。低于 floor 的 ack 得到 `SnapshotTooOld`（而且这个检查先于“消费者是否存在”，对已释放的名字也会得到 `SnapshotTooOld` 而不是 `NotFound`）。
4. 确认不打算再重放目标位置之后的部分之前，先确认 floor 还没越过它；一旦 floor 越过，`feed.read(旧位置)` 失败。要保留“从旧位置重放”的能力，只能靠仍在该位置之前的水位（即尚未推进），不能事后补。
5. 单步确认后重新 `compact`，观察 `removed_commits/removed_versions` 与 `floor`，而不是只看 `blocked`。

### 4.3 释放 consumer

只有在其永久下线、或明确决定放弃其重放能力时做。释放就是删行，不是“暂停”；之后：

- 该消费者的历史保护立即消失；
- `compact` 可以一次跨过很远（示例 floor 1→9，删除 8 个提交）；
- 以旧位置重新注册会被 `SnapshotTooOld` 拒绝，只能从当前 floor 注册。

### 4.4 恢复 / 重建接收端

1. 确认备份的 `revision/floor`；更老的备份意味着放弃其后的修订与回执。
2. 确认模式：主库（换身份）还是 follower（保留身份）。
3. 恢复后 pin/消费者为空，必须按恢复后的 floor 重建；不要直接复用旧位置。
4. 恢复为主库后，旧下游副本会因来源不匹配拒收；跟随新主库需要重建。
5. 副本追平前索引为空是正常状态；不要用“索引缺失”判断重建。

---

## 5. 必须保留现场的情形

遇到以下情况，先停手、留证、再处置：

1. **备份来源可疑**（只有主文件、文件大小与 head 明显不符、来源路径不明）：保留文件与目录清单，不要打开写入，不要用它恢复。
2. **校验和不符 / 来源不匹配 / 副本缺口**：保留原包、`sha256`、`source`、本地 `upstream/head`；不要跳过缺口继续应用。
3. **`inspect(deep=True)` 报告问题**（完整性、外键、保留提交不连续、索引与可见文档不一致）：在修复前先 `archives.backup`（若还能成功）并记录 `issues` 原文；不要先 `compact`。
4. **准备回收但可能还要复盘**：先备份，再释放/回收。`floor` 推进后，任何公开入口都无法取回更低修订。
5. **身份分叉**（同一 `database_id` 的两个文件各自前进，或恢复出第二个主库）：保留两份文件与各自 head，禁止用文件复制“合并”；确认哪一份是唯一写入方后再决定接收端。
6. **在副本上发现本地 pin/消费者**：保留清单；它们不保护回收，删除前确认没有本地读者依赖副本视图。

保留现场本身也有代价：只要长读或旧句柄还在，WAL 会继续增长（2.1）。所以“保留”应尽量用 `archives.backup` 快照 + 只读清单，而不是靠一直挂着旧连接。

---

## 6. 做错会永久丢掉哪种能力

| 动作 | 立刻可观察 | 永久失去 | 不可逆点 |
| --- | --- | --- | --- |
| `compact` 推进 floor | `removed_versions/commits`、`floor` 变化 | 低于 floor 的历史读取；`feed`/`replication.export` 的旧起点；从该点重建副本的能力 | floor 只增不减，旧修订读取报 `SnapshotTooOld` |
| `unpin` | pin 消失，下一次 `compact` 前进 | 该读者对固定修订的读保证 | 旧修订不可再 pin |
| `acknowledge` 前进 | 位置变化，`compact` 前进 | 消费者重放 `(旧位置, 新位置]` 的能力 | floor 越过旧位置后 `feed.read` 失败 |
| `release` 消费者 | 行删除，`compact` 立刻前进 | 上述重放能力 + “消费者存在”的事实 | 只能从当前 floor 重新注册 |
| 恢复更旧的主库 | 新身份、旧 head | 备份之后的修订、提交与回执（幂等窗口） | 旧文件被新文件取代；下游身份不匹配 |
| 恢复主库（任意） | `database_id`/`cursor_secret` 更换 | 旧游标、按旧身份绑定的下游 | 旧游标 `InvalidCursor` |
| 裸拷主文件 | 文件可打开、自检通过 | WAL 中的全部数据 | 不可见：无错误、无告警 |
| 放弃/重置 follower 而不留包 | 换新库 | 缺口范围内的连续性与审计 | 新身份建立后无法回到旧流 |

注意两种损失的可观察性不同：`compact` / `release` 的损失是**可见且可解释**的（下一次读取明确报 `SnapshotTooOld`）；裸拷贝的损失是**静默**的（一切正常、只是为空）。接管时应优先防范后者。

---

## 7. 复现材料

```bash
python3 -m examples.takeover <case>     # 单个场景，打印一行 JSON
python3 -m examples.takeover all        # 依次运行全部场景
```

| case | 决策点 |
| --- | --- |
| `wal` | WAL 增长是 checkpoint 被读快照挡住，还是历史没回收 |
| `reclaim` | 逻辑回收与物理容量是两件事 |
| `watermarks` | `SnapshotTooOld` / `InvalidDocument` / `None` / `NotFound` 的区分；floor 读数与物理最小修订不同；游标不保护水位 |
| `consumer` | ack 前进、释放、只补数据；释放即放弃保护 |
| `restore` | 主库与 follower 的身份、保护清理、回执保留、游标失效范围 |
| `receipts` | 回执在回收后仍在，但恢复更旧归档后请求会被当成新的 |
| `layout` | 主文件裸拷贝会静默丢掉 WAL 中的数据 |
| `entangled` | 四类症状同一条时间线：先卡在消费者，后丢历史读，空间不降，恢复不救 |
| `replica` | 缺口、校验、来源、超出源 floor 的四种失败；追赶期索引缺失正常；空包与目录心跳 |

所有 case 都不需要联网，`all` 在本机约 1–2 秒。默认运行 `watermarks`。

每个 case 都输出可直接比对的字段：

- `state`：`diagnostics.inspect()` 的 `head/floor/counts/issues/mode/database_id`，加只读的 `files`、`pages`、`protections`；
- `protections`：只读打开的 `pins(name, rev)`、`consumers(name, ack)`、`metadata_head/floor`、`catalog_epoch`；
- `checkpoint`：`(busy, log, checkpointed)` 原样；
- `pages`：`page_size/page_count/allocated_bytes/freelist_pages`；
- 每个操作的成功值或 `{error, message}`。

只读证据的采集顺序在 `examples/takeover.py` 里集中实现为 `state()`、`protections()`、`pages()`、`sizes()`；想复核某一行的数字，直接运行对应 case 即可。

这些用例唯一的非 API 访问是：只读 SQLite 查询、一个外部 SQLite 连接持有的读快照（演示 checkpoint 压力），以及 `layout` 中故意做的裸拷贝对照。它们都不参与生产语义，也不写入被观察的库。

---

## 8. 不做的事与已知边界

- 本文不新增命令、不改 `strataleaf/`，也不把示例接成测试套件；仓库测试声明仍为 deferred。
- `compact` 的 `blocked` 是布尔摘要，不是审计记录；真正的审计应保存每次返回的 `requested/floor/removed_*`。
- `inspect` 不覆盖 WAL 大小、`freelist`、逐消费者校验；不要把它当作“空间健康”或“消费者健康”的唯一依据。
- 空闲页复用、`VACUUM`、文件系统重复数据删除等属于 SQLite/运维层，不在库 API 承诺内；任何改变文件布局的动作都应在所有句柄关闭后进行，并单独评估停机窗口与临时空间。
- 副本提升、双向复制、网络传输不在基线；不能用文件复制或改 `meta` 来模拟。
- 到期回收只在显式 `retention.sweep` 时发生；时间经过本身不产生删除提交，也不释放任何空间。副本上带候选的 `sweep` 会因只读被拒绝，没有候选时返回 0 且不写。
