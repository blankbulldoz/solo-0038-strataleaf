# 文档键—实际版本—所属提交清单

> 本文件是 `data/document-inventory.json` 的可读渲染，属于派生索引，不是独立事实源。

`actual_version` 是该键的版本号；`commit` 是产生它的提交修订与 request_key。两者在本库中数值相同，但含义不同。

| 文档键 | actual_version | commit 修订 | commit request_key | 操作 | 状态 | 内容哈希（前 12 位） |
| --- | --- | --- | --- | --- | --- | --- |
| `seg_east_01` | 1 | 1 | `seed_baseline` | `put` | created | `2de3b9f45adc` |
| `seg_east_01` | 2 | 2 | `revise_alpha_north` | `put` | rewritten | `b507b74e3952` |
| `seg_east_01` | 3 | 3 | `restore_alpha_revise_legacy` | `put` | rewritten | `2de3b9f45adc` |
| `seg_west_02` | 1 | 1 | `seed_baseline` | `put` | created | `94c7470029dc` |
| `seg_west_02` | 4 | 4 | `retire_west` | `delete` | deleted | `（删除）` |
| `seg_west_02` | 5 | 5 | `recreate_west` | `put` | recreated | `94c7470029dc` |
| `seg_north_03` | 1 | 1 | `seed_baseline` | `put` | created | `952f156a2bb8` |
| `seg_north_03` | 2 | 2 | `revise_alpha_north` | `put` | rewritten | `d4e50d82b8a8` |
| `seg_north_03` | 7 | 7 | `final_touch` | `delete` | deleted | `（删除）` |
| `seg_south_04` | 1 | 1 | `seed_baseline` | `put` | created | `86c3e0a6067f` |
| `seg_south_04` | 6 | 6 | `retire_south_add_center` | `delete` | deleted | `（删除）` |
| `seg_legacy_05` | 1 | 1 | `seed_baseline` | `put` | created | `19b681c9027d` |
| `seg_legacy_05` | 3 | 3 | `restore_alpha_revise_legacy` | `put` | rewritten | `15246680325c` |
| `seg_center_06` | 6 | 6 | `retire_south_add_center` | `put` | created | `27bec1aa653f` |
| `seg_center_06` | 7 | 7 | `final_touch` | `put` | rewritten | `cc02234e3a34` |
