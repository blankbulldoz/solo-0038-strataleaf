# StrataLeaf 离线验收交接包

本包用于把一份**带验收依据**的数据库交接材料交给只能离线查看的同事。
产品代码（strataleaf 源码树）不在本包内、未做任何改动；所有数据均通过产品
**公开 API**（`Database` 及其子对象）准备，另用标准库 `sqlite3` 只读连接做独立复核。

- 生成日期（UTC）：见 `MANIFEST.json.generated_at_utc`
- 数据场景：集合 `orders`，含模式演进、唯一/普通索引、多版本与墓碑、幂等回执、
  变更流消费者、读保留点、在线备份、TTL 到期、历史回收，以及一次“恢复为新主库”的完整演练。
- 隐私口径：**不含本机私有绝对路径，不含任何签名密钥（cursor_secret）的值**；
  数据库文件、备份与签名游标样本按**私有材料**交接（见 `MANIFEST.json.private_materials`）。

## 接收者要回答的四个问题，结论在哪

| 问题 | 结论 | 证据入口 |
| ---- | ---- | -------- |
| 当前文档是否正确 | 是，与独立预期 rev5 读视图逐字段一致 | docs/claims_and_evidence.md §C1，观察 02_02 |
| 约束是否仍有效 | 是，唯一冲突与 schema 校验在恢复后仍被拒绝，deep 诊断 ok | §C2，观察 02_04 |
| 旧请求重试会怎样 | 同键同操作回放原回执且不增修订；同键异操作冲突；无键请求走版本冲突 | §C3，观察 02_05 / 01_07 |
| 原消费位置能否直接用 | **不能**：恢复清空 consumers/pins，查位 NotFound；提交历史仍在，可在 head 重新注册 | §C4/C6，观察 02_06/02_08 |

另含：恢复会**更换 database_id 与 cursor_secret**、旧签名游标失效（§C5）；
备份边界 rev5 与越界读拒绝（§C7）；校验和失配/目标占用的拒绝路径（§C8）。

## 健康对照：三种信号各能证明什么

`materials/controls/` 提供两个与主库**当前正文完全相同**、但其他事实不同的健康库：

- **对照 A（control_catalog）**：正文相同，目录事实不同（模式代数 1→2、多一个索引）。
- **对照 B（control_requests）**：正文相同，请求处理事实不同（无任何幂等回执，重放产生新修订）。

由此实证（详见 `docs/discrimination.md`）：

- 只看 **head 修订号**：发现不了目录或回执差异。
- 只看 **文件 SHA-256**：能识别字节复制与篡改（含 integrity_check 漏报的非活动字节篡改），
  但逻辑等价的恢复库哈希不同，故不能单独证明逻辑可恢复。
- 只看 **活文档指纹**：三个库完全相同，发现不了模式/索引/回执差异。

## 证据为什么可信（独立性）

1. `materials/ledgers/requests.plan.json` 是**原始写入请求账本**，在任何数据库调用之前整理。
2. `scripts/build_expectations.py` **不导入产品代码、不打开数据库**，仅从账本独立推演预期，
   产出 `evidence/expectations/expected.json`（附 sha256）。
3. 阶段一至三用公开 API 产出原始观察 `evidence/observations/*.json`。
4. `scripts/04_cross_check.py` 只比对“独立预期 vs 实际观察”，给每项结论附
   裁定、观察出处与未证明范围；本轮 **15/15 supported**，无 contradicted/not_observed。
5. 每条结论的“未证明范围”与无环境自查流程见 `docs/evidence_gaps.md`。

## 离线接收者怎么验（无需运行环境）

```
# 1) 全包文件哈希（MANIFEST 自身也在其中）
sha256sum -c SHA256SUMS
# 2) 独立预期未被改
(cd evidence/expectations && sha256sum -c expected.sha256)
# 3) 读验收总报告
python3 -I -m json.tool evidence/manifests/acceptance_report.json
```

有 Python 3.11+ 与一份未改动的产品源码树时，可按 `docs/RUNBOOK.md` 第 2 节端到端重跑
（全部脚本用 `python3 -I` 隔离执行，路径经参数传入，不写包外位置）。

## 私有材料处理提示

`materials/private/` 下的 `.db` 与 `.token` 含真实数据与签名材料，应通过私有通道交接、
最小授权留存；对外分发可仅取 `docs/`、`evidence/`、`scripts/`、`materials/ledgers/`
与 `materials/controls/`，但缺少私有库时 C1-C8 的“可独立重放”属性将无法复核，
只能依赖已固化的观察 JSON（这一限制本身登记在 evidence_gaps.md）。
