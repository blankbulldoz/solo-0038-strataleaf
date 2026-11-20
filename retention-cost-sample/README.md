# 保留历史的实际代价——离线样本材料

本目录是面向非开发同事的说明材料，**不包含任何 StrataLeaf 产品功能改动**，
全部内容由 `build_sample.py` 调用仓库现有公开 API 真实运行后生成。

## 离线阅读顺序

1. `report.md`：主报告，从背景概念到证据清单共 12 节，可直接分发或打印。
2. `evidence/queries/`：报告中每个数字对应的机器可读 JSON 证据。
3. `evidence/snapshots/`：四个回收阶段（stage0–stage3）的真实 SQLite 数据库快照，
   可用任何 SQLite 工具以只读方式打开核对。
4. `evidence/live/`：复跑结束时的两个活动库（主样本库与空间探针库）。

## 重新生成（无需联网）

在仓库根目录执行：

```
python3 -I retention-cost-sample/build_sample.py
```

脚本会清空并重建 `evidence/` 下的全部产物，再重写 `report.md`。
仅依赖 Python 3.11 标准库与本仓库的 `strataleaf` 包，运行约一秒。

## 数字边界

样本刻意保持小规模（1 个集合、4 个业务文档、7 个业务提交；探针库 1500 行仅用于展示页复用机制），
只用于解释 pin / 消费者位置 / 回收水位 / TTL 的语义与代价，
**不得引用其中任何数字作为生产环境的性能、容量或压缩率结论**。
