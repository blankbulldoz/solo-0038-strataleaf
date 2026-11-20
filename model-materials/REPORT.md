# StrataLeaf 查询写法选择材料（实测报告）

本报告由 `scripts/build_materials.py` 用 StrataLeaf 生产 API 在临时数据库上实际运行生成，仓库代码未被修改。每个谓词结果单元格都经过三重核对（查询 API == 库内 `predicates.matches` == 脚本解释器），不一致即中止生成，因此下表中的“排除原因”与引擎行为一一对应。

- 结果含义（谓词语义）见第 3、4 节；索引使用（执行方式）见第 5 节，两者刻意分开。
- 完整数据：`results/predicates.json`（28 谓词 × 4 修订 × 13 键的完整矩阵）。
- 复现：`python3 -I scripts/build_materials.py --repo /workspace`（每次在全新临时库上重建）。

## 1. 数据集：让每对易混概念各有一个对照文档

集合 `items`，模式只声明 `sku/qty/active/tags/profile` 五个字段的类型（`sku/qty/active` 必填）。`price`、`note`、`profile` 子字段、`history` 刻意不声明类型——只有未声明类型的字段才允许为 null，缺失与空值才能放进同一集合对照。

| 键 | 设计目的 |
| --- | --- |
| `item_01` | 基准文档：各字段均为常规值；rev4 被补丁（qty 5→1，note→fresh-v2） |
| `item_02` | 缺失组：无 price、无 note，profile 为空对象（score 缺失） |
| `item_03` | 空值组：price、note、profile.score 都是显式 null（与 item_02 的缺失对照） |
| `item_04` | 数值 1 组：qty/price/score 都是数值 1，note 是字符串 "1"（与 item_05 的布尔对照） |
| `item_05` | 布尔组：price/score/tags 用布尔占位，检验“布尔不当数值、true 不等于 1” |
| `item_06` | 同内容组 A：与 item_07 内容完全相同、键不同 |
| `item_07` | 同内容组 B：与 item_06 内容相同且字段书写顺序不同（验证键序无关） |
| `item_08` | 字符串组：price/score/tags 是数字外观的字符串，note 反而是数值 9.5 |
| `item_09` | 浮点组：score 10.0、tags 1.0，检验 int/float 在相等与索引中统一 |
| `item_10` | 删除重建组：rev2 删除留墓碑，rev3 以新内容重建（见 ITEM10_RECREATED） |
| `item_11` | 零值组：0、0.0、空串、空数组、空对象——都是“存在但为假值”，与缺失/null 对照 |
| `item_12` | 嵌套组：三层点路径 profile.audit.count 与对象数组 history |
| `item_13` | 分页演示：rev5 才写入，验证游标固定读视图 |

完整文档内容见 `data/dataset.json`。要点对照：

- **缺失 vs null**：`item_02` 没有 `price`/`note`；`item_03` 的 `price`/`note`/`profile.score` 是显式 null。
- **数值 vs 布尔**：`item_04` 全是数值 1；`item_05` 在同样位置放布尔 true。
- **int vs float**：`item_09` 的 `score` 是 10.0、`tags` 含 1.0。
- **字符串 vs 数值**：`item_08` 的 `price`/`score` 是数字外观的字符串，`note` 反而是数值。
- **同内容不同键**：`item_06` 与 `item_07` 内容完全相同，且 `item_07` 字段书写顺序不同。
- **删除后重建**：`item_10` 在 rev2 删除、rev3 以全新内容重建。

## 2. 修订时间线

| 修订 | 事件 |
| --- | --- |
| rev0 | 创建集合 items（目录操作，不推进文档修订） |
| rev1 | 种子写入 item_01..item_12（单批次 12 个 put） |
| rev2 | 删除 item_10（写入墓碑） |
| rev3 | 以 expected=2（墓碑版本）重建 item_10，内容全部更换 |
| rev4 | 补丁 item_01：qty 5→1，note→fresh-v2（expected 是文档自身版本 rev1，不是全局 head） |
| — | 创建索引 by_qty/by_active/by_score（目录操作，不推进文档修订）；唯一索引 uniq_sku 被拒绝 |
| rev5 | 分页演示过程中新增 item_13（验证游标固定读视图） |

可见文档数：rev1=12，rev2=11（item_10 墓碑），rev3=12（重建），rev4=12，rev5=13（item_13）。

`item_10` 版本链（实际 `db.get` 结果）：

| 读取修订 | 结果 | 说明 |
| --- | --- | --- |
| rev1 | `{"active": true, "note": "original", "price": 2.0, "profile": {"score": 3}, "qty": 4, "sku": "A-10", "tags": ["legacy"]}` | 原始内容 |
| rev2 | null（不可见） | 墓碑：当前读不可见 |
| rev3 | `{"active": false, "note": "recreated", "price": 20.0, "profile": {"score": 30}, "qty": 40, "sku": "A-10", "tags": ["legacy", "restocked"]}` | 重建后的新内容 |
| rev4 | `{"active": false, "note": "recreated", "price": 20.0, "profile": {"score": 30}, "qty": 40, "sku": "A-10", "tags": ["legacy", "restocked"]}` | 重建内容保持 |
| rev5 | `{"active": false, "note": "recreated", "price": 20.0, "profile": {"score": 30}, "qty": 40, "sku": "A-10", "tags": ["legacy", "restocked"]}` | 重建内容保持 |

`item_01` 在 rev1 为 `qty=5, note=fresh`，rev4 起为 `qty=1, note=fresh-v2`（补丁生效）。

## 3. 结果含义：每个谓词到底排除了谁

以下按主题分组。每个谓词给出 rev4（建电池时的 head）的匹配键与**完整排除清单**——被排除的每个键都列出原因，而不是只展示几个成功返回。历史修订间的差异附在小节末尾；全部修订的完整矩阵见 `results/predicates.json`。

### 缺失与空值

#### A1 · `{"field": "price", "op": "exists", "value": true}`

