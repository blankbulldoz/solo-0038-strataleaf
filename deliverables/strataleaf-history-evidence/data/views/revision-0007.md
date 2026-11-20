# 历史读视图 · 读修订 7

> 本文件是 `data/views/revision-0007.json` 的可读渲染，不是独立事实源；如有差异以 JSON 与 `manifest.json` 摘要为准。

- `record_kind`: `historical_read_view`（历史读视图）
- 集合：`segments`
- 读修订：7
- 来源 API：`Database.query(collection, revision=read_revision, limit=1000)`
- 可见文档数：4
- 内容指纹：`c91f987538b3ba5bda72b9d169001c4d61fd2d8c8276b56b5de3627ed3486f4d`

## 可见文档

| 文档键 | actual_version | 内容哈希（前 12 位） | 文档 |
| --- | --- | --- | --- |
| `seg_center_06` | 7 | `cc02234e3a34` | `{"label": "updated", "region": "center", "sequence": 66}` |
| `seg_east_01` | 3 | `2de3b9f45adc` | `{"label": "baseline", "region": "east", "sequence": 1}` |
| `seg_legacy_05` | 3 | `15246680325c` | `{"label": "revised", "region": "east", "sequence": 55}` |
| `seg_west_02` | 5 | `94c7470029dc` | `{"label": "baseline", "region": "west", "sequence": 2}` |

## 本修订不可见的键

这些键要么尚未创建，要么被墓碑隐藏；逐键原因见 `data/document-inventory.json` 的 `visibility_in_selected_views`。

- `seg_north_03`
- `seg_south_04`
