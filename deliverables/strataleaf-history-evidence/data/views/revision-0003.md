# 历史读视图 · 读修订 3

> 本文件是 `data/views/revision-0003.json` 的可读渲染，不是独立事实源；如有差异以 JSON 与 `manifest.json` 摘要为准。

- `record_kind`: `historical_read_view`（历史读视图）
- 集合：`segments`
- 读修订：3
- 来源 API：`Database.query(collection, revision=read_revision, limit=1000)`
- 可见文档数：5
- 内容指纹：`8da87a3b61320ef814e537520cf43a2c17e89c917b97d08bc2be143aafab46ba`

## 可见文档

| 文档键 | actual_version | 内容哈希（前 12 位） | 文档 |
| --- | --- | --- | --- |
| `seg_east_01` | 3 | `2de3b9f45adc` | `{"label": "baseline", "region": "east", "sequence": 1}` |
| `seg_legacy_05` | 3 | `15246680325c` | `{"label": "revised", "region": "east", "sequence": 55}` |
| `seg_north_03` | 2 | `d4e50d82b8a8` | `{"label": "revised", "region": "north", "sequence": 33}` |
| `seg_south_04` | 1 | `86c3e0a6067f` | `{"label": "baseline", "region": "south", "sequence": 4}` |
| `seg_west_02` | 1 | `94c7470029dc` | `{"label": "baseline", "region": "west", "sequence": 2}` |

## 本修订不可见的键

这些键要么尚未创建，要么被墓碑隐藏；逐键原因见 `data/document-inventory.json` 的 `visibility_in_selected_views`。

- `seg_center_06`