要点：exists:true 只问“字段在不在”，null 也算存在

**匹配（11 个）**：item_01、item_03、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11、item_12

**被谓词排除（1 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_02` | price 不存在；exists:true 不成立 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1 匹配 item_01、item_03、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11、item_12 → rev2 匹配 item_01、item_03、item_04、item_05、item_06、item_07、item_08、item_09、item_11、item_12 → rev3 匹配 item_01、item_03、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11、item_12 → rev4 匹配 item_01、item_03、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11、item_12

#### A2 · `{"field": "price", "op": "exists", "value": false}`

要点：exists:false 只匹配完全缺失的字段

**匹配（1 个）**：item_02

**被谓词排除（11 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | price 存在，值为 9.5；exists:false 不成立 |
| `item_03` | price 存在，值为 null；exists:false 不成立 |
| `item_04` | price 存在，值为 1；exists:false 不成立 |
| `item_05` | price 存在，值为 true；exists:false 不成立 |
| `item_06` | price 存在，值为 4.0；exists:false 不成立 |
| `item_07` | price 存在，值为 4.0；exists:false 不成立 |
| `item_08` | price 存在，值为 "9.5"；exists:false 不成立 |
| `item_09` | price 存在，值为 10；exists:false 不成立 |
| `item_10` | price 存在，值为 20.0；exists:false 不成立 |
| `item_11` | price 存在，值为 0.0；exists:false 不成立 |
| `item_12` | price 存在，值为 3.25；exists:false 不成立 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### A3 · `{"field": "price", "op": "eq", "value": null}`

要点：eq null 只匹配显式 null；缺失字段不参与相等比较

**匹配（1 个）**：item_03

**被谓词排除（11 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | price=9.5（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_02` | price 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_04` | price=1（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_05` | price=true（布尔）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_06` | price=4.0（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_07` | price=4.0（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_08` | price="9.5"（字符串）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_09` | price=10（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_10` | price=20.0（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_11` | price=0.0（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_12` | price=3.25（数值）vs null（null）：null 只与 null 相等 → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### A4 · `{"field": "price", "op": "ne", "value": null}`

要点：陷阱：ne 同样要求字段存在，缺失字段连“不等于”都不成立

**匹配（10 个）**：item_01、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11、item_12

**被谓词排除（2 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_02` | price 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_03` | price=null（null）vs null（null）：两侧都是 null，相等 → ne 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1 匹配 item_01、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11、item_12 → rev2 匹配 item_01、item_04、item_05、item_06、item_07、item_08、item_09、item_11、item_12 → rev3 匹配 item_01、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11、item_12 → rev4 匹配 item_01、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11、item_12

#### A5 · `{"not": {"field": "price", "op": "eq", "value": null}}`

要点：not(eq …) 才是“非 null 或缺失”的写法，与 A4 对照

**匹配（11 个）**：item_01、item_02、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11、item_12

**被谓词排除（1 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_03` | price=null（null）vs null（null）：两侧都是 null，相等 → eq 匹配 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1 匹配 item_01、item_02、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11、item_12 → rev2 匹配 item_01、item_02、item_04、item_05、item_06、item_07、item_08、item_09、item_11、item_12 → rev3 匹配 item_01、item_02、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11、item_12 → rev4 匹配 item_01、item_02、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11、item_12

### 数值与布尔

#### B1 · `{"field": "qty", "op": "eq", "value": 1}`

要点：数值 1 的正常匹配

**匹配（2 个）**：item_01、item_04

**被谓词排除（10 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_02` | qty=0（数值）vs 1（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_03` | qty=3（数值）vs 1（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_05` | qty=7（数值）vs 1（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_06` | qty=2（数值）vs 1（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_07` | qty=2（数值）vs 1（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_08` | qty=12（数值）vs 1（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_09` | qty=8（数值）vs 1（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_10` | qty=40（数值）vs 1（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_11` | qty=0（数值）vs 1（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_12` | qty=6（数值）vs 1（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1 匹配 item_04 → rev2 匹配 item_04 → rev3 匹配 item_04 → rev4 匹配 item_01、item_04

#### B2 · `{"field": "qty", "op": "eq", "value": true}`

要点：true 不等于 1：布尔不与数值相等

**匹配（0 个）**：（无）

**被谓词排除（12 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | qty=1（数值）vs true（布尔）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_02` | qty=0（数值）vs true（布尔）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_03` | qty=3（数值）vs true（布尔）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_04` | qty=1（数值）vs true（布尔）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_05` | qty=7（数值）vs true（布尔）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_06` | qty=2（数值）vs true（布尔）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_07` | qty=2（数值）vs true（布尔）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_08` | qty=12（数值）vs true（布尔）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_09` | qty=8（数值）vs true（布尔）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_10` | qty=40（数值）vs true（布尔）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_11` | qty=0（数值）vs true（布尔）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_12` | qty=6（数值）vs true（布尔）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### B3 · `{"field": "active", "op": "eq", "value": 1}`

要点：反向同样成立：1 不等于 true；索引 token 命名空间也不相交

**匹配（0 个）**：（无）

**被谓词排除（12 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | active=true（布尔）vs 1（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_02` | active=false（布尔）vs 1（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_03` | active=true（布尔）vs 1（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_04` | active=true（布尔）vs 1（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_05` | active=false（布尔）vs 1（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_06` | active=true（布尔）vs 1（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_07` | active=true（布尔）vs 1（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_08` | active=true（布尔）vs 1（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_09` | active=false（布尔）vs 1（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_10` | active=false（布尔）vs 1（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_11` | active=false（布尔）vs 1（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_12` | active=true（布尔）vs 1（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### B4 · `{"field": "active", "op": "eq", "value": true}`

要点：布尔字段的正确写法：直接用 true/false

**匹配（7 个）**：item_01、item_03、item_04、item_06、item_07、item_08、item_12

