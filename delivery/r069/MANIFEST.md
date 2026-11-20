# StrataLeaf 数据库接收材料人工交接清单

- 交接根目录：`/tmp/strataleaf-handoff/`（位于仓库 `/workspace` 之外）
- 数据材料根：`root/`（可由脚本整体重建）；辅助脚本：`scripts/`（与数据目录分离）
- 软件版本：strataleaf 0.1.0，磁盘格式标识 `1`，Python 3.11，SQLite 3.40
- 生成方式：仅使用公开库 API（`Database`、`Archives`、`catalog/indexes/feed/retention/replication/transfer/diagnostics`）与只读 SQLite 观察；未编写新的诊断工具，未改动任何生产数据库
- 机器可读版本：`root/MANIFEST.json`；数据区全部文件 SHA-256：`root/SHA256SUMS`（在 `root/` 下用 `sha256sum -c SHA256SUMS` 复核）
- 可复跑脚本（与数据目录分离，全部以 `python3 -I` 运行，参数为 `<数据根> <strataleaf 包根 /workspace>`）：
  - `scripts/build_materials.py`：构建三个时间点材料与两个独立来源
  - `scripts/observe_materials.py`：`immutable=1` 只读连接 + 公开 API 观察
  - `scripts/verify_recovery.py`：公开 API 恢复与 floor 拒绝验证（写操作只落在临时目录）
  - `scripts/finalize_manifest.py`：重算校验和、生成 `root/MANIFEST.json`

---

## 1. 材料分类口径（五类事实，互不等价）

| 类别 | 在本批材料中的物理载体 | 含义 |
|---|---|---|
| 活文档（live documents） | `versions` 表中 `body IS NOT NULL` 且在读 head 可见的行 | 当前读视图实际返回的 JSON 文档 |
| 历史基准（historical baseline） | T1 备份、各备份内未回收的 `versions` 旧修订、`commits` 保留区间 | floor 之前仍需保留的每键基准版本与提交日志；回收后只能靠更早点的备份服务 |
| 墓碑（tombstones） | `versions` 表中 `body IS NULL` 的行 | 删除事实（含 TTL `sweep` 产生的删除提交）；隐藏当前值，但保留版本前置语义 |
| 索引定义（index definitions） | `index_defs` 表 + 当前 head 的 `index_values` 投影 | 只服务当前版本；历史读不消费索引；投影与定义是独立于文档修订的事实 |
| 消费位置（consumer positions） | `consumers.ack` 与 `pins` 表 | 变更流确认点与读保留点；单调前进，直接限制回收 floor |

关键边界：这五类是**不同的事实**。全局文档 `head`、历史 `floor`、目录 `catalog_epoch` 由三套独立计数器维护，任何一个都不能代表另外两个。

---

## 2. 三个时间点的材料内容

每个时间点目录 `root/materials/tN/` 含三件真实接收材料：在线备份 `backup_tN.db`（`archives.backup` 产物，附 `backup_receipt_tN.json` 回执）、复制包 `replication_pack_tN.json`（`replication.export` 信封，含校验和）、逻辑导出 `logical_export_segments_tN.json`（`transfer.export_lines` 的 JSONL）。

### T1（历史基准点）— `root/materials/t1/`

- head **2**，floor **0**，catalog_epoch **1**；模式 generation 1（字段 region/sequence/code），无索引
- 提交：rev1 批量写入 4 个文档（请求键 `seed_segments`，4 操作）；rev2 携带前置版本更新 `seg_01`
- 活文档：`seg_00@1, seg_01@2, seg_02@1, seg_03@1`；无墓碑
- 消费位置：消费者 `mirror_a`、`mirror_b` 均 ack=0；保留点 `long_reader` pin 在 rev2
- 复制包：since=0，through=2，含 rev [1,2]，信封 source = 源库 database_id
- 逻辑导出：revision=2，4 行
- 备份回执哈希已用包自带公开摘要助手独立重算，**一致**；`PRAGMA integrity_check=ok`，外键检查 0 行

### T2（目录变更 + 墓碑点）— `root/materials/t2/`

- head **3**，floor **0**，catalog_epoch **4**；模式 generation 2（新增 `zone` 字段）
- **目录在 head 不变的情况下单独前进了 3 个 epoch**：建索引 `by_region`、建唯一索引 `unique_code`、模式演进（见 `root/build_ledger.json` 的 `head_after_catalog_only=2`）。之后 rev3 才是文档提交：新增 `seg_04` + 删除 `seg_03`
- 墓碑：`seg_03@3`（body NULL）。公开 API 探针证明：head 处 `get("seg_03")` 返回 None；rev2 处仍能读到旧值
- 索引定义：`by_region(region, 非唯一)`、`unique_code(code, 唯一)`
- 消费位置：`mirror_a` ack=3（已跟上），`mirror_b` ack=0（刻意滞后）
- 复制包：through=3，含 rev [1,2,3]，包头携带新目录（generation 2）与两个索引定义
- 逻辑导出：revision=3，4 行（导出只含可见文档，不含墓碑）

### T3（回收 + TTL 墓碑点）— `root/materials/t3/`

