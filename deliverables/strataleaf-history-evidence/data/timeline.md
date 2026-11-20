# 变更过程时间线

> 本文件是 `data/change-process.json` 的可读渲染，不是历史读视图；它按提交列出操作，不表示某个修订下的完整可见集合。

- `record_kind`: `change_process`（变更过程）
- 来源 API：`Database.feed.read(since=0, limit=1000)`

## 提交修订 1 · `seed_baseline`

| # | 操作 | 集合 | 键 | expected | 内容哈希（前 12 位） |
| --- | --- | --- | --- | --- | --- |
| 0 | `put` | `segments` | `seg_east_01` | 0 | `2de3b9f45adc` |
| 1 | `put` | `segments` | `seg_west_02` | 0 | `94c7470029dc` |
| 2 | `put` | `segments` | `seg_north_03` | 0 | `952f156a2bb8` |
| 3 | `put` | `segments` | `seg_south_04` | 0 | `86c3e0a6067f` |
| 4 | `put` | `segments` | `seg_legacy_05` | 0 | `19b681c9027d` |

## 提交修订 2 · `revise_alpha_north`

| # | 操作 | 集合 | 键 | expected | 内容哈希（前 12 位） |
| --- | --- | --- | --- | --- | --- |
| 0 | `put` | `segments` | `seg_east_01` | 1 | `b507b74e3952` |
| 1 | `put` | `segments` | `seg_north_03` | 1 | `d4e50d82b8a8` |

## 提交修订 3 · `restore_alpha_revise_legacy`

| # | 操作 | 集合 | 键 | expected | 内容哈希（前 12 位） |
| --- | --- | --- | --- | --- | --- |
| 0 | `put` | `segments` | `seg_east_01` | 2 | `2de3b9f45adc` |
| 1 | `put` | `segments` | `seg_legacy_05` | 1 | `15246680325c` |

## 提交修订 4 · `retire_west`

| # | 操作 | 集合 | 键 | expected | 内容哈希（前 12 位） |
| --- | --- | --- | --- | --- | --- |
| 0 | `delete` | `segments` | `seg_west_02` | 1 | `（删除）` |

## 提交修订 5 · `recreate_west`

| # | 操作 | 集合 | 键 | expected | 内容哈希（前 12 位） |
| --- | --- | --- | --- | --- | --- |
| 0 | `put` | `segments` | `seg_west_02` | 4 | `94c7470029dc` |

## 提交修订 6 · `retire_south_add_center`

| # | 操作 | 集合 | 键 | expected | 内容哈希（前 12 位） |
| --- | --- | --- | --- | --- | --- |
| 0 | `delete` | `segments` | `seg_south_04` | 1 | `（删除）` |
| 1 | `put` | `segments` | `seg_center_06` | 0 | `27bec1aa653f` |

## 提交修订 7 · `final_touch`

| # | 操作 | 集合 | 键 | expected | 内容哈希（前 12 位） |
| --- | --- | --- | --- | --- | --- |
| 0 | `delete` | `segments` | `seg_north_03` | 2 | `（删除）` |
| 1 | `put` | `segments` | `seg_center_06` | 6 | `cc02234e3a34` |