**被谓词排除（5 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_02` | active=false（布尔）vs true（布尔）：布尔与布尔比较 → eq 排除 |
| `item_05` | active=false（布尔）vs true（布尔）：布尔与布尔比较 → eq 排除 |
| `item_09` | active=false（布尔）vs true（布尔）：布尔与布尔比较 → eq 排除 |
| `item_10` | active=false（布尔）vs true（布尔）：布尔与布尔比较 → eq 排除 |
| `item_11` | active=false（布尔）vs true（布尔）：布尔与布尔比较 → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1 匹配 item_01、item_03、item_04、item_06、item_07、item_08、item_10、item_12 → rev2 匹配 item_01、item_03、item_04、item_06、item_07、item_08、item_12 → rev3 匹配 item_01、item_03、item_04、item_06、item_07、item_08、item_12 → rev4 匹配 item_01、item_03、item_04、item_06、item_07、item_08、item_12

#### B5 · `{"field": "price", "op": "ge", "value": 0}`

要点：范围比较悄悄排除布尔、字符串、null 与缺失，不报错

**匹配（8 个）**：item_01、item_04、item_06、item_07、item_09、item_10、item_11、item_12

**被谓词排除（4 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_02` | price 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_03` | price=null（null）：范围比较要求两侧同为数值或同为字符串，目标是数值 → 排除 |
| `item_05` | price=true（布尔）：布尔不参与范围比较 → 排除 |
| `item_08` | price="9.5"（字符串）：范围比较要求两侧同为数值或同为字符串，目标是数值 → 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1 匹配 item_01、item_04、item_06、item_07、item_09、item_10、item_11、item_12 → rev2 匹配 item_01、item_04、item_06、item_07、item_09、item_11、item_12 → rev3 匹配 item_01、item_04、item_06、item_07、item_09、item_10、item_11、item_12 → rev4 匹配 item_01、item_04、item_06、item_07、item_09、item_10、item_11、item_12

#### B6 · `{"field": "price", "op": "lt", "value": 10}`

要点：同上：lt 只看数值，其余类型一律排除

**匹配（6 个）**：item_01、item_04、item_06、item_07、item_11、item_12

**被谓词排除（6 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_02` | price 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_03` | price=null（null）：范围比较要求两侧同为数值或同为字符串，目标是数值 → 排除 |
| `item_05` | price=true（布尔）：布尔不参与范围比较 → 排除 |
| `item_08` | price="9.5"（字符串）：范围比较要求两侧同为数值或同为字符串，目标是数值 → 排除 |
| `item_09` | price=10：数值比较 10 lt 10 不成立 → 排除 |
| `item_10` | price=20.0：数值比较 20.0 lt 10 不成立 → 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1 匹配 item_01、item_04、item_06、item_07、item_10、item_11、item_12 → rev2 匹配 item_01、item_04、item_06、item_07、item_11、item_12 → rev3 匹配 item_01、item_04、item_06、item_07、item_11、item_12 → rev4 匹配 item_01、item_04、item_06、item_07、item_11、item_12

### int/float 与字符串

#### C1 · `{"field": "price", "op": "eq", "value": 4}`

要点：eq 4 匹配 4.0：int/float 在相等中统一

**匹配（2 个）**：item_06、item_07

**被谓词排除（10 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | price=9.5（数值）vs 4（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_02` | price 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_03` | price=null（null）vs 4（数值）：null 只与 null 相等 → eq 排除 |
| `item_04` | price=1（数值）vs 4（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_05` | price=true（布尔）vs 4（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_08` | price="9.5"（字符串）vs 4（数值）：类型不同，不相等 → eq 排除 |
| `item_09` | price=10（数值）vs 4（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_10` | price=20.0（数值）vs 4（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_11` | price=0.0（数值）vs 4（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_12` | price=3.25（数值）vs 4（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### C2 · `{"field": "profile.score", "op": "eq", "value": 10}`

要点：10 与 10.0 都匹配；"10" 与 true 不匹配

**匹配（4 个）**：item_01、item_06、item_07、item_09

**被谓词排除（8 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_02` | profile.score 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_03` | profile.score=null（null）vs 10（数值）：null 只与 null 相等 → eq 排除 |
| `item_04` | profile.score=1（数值）vs 10（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_05` | profile.score=true（布尔）vs 10（数值）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_08` | profile.score="10"（字符串）vs 10（数值）：类型不同，不相等 → eq 排除 |
| `item_10` | profile.score=30（数值）vs 10（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |
| `item_11` | profile.score 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_12` | profile.score=8（数值）vs 10（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### C3 · `{"field": "price", "op": "eq", "value": "9.5"}`

要点：字符串 "9.5" 不等于数值 9.5

**匹配（1 个）**：item_08

**被谓词排除（11 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | price=9.5（数值）vs "9.5"（字符串）：类型不同，不相等 → eq 排除 |
| `item_02` | price 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_03` | price=null（null）vs "9.5"（字符串）：null 只与 null 相等 → eq 排除 |
| `item_04` | price=1（数值）vs "9.5"（字符串）：类型不同，不相等 → eq 排除 |
| `item_05` | price=true（布尔）vs "9.5"（字符串）：布尔与数值/其他类型互不相等（true 不等于 1） → eq 排除 |
| `item_06` | price=4.0（数值）vs "9.5"（字符串）：类型不同，不相等 → eq 排除 |
| `item_07` | price=4.0（数值）vs "9.5"（字符串）：类型不同，不相等 → eq 排除 |
| `item_09` | price=10（数值）vs "9.5"（字符串）：类型不同，不相等 → eq 排除 |
| `item_10` | price=20.0（数值）vs "9.5"（字符串）：类型不同，不相等 → eq 排除 |
| `item_11` | price=0.0（数值）vs "9.5"（字符串）：类型不同，不相等 → eq 排除 |
| `item_12` | price=3.25（数值）vs "9.5"（字符串）：类型不同，不相等 → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### C4 · `{"field": "note", "op": "eq", "value": 9.5}`

