# StrataLeaf 查询写法选择材料

这套材料帮助调用方选择查询写法：用一份精心设计的示例数据集，通过 StrataLeaf
**生产 API 实际运行** 28 个谓词，在当前与历史修订上记录完整结果矩阵，并把
**结果含义（语义）** 与 **索引使用（执行方式）** 分开解释。

材料全部在仓库之外生成与存放，不修改仓库内任何文件。

## 阅读入口

1. 先读 `REPORT.md` —— 实测报告，全部结论都在这里：
   - 第 1–2 节：数据集与修订时间线（每个文档针对哪对易混概念）。
   - 第 3 节：**结果含义**。每个谓词给出匹配键与**完整排除清单**——被排除的
     每个键都带原因（缺失 / null / 类型隔离 / 值比较 / 可见性），可以逐键判断
     一条条件究竟排除了哪些输入。
   - 第 4 节：28 谓词 × 4 修订的匹配对照总表（删除→重建、补丁的影响一目了然）。
   - 第 5 节：**索引使用**。plan、检查行数、标量 token 规则、唯一索引冲突；
     强调索引只改变候选规模、不改变结果（两种执行方式逐键核对一致）。
   - 第 6–7 节：游标分页的读视图固定、8 个实际 API 拒绝。
2. 需要逐键逐修订的完整数据时查 `results/predicates.json`
   （28 谓词 × 4 修订 × 13 键，含每个文档的逐叶子评估树）。

## 目录结构

```
data/dataset.json        集合模式与 12+1 个文档及各自设计目的
results/timeline.json    rev0–5 事件与 item_10/item_01/item_13 版本链（实际 get 结果）
results/predicates.json  完整谓词结果矩阵（含 plan 与逐键评估树）
results/index_usage.json 索引对照、标量 token 规则、唯一索引冲突、深度自检
results/errors.json      8 个实际 API 拒绝的类型与消息
results/pagination.json  游标分页全过程（翻页期间写入新文档）
scripts/build_materials.py  生成脚本
REPORT.md                实测报告（生成物）
```

## 复现

```bash
python3 -I scripts/build_materials.py --repo /workspace
```

每次运行在全新临时数据库上重建并覆盖 `data/`、`results/`、`REPORT.md`；
`--repo` 指向 StrataLeaf 源码根目录（默认 `/workspace`），`--out` 可改输出目录。

## 可信度约定

- 所有结果来自真实 API 调用；生成脚本对每个“文档 × 谓词 × 修订”单元格做
  三重核对（查询 API 返回 == 库内 `strataleaf.predicates.matches` == 脚本内
  解释器），任一不一致即中止，不会产出半成品材料。
- 数据库文件本身是临时的；材料只保留逻辑结果（文档、修订号、plan、键集合），
  不保留每次运行随机生成的数据库标识与游标签名。
