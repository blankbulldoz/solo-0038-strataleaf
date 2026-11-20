# 单机灾备运维手册

面向嵌入 StrataLeaf 的 Python 服务值班人员。所有命令在仓库根目录执行，业务库实现保持不变，工具只使用公开 API。

## 0. 术语与事实

一次恢复是否「成立」由下列事实共同决定，**任意一项不等都不能宣布接收成功**：

- 归档：SHA-256、`revision`、`floor`、`database_id`；
- 目录：集合名、模式、代数（`catalog_epoch`）；
- 索引：索引**定义**摘要 + 由可见文档**重算**的物化投影；
- 文档：指定修订下的键、版本、内容、到期事实的合并摘要与数量；
- 提交日志在保留范围内连续；
- SQLite 物理完整性与外键无违规；
- 身份：新主库必须轮换 `database_id`；副本必须与上游一致。

`head` 相等只是必要条件之一。

## 1. 日常：定期备份

```bash
python3 -m ops.dr backup \
  --source /srv/app/service.db \
  --store  /srv/backups \
  --label  nightly
```

- 通过 `Database.archives.backup`（SQLite 在线备份 API）生成，发布到**未占用**目标；
- 成功后写 `<归档名>.manifest.json`，内含方法、摘要、水位、目录/索引/文档事实与每次尝试记录；
- 失败会重试（每次换新名字），全部失败则写 `<前缀>.failed.manifest.json` 并以非零码退出；**不会**发布半成品；
- 工具会拒收「旁边有非空 WAL 侧车」的归档，并报告遗留的 `.strataleaf-*` 临时文件。

**不要**用 `cp`/`rsync` 复制活动 `service.db` 当备份。需要说明时运行：

```bash
python3 -m ops.dr reject-copy --path /srv/backups/suspect.db
python3 -m ops.dr inspect     --path /srv/backups/suspect.db
```

## 2. 定期：复核归档

```bash
python3 -m ops.dr verify --archive /srv/backups/nightly-20260101T000000Z-a1.db
```

从清单重算归档摘要与全部逻辑事实。非零退出即代表归档不可用，按第 6 节处置。

## 3. 灾难：恢复为新主库（隔离目录）

```bash
mkdir -p /srv/recovery
python3 -m ops.dr restore \
  --archive     /srv/backups/nightly-20260101T000000Z-a1.db \
  --destination /srv/recovery/service-$(date -u +%Y%m%dT%H%M%SZ).db \
  --mode primary
```

`restore` 会：先校验归档摘要 → 用库 API 恢复到新路径（拒绝覆盖已存在文件）→ 重算并比对全部事实 → 断言 `database_id` 已轮换、`mode=primary`、`upstream` 清空。**只有 `ok: true` 才可继续。**

恢复后业务进程改连新文件；旧游标、消费者、确认位置均失效，需按业务重新注册：

```python
# 业务侧示意；确认位置应从恢复点重新判定
with Database("/srv/recovery/service-....db") as db:
    start = db.revision          # 或按业务语义选择
    db.feed.register("worker", start)
```

## 4. 恢复为副本并接收后续变化

当需要「以某归档为基线、再接收主库后续提交」时：

```bash
# 4.1 以归档引导一个副本（绑定原主库身份）
python3 -c "from strataleaf import Archives; \
  Archives.restore('/srv/backups/b.db','/srv/recovery/replica.db','<sha256>',follower=True)"

# 4.2 持续接收（可反复执行；到当前 head 即停）
python3 -m ops.dr catch-up \
  --source  /srv/app/service.db \
  --replica /srv/recovery/replica.db
```

接收语义（由库保证，工具记录）：

- 只接受**同一上游、连续**的提交包；重复范围不重复写入；缺口、摘要错误、来源不符整包拒绝；
- 分段追赶期间副本暂不提供当前索引，追到导出端 head 才安装最新索引，避免中间历史视图误触发唯一约束；
- 副本**拒绝**主库写入（`ReadOnly`）；
- 输出 `status`：`caught_up` / `interrupted` / `partial` / `requires_rebootstrap` / `failed`。后两者见第 6 节。