要点：未声明类型的 note 里，数值 9.5 与字符串 "9.5" 是两种输入

**匹配（1 个）**：item_08

**被谓词排除（11 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | note="fresh-v2"（字符串）vs 9.5（数值）：类型不同，不相等 → eq 排除 |
| `item_02` | note 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_03` | note=null（null）vs 9.5（数值）：null 只与 null 相等 → eq 排除 |
| `item_04` | note="1"（字符串）vs 9.5（数值）：类型不同，不相等 → eq 排除 |
| `item_05` | note="true"（字符串）vs 9.5（数值）：类型不同，不相等 → eq 排除 |
| `item_06` | note="dup"（字符串）vs 9.5（数值）：类型不同，不相等 → eq 排除 |
| `item_07` | note="dup"（字符串）vs 9.5（数值）：类型不同，不相等 → eq 排除 |
| `item_09` | note=null（null）vs 9.5（数值）：null 只与 null 相等 → eq 排除 |
| `item_10` | note="recreated"（字符串）vs 9.5（数值）：类型不同，不相等 → eq 排除 |
| `item_11` | note=""（字符串）vs 9.5（数值）：类型不同，不相等 → eq 排除 |
| `item_12` | note="nested"（字符串）vs 9.5（数值）：类型不同，不相等 → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### C5 · `{"field": "note", "op": "lt", "value": "2"}`

要点：字符串范围按字典序；数值 note 被静默排除

**匹配（2 个）**：item_04、item_11

**被谓词排除（10 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | note="fresh-v2"：字符串字典序比较 "fresh-v2" lt "2" 不成立 → 排除 |
| `item_02` | note 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_03` | note=null（null）：范围比较要求两侧同为数值或同为字符串，目标是字符串 → 排除 |
| `item_05` | note="true"：字符串字典序比较 "true" lt "2" 不成立 → 排除 |
| `item_06` | note="dup"：字符串字典序比较 "dup" lt "2" 不成立 → 排除 |
| `item_07` | note="dup"：字符串字典序比较 "dup" lt "2" 不成立 → 排除 |
| `item_08` | note=9.5（数值）：范围比较要求两侧同为数值或同为字符串，目标是字符串 → 排除 |
| `item_09` | note=null（null）：范围比较要求两侧同为数值或同为字符串，目标是字符串 → 排除 |
| `item_10` | note="recreated"：字符串字典序比较 "recreated" lt "2" 不成立 → 排除 |
| `item_12` | note="nested"：字符串字典序比较 "nested" lt "2" 不成立 → 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

### 数组与整体值

#### D1 · `{"field": "tags", "op": "contains", "value": "red"}`

要点：contains 做数组成员判断

**匹配（3 个）**：item_01、item_03、item_12

