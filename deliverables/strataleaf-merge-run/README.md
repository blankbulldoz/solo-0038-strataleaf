# 双来源逻辑文档合并 — 运行产物

本目录全部在仓库外；仓库本身未被修改（`git status` 为空）。所有数据库读写只调用
StrataLeaf 既有的 `Database.transfer.export_lines` / `Database.transfer.import_lines`，
没有新增关联或合并 API。

## 目录

| 路径 | 内容 |
| --- | --- |
| `inputs/source_a.jsonl`, `inputs/source_b.jsonl` | 由两个真实源库 `transfer.export_lines` 导出的输入 |
| `inputs/source_*.manifest.json` | 源库身份、导出修订、导出摘要、逻辑指纹 |
| `merged/merged.jsonl` | 整理后的键空间，可直接交给 `import_lines` |
| `merged/source_map.jsonl` | 每条文档的 来源 → 合并键 映射及引用改写明细 |
| `merged/conflicts.json` | 重名键、引用改写、无法可靠判断的引用清单 |
| `merged/import_report.json` | 导入回执修订、实际计数、指纹、诊断结果 |
| `merged/merged.db` | 通过既有导入 API 写入的新库 |
| `merged/verification.json` | 独立读取该库后逐项核对的 17 项结果 |
| `scripts/build_sources.py` | 建两个源库并导出真实输入 |
| `scripts/merge_sources.py` | 键空间整理 + 引用改写 + 冲突清单 + 导入 |
| `scripts/verify_merge.py` | 重新打开合并库核对关系与来源映射 |

## 复现

```bash
python3 -I /tmp/strataleaf-merge-run/scripts/build_sources.py
python3 -I /tmp/strataleaf-merge-run/scripts/merge_sources.py
python3 -I /tmp/strataleaf-merge-run/scripts/verify_merge.py   # 退出码 0
```

脚本以只读方式从 `/workspace` 导入项目源码；数据与输出都写在本目录。

## 键空间规则

`merged key = 来源前缀 + 原键`，前缀为 `a__` / `b__`。两个来源的全部文档都保留，
重名键不会互相覆盖，也不做跨来源文档合并。声明为引用的字段
（`links.parent`、`links.peer`）只在本来源的键集合内解析并改写；若目标不在本来源，
保留原文本并在 `conflicts.json` 中列为无法可靠判断，绝不改指另一来源的同名键。