- head **5**，floor **3**，catalog_epoch **5**
- rev4 写入带 TTL 的 `seg_ttl`（expires_at=1000）；显式 `retention.sweep(1000)` 产生 rev5 删除提交（时间经过本身不改读视图，只有 sweep 改）
- 墓碑：`seg_03@3`、`seg_ttl@5`
- 目录：`by_region` 被 drop（epoch 再 +1，head 不变），仅保留 `unique_code`
- 回收：释放 `long_reader`、两个消费者分别 ack 到 5 和 3 后，`compact(5)` 实际 floor=3（`blocked=true`，受最慢消费者限制）；移除 2 个旧版本行、3 条提交日志。`commits` 仅保留 rev [4,5]
- 消费位置：`mirror_a` ack=5，`mirror_b` ack=3；无 pin
- 复制包：since=3，through=5，**只含 rev [4,5]**（floor 之前的提交已不可导出）
- 逻辑导出：revision=5，4 行（与 T2 当前可见键集合相同）
- 公开 API 实证：T3 上 `get(..., revision=1)` 抛 `SnapshotTooOld: revision 1 is older than retained floor 3`

---

## 3. 为什么“相同 head”也不能互相替代

### 3.1 目录变化与 head 解耦（本批直接证据）

T2 的构建台账显示：建两个索引、演进一次模式期间 head 始终是 2，catalog_epoch 却从 1 升到 4。也就是说，**两个 head 完全相同的数据库可以拥有不同的模式 generation、不同的索引定义和不同的 epoch**。

推论（有证据支持的部分）：
- 同 head 数字 ≠ 同目录。复制协议正是用 catalog_epoch 防止“过时的同版本包”倒退目录（`replica.apply` 对 `through==head 且 epoch 不增` 的包判为重复空包）。
- 同 head 数字 ≠ 同读能力：T2 有唯一索引约束，T1 没有；在 T1 上能提交的写在 T2 可能因唯一冲突失败。
- 同 head 数字 ≠ 同历史：T1 floor=0 可读 rev1，T3 floor=3 明确拒绝 rev1。

### 3.2 来源不同（独立 database_id）也不能互相替代

`root/independent_origins/` 提供三个同 head=5、同当前读视图的独立来源：

| 来源 | 如何产生 | database_id | 游标密钥 | 文件 SHA-256 |
|---|---|---|---|---|
| origin_a：`root/materials/t3/backup_t3.db` | 源主库在线备份 | id-A | secret-A | hash-A |
| origin_b：`root/independent_origins/restored_primary/origin_b.db` | T3 备份经 `Archives.restore` 恢复为**新主库** | **新 id-B** | **新 secret-B** | hash-B |
| origin_c：`root/independent_origins/transferred_origin/origin_c.db` | 全新库经公开写入/转移 API **重放同样 5 个修订** | **新 id-C** | 新 secret-C | hash-C |

观察结果（`root/evidence/observations.json` → `independent_origins`）：三个 id 互不相同、三个游标密钥互不相同、三个文件哈希互不相同；但 `diagnostics.fingerprint()` 在 revision 5 上**完全相等**，且三个库 `inspect(deep=True)` 均 ok。

不能互相替代的原因：
- 游标签名由各库私有密钥生成，A 的游标拿到 B/C 上无效；fingerprint 相同**不代表**游标可移植。
- 副本绑定上游 database_id；来源 id 不匹配的复制包会被 `replication.apply` 拒绝。
- restore 为新主库时会清空 pins/consumers（已观察到 origin_b 两者均为空），消费保护位置必须重新登记，无法从另一个库“继承”。
- origin_c 的提交历史是重放产生的独立历史，请求键、receipts 与源库不同；它不是源库的备份等价物。

### 3.3 文件哈希不同但逻辑读视图相同——如何解释

这是预期行为，不是损坏。本批有两组实证：

1. **T2 与 T3 备份**：文件哈希不同（同为 86016 字节但页内容不同：T3 多了 rev4/5、少了被回收的行、floor/epoch 不同），但两者在各自 head 的当前可见文档集合相同（都是 4 个键，`fingerprint` 同为 `2d92308c…41ce`）。指纹只摘要“某修订上可见的键/版本/文档/到期事实”，不包含历史长度、floor、目录定义、物理页布局。
2. **三个独立来源**：如 3.2，哈希、id、密钥全不同，fingerprint 全相同。

产生哈希差异但逻辑视图等价的具体机制（均由公开 API 路径造成，无字节篡改）：
- `Archives.restore` 恢复新主库会重写 `meta`：新 `database_id`、新 `cursor_secret`、mode/upstream 调整，并清空 pins/consumers；SQLite 在线备份按页复制，元数据页与空闲页也会带来字节差异。
- 转移导入/重放走完整写入路径，生成全新的版本链物理排布与新的 receipts。
- 任何新提交、checkpoint、WAL 回收状态变化都会改变物理字节，但不改变旧修订上的逻辑读视图。

唯一保持文件哈希不变的变换是**纯字节拷贝**（对照组 `root/scratch_control/t3_byte_copy_control.db`，哈希与 T3 备份逐字节相同）。凡经过受支持的逻辑变换（restore/import/新提交），哈希必然改变。