**被谓词排除（9 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_02` | tags=[]：没有成员与 "red" 相等（空数组）→ 排除 |
| `item_04` | tags=[1, 2]：没有成员与 "red" 相等（1:类型不同，不相等；2:类型不同，不相等）→ 排除 |
| `item_05` | tags=[true, false]：没有成员与 "red" 相等（true:布尔与数值/其他类型互不相等（true 不等于 1）；false:布尔与数值/其他类型互不相等（true 不等于 1））→ 排除 |
| `item_06` | tags=["blue"]：没有成员与 "red" 相等（"blue":字符串精确比较）→ 排除 |
| `item_07` | tags=["blue"]：没有成员与 "red" 相等（"blue":字符串精确比较）→ 排除 |
| `item_08` | tags=["9", "10"]：没有成员与 "red" 相等（"9":字符串精确比较；"10":字符串精确比较）→ 排除 |
| `item_09` | tags=[1.0, 2.5]：没有成员与 "red" 相等（1.0:类型不同，不相等；2.5:类型不同，不相等）→ 排除 |
| `item_10` | tags=["legacy", "restocked"]：没有成员与 "red" 相等（"legacy":字符串精确比较；"restocked":字符串精确比较）→ 排除 |
| `item_11` | tags=[]：没有成员与 "red" 相等（空数组）→ 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### D2 · `{"field": "tags", "op": "contains", "value": 1}`

要点：成员比较仍守类型规则：[1,2] 与 [1.0,2.5] 匹配，[true,false] 不匹配

**匹配（2 个）**：item_04、item_09

**被谓词排除（10 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | tags=["red", "sale"]：没有成员与 1 相等（"red":类型不同，不相等；"sale":类型不同，不相等）→ 排除 |
| `item_02` | tags=[]：没有成员与 1 相等（空数组）→ 排除 |
| `item_03` | tags=["red"]：没有成员与 1 相等（"red":类型不同，不相等）→ 排除 |
| `item_05` | tags=[true, false]：没有成员与 1 相等（true:布尔与数值/其他类型互不相等（true 不等于 1）；false:布尔与数值/其他类型互不相等（true 不等于 1））→ 排除 |
| `item_06` | tags=["blue"]：没有成员与 1 相等（"blue":类型不同，不相等）→ 排除 |
| `item_07` | tags=["blue"]：没有成员与 1 相等（"blue":类型不同，不相等）→ 排除 |
| `item_08` | tags=["9", "10"]：没有成员与 1 相等（"9":类型不同，不相等；"10":类型不同，不相等）→ 排除 |
| `item_10` | tags=["legacy", "restocked"]：没有成员与 1 相等（"legacy":类型不同，不相等；"restocked":类型不同，不相等）→ 排除 |
| `item_11` | tags=[]：没有成员与 1 相等（空数组）→ 排除 |
| `item_12` | tags=["red", "blue"]：没有成员与 1 相等（"red":类型不同，不相等；"blue":类型不同，不相等）→ 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### D3 · `{"field": "tags", "op": "contains", "value": true}`

要点：contains true 只认布尔成员

**匹配（1 个）**：item_05

**被谓词排除（11 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | tags=["red", "sale"]：没有成员与 true 相等（"red":布尔与数值/其他类型互不相等（true 不等于 1）；"sale":布尔与数值/其他类型互不相等（true 不等于 1））→ 排除 |
| `item_02` | tags=[]：没有成员与 true 相等（空数组）→ 排除 |
| `item_03` | tags=["red"]：没有成员与 true 相等（"red":布尔与数值/其他类型互不相等（true 不等于 1））→ 排除 |
| `item_04` | tags=[1, 2]：没有成员与 true 相等（1:布尔与数值/其他类型互不相等（true 不等于 1）；2:布尔与数值/其他类型互不相等（true 不等于 1））→ 排除 |
| `item_06` | tags=["blue"]：没有成员与 true 相等（"blue":布尔与数值/其他类型互不相等（true 不等于 1））→ 排除 |
| `item_07` | tags=["blue"]：没有成员与 true 相等（"blue":布尔与数值/其他类型互不相等（true 不等于 1））→ 排除 |
| `item_08` | tags=["9", "10"]：没有成员与 true 相等（"9":布尔与数值/其他类型互不相等（true 不等于 1）；"10":布尔与数值/其他类型互不相等（true 不等于 1））→ 排除 |
| `item_09` | tags=[1.0, 2.5]：没有成员与 true 相等（1.0:布尔与数值/其他类型互不相等（true 不等于 1）；2.5:布尔与数值/其他类型互不相等（true 不等于 1））→ 排除 |
| `item_10` | tags=["legacy", "restocked"]：没有成员与 true 相等（"legacy":布尔与数值/其他类型互不相等（true 不等于 1）；"restocked":布尔与数值/其他类型互不相等（true 不等于 1））→ 排除 |
| `item_11` | tags=[]：没有成员与 true 相等（空数组）→ 排除 |
| `item_12` | tags=["red", "blue"]：没有成员与 true 相等（"red":布尔与数值/其他类型互不相等（true 不等于 1）；"blue":布尔与数值/其他类型互不相等（true 不等于 1））→ 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### D4 · `{"field": "tags", "op": "eq", "value": []}`

要点：eq [] 是整体相等，能匹配空数组；contains 永远匹配不了空数组

**匹配（2 个）**：item_02、item_11

**被谓词排除（10 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | tags=["red", "sale"]（数组）vs []（数组）：数组整体相等：长度相同且逐元素相等 → eq 排除 |
| `item_03` | tags=["red"]（数组）vs []（数组）：数组整体相等：长度相同且逐元素相等 → eq 排除 |
| `item_04` | tags=[1, 2]（数组）vs []（数组）：数组整体相等：长度相同且逐元素相等 → eq 排除 |
| `item_05` | tags=[true, false]（数组）vs []（数组）：数组整体相等：长度相同且逐元素相等 → eq 排除 |
| `item_06` | tags=["blue"]（数组）vs []（数组）：数组整体相等：长度相同且逐元素相等 → eq 排除 |
| `item_07` | tags=["blue"]（数组）vs []（数组）：数组整体相等：长度相同且逐元素相等 → eq 排除 |
| `item_08` | tags=["9", "10"]（数组）vs []（数组）：数组整体相等：长度相同且逐元素相等 → eq 排除 |
| `item_09` | tags=[1.0, 2.5]（数组）vs []（数组）：数组整体相等：长度相同且逐元素相等 → eq 排除 |
| `item_10` | tags=["legacy", "restocked"]（数组）vs []（数组）：数组整体相等：长度相同且逐元素相等 → eq 排除 |
| `item_12` | tags=["red", "blue"]（数组）vs []（数组）：数组整体相等：长度相同且逐元素相等 → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### D5 · `{"field": "profile", "op": "eq", "value": {"flagged": false, "score": 10}}`

要点：对象整体相等：键集相同且递归相等，键序无关（item_07 顺序不同仍匹配）

**匹配（3 个）**：item_01、item_06、item_07

**被谓词排除（9 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_02` | profile={}（对象）vs {"flagged": false, "score": 10}（对象）：对象整体相等：键集相同且递归相等，键序无关 → eq 排除 |
| `item_03` | profile={"score": null}（对象）vs {"flagged": false, "score": 10}（对象）：对象整体相等：键集相同且递归相等，键序无关 → eq 排除 |
| `item_04` | profile={"score": 1}（对象）vs {"flagged": false, "score": 10}（对象）：对象整体相等：键集相同且递归相等，键序无关 → eq 排除 |
| `item_05` | profile={"score": true}（对象）vs {"flagged": false, "score": 10}（对象）：对象整体相等：键集相同且递归相等，键序无关 → eq 排除 |
| `item_08` | profile={"score": "10"}（对象）vs {"flagged": false, "score": 10}（对象）：对象整体相等：键集相同且递归相等，键序无关 → eq 排除 |
| `item_09` | profile={"score": 10.0}（对象）vs {"flagged": false, "score": 10}（对象）：对象整体相等：键集相同且递归相等，键序无关 → eq 排除 |
| `item_10` | profile={"score": 30}（对象）vs {"flagged": false, "score": 10}（对象）：对象整体相等：键集相同且递归相等，键序无关 → eq 排除 |
| `item_11` | profile={}（对象）vs {"flagged": false, "score": 10}（对象）：对象整体相等：键集相同且递归相等，键序无关 → eq 排除 |
| `item_12` | profile={"audit": {"count": 2}, "flagged": true, "score": 8}（对象）vs {"flagged": false, "score": 10}（对象）：对象整体相等：键集相同且递归相等，键序无关 → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### D6 · `{"field": "history", "op": "contains", "value": {"qty": 5}}`

要点：contains 的目标可以是对象：逐成员做整体相等

**匹配（1 个）**：item_12

**被谓词排除（11 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | history 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_02` | history 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_03` | history 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_04` | history 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_05` | history 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_06` | history 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_07` | history 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_08` | history 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_09` | history 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_10` | history 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_11` | history 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### D7 · `{"field": "note", "op": "contains", "value": "e"}`

