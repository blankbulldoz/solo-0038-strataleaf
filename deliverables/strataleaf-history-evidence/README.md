# StrataLeaf 历史文档材料包

本目录是一份**对外交付的历史材料**，由临时数据库上的公开 API 生成；不是产品功能，也未改动 `strataleaf/` 产品代码。

## 先用一分钟分清两类记录

| 类别 | 文件 | 回答的问题 | 不能回答的问题 |
| --- | --- | --- | --- |
| **历史读视图** | `data/views/revision-0001.json` 等（附 `.md` 可读渲染） | 在某个固定读修订下，哪些文档可见、实际版本是多少 | 中间发生过哪些提交 |
| **变更过程** | `data/change-process.json`（附 `data/timeline.md`） | 哪个提交对哪个键做了什么 | 某个修订的完整可见集合 |
| **派生清单** | `data/document-inventory.json`（附 `.csv` / `.md`） | 文档键、实际版本、所属提交如何一一对应 | 独立的新事实 |
| **同内容解释** | `data/key-explanations.json` | 为什么端点内容相同却版本不同 | 其他键的语义 |
| **缺口登记** | `data/gaps.json` | 材料缺了什么、为什么、如何补救 | 补齐材料之外的证据 |
| **观察来源** | `data/observations.jsonl` | 每条事实来自哪次本地 API 调用 | 远程或第三方来源 |

每个 JSON 文件都带 `record_kind` 与 `record_kind_zh`，可机器区分，不依赖文件名。

## 这批材料是怎么产生的

1. 在独立临时目录创建全新 SQLite 数据库，建一个 `segments` 集合。
2. 用 `Database.batch(request_key).commit()` 连续提交 7 次，覆盖新增、改写、删除与重建。
3. 用 `Database.query(..., revision=N)` 导出 4 个所选修订的读视图，并用 `Database.diagnostics.fingerprint(N)` 记录内容指纹。
4. 用 `Database.feed.read(0)` 取得完整提交序列，形成变更过程。
5. 用 `Database.get(..., revision=N)` 在每个提交修订复核对应键，形成版本清单中的 API 旁证。
6. 导出后显式执行 `Database.retention.compact(head)`，再尝试重读旧修订和旧提交，把失败与被回收的片段登记为缺口。

所有观察都是在本机临时库上通过公开库 API 取得的；临时库在结束时删除，不随材料交付。

## 键与版本速览

共 6 个文档键、15 个实际版本；所选视图修订为 1, 3, 5, 7。

### 内容相同、版本不同的键

- `seg_east_01`：读视图 r1 与 r7 内容哈希同为 `2de3b9f45adc…`，但实际版本分别为 1 与 3；中间版本：r2(rewritten)。
- `seg_west_02`：读视图 r1 与 r7 内容哈希同为 `94c7470029dc…`，但实际版本分别为 1 与 5；中间版本：r4(deleted)。

这说明**内容哈希相同不等于同一版本**：删除后重建、改写后改回，都会产生新的实际版本。

## 缺口必须显式对待

- `GAP-001`（views_not_exported_for_every_revision，recoverable_from_other_parts）：只导出了所选修订的历史读视图；其余提交修订没有单独的快照文件。
  - 影响：不能把“没有某修订的视图文件”理解成该修订没有提交或没有变化。
  - 补救：这些修订的逐操作内容仍完整保存在 data/change-process.json；任意键在该修订的状态可由 data/document-inventory.json 的 versions 推出。
- `GAP-002`（source_history_reclaimed_after_export，retained_copy_in_material）：材料导出后，临时源库执行了显式回收；源库不再能重读所选视图对应的旧修订和旧提交。
  - 影响：材料中的历史读视图和变更过程是这些片段的留存副本；不能用导出后的源库再次核对它们。
  - 补救：以 data/views/*.json 与 data/change-process.json 及其 sha256 为准，并运行 verify.py 做内部交叉校验。
- `GAP-003`（tombstone_version_not_readable_through_get，recoverable_from_other_parts）：公开 Database.get 对墓碑返回空值，不能通过它取得“删除版本号”。
  - 影响：若只靠 Database.get，会看不出键是“从未创建”还是“已删除”。
  - 补救：删除的 actual_version 由 data/change-process.json 的操作修订给出；data/document-inventory.json 的 tombstone_note_zh 与 api_confirmation 记录了这一限制。
- `GAP-004`（internal_projection_not_exported，out_of_scope）：材料不包含 SQLite 内部表、索引投影、WAL 文件或游标签名。
  - 影响：接收者不能据此复核物理存储细节或索引物化，只能复核公开 API 语义。
  - 补救：如需物理层复核，应在源环境用 Database.diagnostics.inspect(deep=True) 单独取证；本材料只声明公开读视图与提交事实。

## 自校验

在任意装有 Python 3.11+ 的机器上运行：

```
python3 deliverables/strataleaf-history-evidence/verify.py
```

校验会检查：文件摘要、清单与变更过程的一致性、视图记录与清单的一致性、可见性推导、同内容解释的前提，以及缺口登记的必要字段。

## 边界

- 材料只描述公开 API 可观察的事实，不声明物理存储或索引投影。
- `actual_version` 是本库中该键的版本号；本库每次提交对每个键至多写一次，因此它等于产生该版本的提交修订，但两者含义不同，清单中分别列出。
- 生成时间、`database_id` 与摘要每次重建都会不同；请以本包内 `manifest.json` 记录的摘要为准。

## 重建

```
python3 tools/build_history_materials.py
```
