# 历史读视图 · 读修订 1

> 本文件是 `data/views/revision-0001.json` 的可读渲染，不是独立事实源；如有差异以 JSON 与 `manifest.json` 摘要为准。

- `record_kind`: `historical_read_view`（历史读视图）
- 集合：`segments`
- 读修订：1
- 来源 API：`Database.query(collection, revision=read_revision, limit=1000)`
- 可见文档数：5
- 内容指纹：`dfabfb5efe75c1d27e7983fc429244ce96a3963eb42b12ea99597345a7224fa2`

## 可见文档

| 文档键 | actual_version | 内容哈希（前 12 位） | 文档 |
| --- | --- | --- | --- |
| `seg_east_01` | 1 | `2de3b9f45adc` | `{"label": "baseline", "region": "east", "sequence": 1}` |
| `seg_legacy_05` | 1 | `19b681c9027d` | `{"label": "baseline", "region": "east", "sequence": 5}` |
| `seg_north_03` | 1 | `952f156a2bb8` | `{"label": "baseline", "region": "north", "sequence": 3}` |
| `seg_south_04` | 1 | `86c3e0a6067f` | `{"label": "baseline", "region": "south", "sequence": 4}` |
| `seg_west_02` | 1 | `94c7470029dc` | `{"label": "baseline", "region": "west", "sequence": 2}` |

## 本修订不可见的键

这些键要么尚未创建，要么被墓碑隐藏；逐键原因见 `data/document-inventory.json` 的 `visibility_in_selected_views`。

- `seg_center_06`