要点：字符串字段上 contains 是子串判断；非字符串 note 被排除

**匹配（4 个）**：item_01、item_05、item_10、item_12

**被谓词排除（8 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_02` | note 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_03` | note=null（null）：contains 只作用于数组（成员）或字符串（子串）→ 排除 |
| `item_04` | note="1"：子串 "e" 不存在 → 排除 |
| `item_06` | note="dup"：子串 "e" 不存在 → 排除 |
| `item_07` | note="dup"：子串 "e" 不存在 → 排除 |
| `item_08` | note=9.5（数值）：contains 只作用于数组（成员）或字符串（子串）→ 排除 |
| `item_09` | note=null（null）：contains 只作用于数组（成员）或字符串（子串）→ 排除 |
| `item_11` | note=""：子串 "e" 不存在 → 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1 匹配 item_01、item_05、item_12 → rev2 匹配 item_01、item_05、item_12 → rev3 匹配 item_01、item_05、item_10、item_12 → rev4 匹配 item_01、item_05、item_10、item_12

### 嵌套与组合

#### E1 · `{"field": "profile.audit.count", "op": "eq", "value": 2}`

要点：三层点路径；中间层缺失同样按缺失处理

**匹配（1 个）**：item_12

**被谓词排除（11 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | profile.audit.count 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_02` | profile.audit.count 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_03` | profile.audit.count 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_04` | profile.audit.count 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_05` | profile.audit.count 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_06` | profile.audit.count 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_07` | profile.audit.count 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_08` | profile.audit.count 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_09` | profile.audit.count 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_10` | profile.audit.count 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |
| `item_11` | profile.audit.count 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### E2 · `{"field": "qty", "op": "in", "value": [0, 1, 8]}`

要点：in 是逐个候选值的相等判断

**匹配（5 个）**：item_01、item_02、item_04、item_09、item_11

**被谓词排除（7 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_03` | qty=3（数值）：不等于任何候选值 [0, 1, 8] → 排除 |
| `item_05` | qty=7（数值）：不等于任何候选值 [0, 1, 8] → 排除 |
| `item_06` | qty=2（数值）：不等于任何候选值 [0, 1, 8] → 排除 |
| `item_07` | qty=2（数值）：不等于任何候选值 [0, 1, 8] → 排除 |
| `item_08` | qty=12（数值）：不等于任何候选值 [0, 1, 8] → 排除 |
| `item_10` | qty=40（数值）：不等于任何候选值 [0, 1, 8] → 排除 |
| `item_12` | qty=6（数值）：不等于任何候选值 [0, 1, 8] → 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1 匹配 item_02、item_04、item_09、item_11 → rev2 匹配 item_02、item_04、item_09、item_11 → rev3 匹配 item_02、item_04、item_09、item_11 → rev4 匹配 item_01、item_02、item_04、item_09、item_11

#### E3 · `{"and": [{"field": "qty", "op": "ge", "value": 1}, {"field": "active", "op": "eq", "value": true}]}`

要点：and 组合；索引只取其中的等值项，最终谓词仍整体复核

**匹配（7 个）**：item_01、item_03、item_04、item_06、item_07、item_08、item_12

**被谓词排除（5 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_02` | qty=0：数值比较 0 ge 1 不成立 → 排除；active=false（布尔）vs true（布尔）：布尔与布尔比较 → eq 排除 |
| `item_05` | active=false（布尔）vs true（布尔）：布尔与布尔比较 → eq 排除 |
| `item_09` | active=false（布尔）vs true（布尔）：布尔与布尔比较 → eq 排除 |
| `item_10` | active=false（布尔）vs true（布尔）：布尔与布尔比较 → eq 排除 |
| `item_11` | qty=0：数值比较 0 ge 1 不成立 → 排除；active=false（布尔）vs true（布尔）：布尔与布尔比较 → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1 匹配 item_01、item_03、item_04、item_06、item_07、item_08、item_10、item_12 → rev2 匹配 item_01、item_03、item_04、item_06、item_07、item_08、item_12 → rev3 匹配 item_01、item_03、item_04、item_06、item_07、item_08、item_12 → rev4 匹配 item_01、item_03、item_04、item_06、item_07、item_08、item_12

#### E4 · `{"or": [{"field": "qty", "op": "eq", "value": 0}, {"field": "price", "op": "eq", "value": null}]}`

要点：or 组合：qty=0 或 price 为 null

**匹配（3 个）**：item_02、item_03、item_11

**被谓词排除（9 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | qty=1（数值）vs 0（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除；price=9.5（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_04` | qty=1（数值）vs 0（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除；price=1（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_05` | qty=7（数值）vs 0（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除；price=true（布尔）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_06` | qty=2（数值）vs 0（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除；price=4.0（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_07` | qty=2（数值）vs 0（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除；price=4.0（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_08` | qty=12（数值）vs 0（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除；price="9.5"（字符串）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_09` | qty=8（数值）vs 0（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除；price=10（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_10` | qty=40（数值）vs 0（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除；price=20.0（数值）vs null（null）：null 只与 null 相等 → eq 排除 |
| `item_12` | qty=6（数值）vs 0（数值）：数值比较统一 int/float（1 与 1.0 相等） → eq 排除；price=3.25（数值）vs null（null）：null 只与 null 相等 → eq 排除 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）

#### E5 · `{"not": {"field": "tags", "op": "contains", "value": "red"}}`

要点：not 包 contains：取“不含 red”的文档

**匹配（9 个）**：item_02、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11

**被谓词排除（3 个）**：

| 键 | 排除原因 |
| --- | --- |
| `item_01` | tags=["red", "sale"]：成员 "red" 与目标相等 → 匹配 |
| `item_03` | tags=["red"]：成员 "red" 与目标相等 → 匹配 |
| `item_12` | tags=["red", "blue"]：成员 "red" 与目标相等 → 匹配 |