---

## 4. 每份文件的用途与核对方式

| 文件 | 产生 API | 能核对什么 |
|---|---|---|
| `root/materials/tN/backup_tN.db` | `archives.backup`（SQLite 在线备份 + 完整性/摘要校验后原子发布） | 完整数据库事实；SHA-256 与回执一致；`integrity_check=ok`；可经 `Archives.restore` 恢复（三份均已验证，见 `root/evidence/restore_and_floor_checks.json`） |
| `root/materials/tN/backup_receipt_tN.json` | backup 回执 | revision/floor/database_id/sha256；哈希已独立重算复核 |
| `root/materials/tN/replication_pack_tN.json` | `replication.export` | 信封 `sha256` 可重算校验传输损坏；包头给出来源 id、since/through、floor、catalog_epoch、目录与索引定义；只含 floor 之后的连续提交 |
| `root/materials/tN/logical_export_segments_tN.json` | `transfer.export_lines` | 仅当前可见文档（键/文档/到期），JSONL；可经 `import_lines` 走正常写入路径回灌；不含墓碑、不含历史、不含目录定义 |
| `root/independent_origins/restored_primary/` | `Archives.restore` | 证明“恢复新主库换身份但保留逻辑内容”，附恢复回执 |
| `root/independent_origins/transferred_origin/` | `catalog` + `transfer` + `batch` | 证明独立重放可达到同 head 同视图但不同身份/历史，附各重放提交回执 |
| `root/live_source/source_primary.db` | 构建过程的源主库 | 仅作溯源留存；构建结束后再无写句柄打开 |
| `root/evidence/observations.json` | 只读观察脚本 | 物理表计数、版本链、墓碑行、索引定义、消费位置、fingerprint、跨来源对比 |
| `root/evidence/restore_and_floor_checks.json` | 公开 API 验证 | 三份备份可恢复性、T3 floor 拒绝、覆盖目标拒绝、T2/T3 同视图不同文件 |
| `root/build_ledger.json` | 构建脚本 | 每个步骤的修订号与回执（含 catalog-only 步骤 head 不变的记录） |
| `root/sidecar_cleanup.json` | 构建脚本 | 清理掉的 SQLite `-wal/-shm` 临时侧车清单，保证交付目录只含实际材料 |
| `root/SHA256SUMS` / `root/MANIFEST.json` | 汇总脚本 | 数据区全量哈希与机器可读清单 |

---

## 5. 证据边界：能核对什么、不能据此推断什么

**可以核对（本批已保留依据，可独立复跑）：**
- 每个文件的 SHA-256、备份回执哈希、复制包信封校验和
- SQLite 物理完整性（`PRAGMA integrity_check`、`foreign_key_check`）——通过 `immutable=1` 只读连接完成，观察过程不创建任何 WAL 侧车、不触发任何写路径
- 指定修订上的逻辑读视图（`get/query/fingerprint`）、墓碑与旧版本可见性、floor 越界报错
- 目录 generation、catalog_epoch、索引定义、消费者 ack、pin 的具体取值
- 三份备份都能用公开 `Archives.restore` 恢复到未占用目标，且恢复后 inspect ok
- 恢复拒绝覆盖已存在目标；备份文件在全部只读/恢复验证后字节哈希保持不变

**不能据此推断（明确的能力边界）：**
- fingerprint 相同**不能**推断数据库身份相同、游标可互换、上游绑定一致或历史深度相同——它不涵盖 id、密钥、floor、模式、索引与物理布局
- 文件哈希不同**不能**推断内容被篡改——受支持的逻辑变换本来就改变字节
- `integrity_check=ok` 只证明 SQLite 物理结构与外键完整，**不能**证明业务数据正确；业务正确性由 fingerprint 与读视图探针支撑
- 逻辑导出只含当前可见文档，**不能**用来重建墓碑、版本链、模式演进或消费位置
- 复制包校验和只检测传输损坏，**不**代替对端身份认证；能否应用仍取决于上游 id 与连续修订范围
- T3 材料**不能**用于读取 rev1–rev2 的历史；该需求只能回到 T1 备份。消费者位置/pin 是恢复后需要人工重新登记的事实，系统不替调用方证明处理成功
- 本批材料不预埋任何损坏样本，也不对任何生产路径做写操作；观察结论只对这些交付文件本身有效

---

## 6. 接收方复核步骤（全部只读 / 公开 API）

```bash
cd /tmp/strataleaf-handoff/root
sha256sum -c SHA256SUMS                                   # 1. 数据区全量哈希核对
python3 -I ../scripts/observe_materials.py . /workspace   # 2. 只读观察重跑
python3 -I ../scripts/verify_recovery.py  . /workspace    # 3. 恢复与 floor 验证（写操作仅在临时目录）
python3 -I ../scripts/finalize_manifest.py . /workspace   # 4. 重生成 MANIFEST.json 与 SHA256SUMS
```

预期：命令均输出 `{"ok": true, ...}`；复核结束后材料目录无 `-wal/-shm` 残留，备份哈希与 `SHA256SUMS` 保持一致。
