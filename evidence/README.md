# 演练证据

`drill-run/` 是一次**真实执行**的产物（2026-11-20），不是手工整理的样例。它由以下命令生成：

```bash
python3 -m ops.dr drill --root /workspace/evidence/drill-run --writer-seconds 3.0
python3 -m ops.dr verify-evidence --root /workspace/evidence/drill-run
```

## 目录

| 路径 | 内容 |
| --- | --- |
| `evidence/drill-report.json` | 全量机器可读证据：每个场景的事实、检查项与事件。 |
| `evidence/drill-report.md` | 同上的人读版本。 |
| `evidence/artifacts.sha256` | 下列所有数据库文件的 SHA-256。 |
| `primary/service.db` | 演练中持续被写入的主库（唯一事实来源）。 |
| `backups/drill1-*.db` + `.manifest.json` | 写入期间取得的归档及其清单。 |
| `backups/raw-copy-suspect.db` | **反例**：活动主文件的裸拷贝，工具拒收，且单独不可读。 |
| `backups/recovery-*.db` + `.manifest.json` | 历史回收后用于重新引导的新归档。 |
| `backups/occupied-store/keep-me-a1.db` | 备份失败注入：被占用目标，内容未被覆盖。 |
| `backups/occupied-store/keep-me-retry1-*.db` | 同一注入下改名为新目标后成功的归档。 |
| `restore/primary-restored.db` | 由归档恢复到隔离目录的新主库（身份已轮换）。 |
| `replica/service-replica.db` | 接收后续提交的副本（含中断续传）。 |
| `replica/stale/stale-replica.db` | 因源历史已回收而无法增量接收的陈旧副本。 |
| `replica/rebootstrapped.db` | 用新归档重新引导并追平的副本。 |

## 复核

不信任报告时，独立重算摘要、从清单复核归档、并按归档事实重推恢复与最终比对：

```bash
python3 -m ops.dr verify-evidence --root evidence/drill-run
```

预期退出码 0 且输出 `"ok": true`。

`*.db-wal` / `*.db-shm` 为易失侧车，已被 `.gitignore` 排除；归档在发布时已断言自包含（无依赖的非空 WAL）。