**该修订不可见（与谓词无关）**：`item_13`（该修订时尚未写入）

**历史差异**：rev1 匹配 item_02、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11 → rev2 匹配 item_02、item_04、item_05、item_06、item_07、item_08、item_09、item_11 → rev3 匹配 item_02、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11 → rev4 匹配 item_02、item_04、item_05、item_06、item_07、item_08、item_09、item_10、item_11

## 4. 历史修订对照总表

每个谓词在四个修订上的匹配键（只写键后缀，如 `01 03` = item_01、item_03）：

| 谓词 | rev1 | rev2 | rev3 | rev4 |
| --- | --- | --- | --- | --- |
| A1 | 01 03 04 05 06 07 08 09 10 11 12 | 01 03 04 05 06 07 08 09 11 12 | 01 03 04 05 06 07 08 09 10 11 12 | 01 03 04 05 06 07 08 09 10 11 12 |
| A2 | 02 | 02 | 02 | 02 |
| A3 | 03 | 03 | 03 | 03 |
| A4 | 01 04 05 06 07 08 09 10 11 12 | 01 04 05 06 07 08 09 11 12 | 01 04 05 06 07 08 09 10 11 12 | 01 04 05 06 07 08 09 10 11 12 |
| A5 | 01 02 04 05 06 07 08 09 10 11 12 | 01 02 04 05 06 07 08 09 11 12 | 01 02 04 05 06 07 08 09 10 11 12 | 01 02 04 05 06 07 08 09 10 11 12 |
| B1 | 04 | 04 | 04 | 01 04 |
| B2 | — | — | — | — |
| B3 | — | — | — | — |
| B4 | 01 03 04 06 07 08 10 12 | 01 03 04 06 07 08 12 | 01 03 04 06 07 08 12 | 01 03 04 06 07 08 12 |
| B5 | 01 04 06 07 09 10 11 12 | 01 04 06 07 09 11 12 | 01 04 06 07 09 10 11 12 | 01 04 06 07 09 10 11 12 |
| B6 | 01 04 06 07 10 11 12 | 01 04 06 07 11 12 | 01 04 06 07 11 12 | 01 04 06 07 11 12 |
| C1 | 06 07 | 06 07 | 06 07 | 06 07 |
| C2 | 01 06 07 09 | 01 06 07 09 | 01 06 07 09 | 01 06 07 09 |
| C3 | 08 | 08 | 08 | 08 |
| C4 | 08 | 08 | 08 | 08 |
| C5 | 04 11 | 04 11 | 04 11 | 04 11 |
| D1 | 01 03 12 | 01 03 12 | 01 03 12 | 01 03 12 |
| D2 | 04 09 | 04 09 | 04 09 | 04 09 |
| D3 | 05 | 05 | 05 | 05 |
| D4 | 02 11 | 02 11 | 02 11 | 02 11 |
| D5 | 01 06 07 | 01 06 07 | 01 06 07 | 01 06 07 |
| D6 | 12 | 12 | 12 | 12 |
| D7 | 01 05 12 | 01 05 12 | 01 05 10 12 | 01 05 10 12 |
| E1 | 12 | 12 | 12 | 12 |
| E2 | 02 04 09 11 | 02 04 09 11 | 02 04 09 11 | 01 02 04 09 11 |
| E3 | 01 03 04 06 07 08 10 12 | 01 03 04 06 07 08 12 | 01 03 04 06 07 08 12 | 01 03 04 06 07 08 12 |
| E4 | 02 03 11 | 02 03 11 | 02 03 11 | 02 03 11 |
| E5 | 02 04 05 06 07 08 09 10 11 | 02 04 05 06 07 08 09 11 | 02 04 05 06 07 08 09 10 11 | 02 04 05 06 07 08 09 10 11 |

可见性背景：item_10 在 rev2 为墓碑（任何谓词都匹配不到）；item_13 在 rev1–4 尚未写入。修订间结果差异只来自三类原因：文档被更新（item_01）、删除/重建（item_10）、可见性本身。

## 5. 索引使用（执行方式，不改变结果含义）

已建索引：`by_qty(qty)`、`by_active(active)`、`by_score(profile.score)`，均非唯一。以下对照在 rev4（建电池时的 head）实测：

| 谓词 | 访问方式 | 使用索引 | 检查行数 | 全扫检查行数 | 两种执行结果一致 |
| --- | --- | --- | --- | --- | --- |
| A1 | version_scan | — | 12 | 12 | ✓ |
| A2 | version_scan | — | 12 | 12 | ✓ |
| A3 | version_scan | — | 12 | 12 | ✓ |
| A4 | version_scan | — | 12 | 12 | ✓ |
| A5 | version_scan | — | 12 | 12 | ✓ |
| B1 | equality_index | by_qty | 2 | 12 | ✓ |
| B2 | equality_index | by_qty | 0 | 12 | ✓ |
| B3 | equality_index | by_active | 0 | 12 | ✓ |
| B4 | equality_index | by_active | 7 | 12 | ✓ |
| B5 | version_scan | — | 12 | 12 | ✓ |
| B6 | version_scan | — | 12 | 12 | ✓ |
| C1 | version_scan | — | 12 | 12 | ✓ |
| C2 | equality_index | by_score | 4 | 12 | ✓ |
| C3 | version_scan | — | 12 | 12 | ✓ |
| C4 | version_scan | — | 12 | 12 | ✓ |
| C5 | version_scan | — | 12 | 12 | ✓ |
| D1 | version_scan | — | 12 | 12 | ✓ |
| D2 | version_scan | — | 12 | 12 | ✓ |
| D3 | version_scan | — | 12 | 12 | ✓ |
| D4 | version_scan | — | 12 | 12 | ✓ |
| D5 | version_scan | — | 12 | 12 | ✓ |
| D6 | version_scan | — | 12 | 12 | ✓ |
| D7 | version_scan | — | 12 | 12 | ✓ |
| E1 | version_scan | — | 12 | 12 | ✓ |
| E2 | version_scan | — | 12 | 12 | ✓ |
| E3 | equality_index | by_active | 7 | 12 | ✓ |
| E4 | version_scan | — | 12 | 12 | ✓ |
| E5 | version_scan | — | 12 | 12 | ✓ |