防止「追赶期间源被回收」：在开始长追赶前对源加保留点，追平后再释放（演练即如此做）：

```python
with Database("/srv/app/service.db") as db:
    db.retention.pin("dr_catchup", db.revision)
# ... 完成接收 ...
with Database("/srv/app/service.db") as db:
    db.retention.unpin("dr_catchup")
```

## 5. 演练（可重复、留证据）

```bash
rm -rf /srv/drills/run-$(date -u +%Y%m%d)
python3 -m ops.dr drill --root /srv/drills/run-$(date -u +%Y%m%d)
```

真实执行：持续写入 + 写入期间在线备份 + 隔离恢复核对 + 副本接收（含中断续传、重复包、篡改包）+ 源历史回收 + 重新引导 + 备份失败注入 + 裸拷贝对照。产物：

- `evidence/drill-report.json`：机器可读全量证据；
- `evidence/drill-report.md`：人读报告（含恢复点/滞后/边界）；
- `evidence/artifacts.sha256`：所有数据库/清单/报告的摘要。

第三方**不信任报告**时的独立复核：

```bash
python3 -m ops.dr verify-evidence --root /srv/drills/run-YYYYMMDD
```

它复算 `artifacts.sha256`、从清单复核每个归档、并按归档事实重推恢复/最终比对。退出码非零即证据不可信。

## 6. 故障处置

### 6.1 备份失败（存储满 / 只读 / 目标被占用）
1. 看备份仓里的 `<前缀>.failed.manifest.json` 与工具输出的 `attempts`；
2. 目标被占用时工具自动改名重试，**原文件保持不变**；确认后清理或换 `--store`；
3. 存储只读或空间不足：修复权限/扩容后重跑，不要手工搬运活动主文件。

### 6.2 接收中断
`catch-up` 返回 `interrupted` 或进程被杀：
1. 用 `inspect` 读副本当前 `head` 与 `floor`；
2. 直接重跑 `catch-up`（幂等，重复包不重复写）；
3. 若源在中断期间被回收（见 6.3），转重新引导。

### 6.3 源历史已回收（`requires_rebootstrap`）
症状：`catch-up` 事件里 `reclaimed`，或直接 `SnapshotTooOld: revision N is older than retained floor M`。
原因：源的 `floor` 已越过副本所需修订，增量历史不可得。
处置：
1. 立即对源做一份**新备份**并 `verify`；
2. 用新归档重新引导副本（`Archives.restore(..., follower=True)`）；
3. 再 `catch-up` 追上；
4. 复盘为何保留点/消费者未覆盖该副本（本工具演练用 pin 保护，生产应长期保留一个接收专用 pin）。

### 6.4 校验失败 / 包被篡改
库以 `ReplicationError` 整包拒绝，副本 `head` 不变（工具的 `corrupt_probe` 会断言这一点）。不要重试被篡改的包；重新 `export` 原始包，或重新引导。

### 6.5 恢复核对不通过
`restore` 非零退出时，看 `checks` 中 `ok:false` 的项：
- `documents_match`/`documents_count_matches`：归档内容与清单不符 → 归档损坏，换上一份并 `verify`；
- `index_projection_recomputed`：索引投影与可见文档不一致 → 归档不可信，勿上线；
- `identity_rotated`/`mode_is_primary`：恢复方式不对（是否误用 `follower`）；
- `sqlite_integrity`/`foreign_keys`/`commit_continuity`：物理或逻辑损坏，换归档。

## 7. 收尾
- 关闭所有调用方句柄后再移动/归档数据目录；不要共享网络盘。
- 清理工具遗留的 `.strataleaf-*` 临时侧车（仅当主临时文件已不存在）：
  ```bash
  python3 -m ops.dr prune-temporaries --directory /srv/backups            # 先看
  python3 -m ops.dr prune-temporaries --directory /srv/backups --apply    # 再删
  ```
- 回收不会自动缩小文件；物理容量由 SQLite 空闲页复用。
- 保留演练证据至少一个备份周期，便于审计恢复点与 RTO。