读法：

- `equality_index` 只表示**候选行来自索引投影**；每一行仍用完整谓词复核，所以两种执行方式的结果逐键相同（上表最后一列，脚本亦断言，不一致即中止生成）。
- 只有谓词顶层的 `eq` 项（含 `and` 之下的）且字段建有索引、目标值是标量时才走索引；范围、成员、整体对象相等（如 D5）与无索引字段（如 price）都回退 `version_scan`。
- B3（`active eq 1`）走了索引但检查 0 行：数值 token `n:1` 在布尔索引里不存在，索引层面就编码了“true 不等于 1”。
- E3 只有 `active eq true` 是等值项：索引先缩到 7 行候选，`qty ge 1` 在复核阶段生效。
- **历史修订不用索引**：rev1–3 的全部 28 个谓词实测均为 `version_scan`（见 `results/predicates.json` 中各 run 的 plan），索引只服务当前 head。

标量索引 token 规则（`strataleaf.codec.scalar_token` 实测）：

| 值 | token | 说明 |
| --- | --- | --- |
| `null` | `v:null` | null 是可索引标量（eq null 可走索引） |
| `true` | `b:1` | 布尔有独立命名空间 b:，与数值不相交 |
| `false` | `b:0` | 布尔有独立命名空间 b:，与数值不相交 |
| `0` | `n:0` | 数值命名空间 n:，整数直接表示 |
| `1` | `n:1` | 数值命名空间 n:，整数直接表示 |
| `1.0` | `n:1` | 整数值浮点与整数 1 共用同一 token n:1 |
| `2.5` | `n:2.5` | 非整浮点保留完整表示 |
| `"1"` | `v:"1"` | 字符串命名空间 v:，与数值/布尔不相交 |
| `"true"` | `v:"true"` | 字符串命名空间 v:，与数值/布尔不相交 |
| `"red"` | `v:"red"` | 字符串命名空间 v:，与数值/布尔不相交 |
| `""` | `v:""` | 字符串命名空间 v:，与数值/布尔不相交 |
| `[]` | （无，不索引） | 数组与对象不产生 token，不可索引（eq 对象/数组只能全扫） |
| `[1]` | （无，不索引） | 数组与对象不产生 token，不可索引（eq 对象/数组只能全扫） |
| `{}` | （无，不索引） | 数组与对象不产生 token，不可索引（eq 对象/数组只能全扫） |
| `{"a": 1}` | （无，不索引） | 数组与对象不产生 token，不可索引（eq 对象/数组只能全扫） |

唯一索引：对 `sku` 建唯一索引 `uniq_sku` 被拒绝（`Conflict: existing documents violate unique index`）——item_06 与 item_07 内容相同、键不同，唯一约束在现有数据上即冲突；缺失字段与对象/数组不进入索引，null 作为标量参与唯一性。

深度自检：`diagnostics.inspect(True)` → ok=True，issues=[]，head=5，floor=0（含索引投影与当前文档的逐条重算核对）。

## 6. 分页与游标

谓词 `null`（匹配全部）、页大小 5，首页读修订 rev4；翻页期间写入 `item_13`（rev5）：

| 页 | 键 | 读修订 | 签发后续游标 |
| --- | --- | --- | --- |
| 1 | item_01、item_02、item_03、item_04、item_05 | rev4 | 是 |
| 2 | item_06、item_07、item_08、item_09、item_10 | rev4 | 是 |
| 3 | item_11、item_12 | rev4 | 否 |

三页均未出现 `item_13`：游标把读视图固定在首页的 rev4。重新不带游标查询（rev5）则返回 13 个键，包含 `item_13`。游标只固定读视图，不是保留租约；跨长时操作应自行 `retention.pin`。

## 7. 观测到的错误（实际 API 拒绝）

| 编号 | 场景 | 错误 | 消息 |
| --- | --- | --- | --- |
| E1 | 用 expected=0 重建已删除的键（0 只允许从未出现过的键） | `Conflict` | revision conflict for items/item_10: expected 0, found 2 |
| E2 | item_06/item_07 内容相同（sku 同为 A-06）时建唯一索引 uniq_sku | `Conflict` | existing documents violate unique index |
| E3 | 读取超过 head 的修订 | `InvalidDocument` | read revision must be between zero and current head |
| E4 | 查询不存在的集合 | `NotFound` | unknown collection |
| E5 | 谓词使用未定义的操作符 like | `InvalidDocument` | predicate must have field, op and value |
| E6 | 页大小 limit=0 | `InvalidDocument` | page size must be between 1 and 1000 |
| E7 | in 的候选值不是数组 | `InvalidDocument` | in requires an array |
| E8 | 把首页游标用于另一个谓词（游标绑定谓词摘要） | `InvalidCursor` | cursor does not belong to this query/read revision |

## 8. 文件清单

| 文件 | 内容 |
| --- | --- |
| `data/dataset.json` | 集合模式、12+1 个文档及各自设计目的 |
| `results/timeline.json` | rev0–5 事件、item_10/item_01/item_13 版本链（实际 get 结果） |
| `results/predicates.json` | 28 谓词 × 4 修订 × 13 键完整矩阵：plan、匹配键、逐键评估树 |
| `results/index_usage.json` | 索引对照、标量 token 规则、唯一索引冲突、深度自检 |
| `results/errors.json` | 8 个实际 API 拒绝的类型与消息 |
| `results/pagination.json` | 游标分页全过程与翻页期间写入的可见性 |
| `scripts/build_materials.py` | 生成脚本；重跑即在全新临时库上重建全部材料 |

