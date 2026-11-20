# 受限容器运行手册

本手册面向把 StrataLeaf 嵌入服务并部署到资源受限 Linux 容器的运维者。目标不是"进程起来了"，而是：**已确认的提交在停止/强制终止后仍然存在，健康判断不被文件存在或进程存活所冒充，恢复结果可由文档、日志和索引三者交叉验证。**

配套的可执行物：

| 文件 | 作用 |
| --- | --- |
| `deploy/container/service.conf` | 唯一配置来源：身份、路径、空间下限、WAL 上限、停机预算 |
| `deploy/container/strataleaf_ops.py` | preflight / probe / verify / supervise / drill / stop |
| `deploy/container/entrypoint.sh` | 以配置身份启动，先失败关闭再服务 |
| `deploy/container/healthcheck.sh` | 探针钩子（liveness） |
| `deploy/container/Dockerfile` | 非特权、只读代码层、两个卷 |
| `deploy/docker/compose.yaml` | 单服务 compose（命名卷，非网络盘） |
| `deploy/systemd/strataleaf.service` | 非容器主机等价单元，冻结项对齐 |
| `deploy/service/server.py` | 参考 HTTP 服务：`/healthz`、`/readyz`、`POST /events` |
| `deploy/docker/kubernetes.yaml` | StatefulSet（单副本、RWO 本地卷）、liveness/readiness 分离 |
| `deploy/smoke/drills.py` | 可复现演练：正常停止、SIGKILL、撕裂提交、长读、写冲突、空间、网络盘、身份、HTTP 就绪 |
| `deploy/artifacts/` | 一次完整演练的 `drill-report.json`、`index.json`、`report.md` |

演练证据由 `drill` 写入 `<workspace>/drill-report.json`、`index.json`、`report.md`。在无持久卷的沙箱中运行必须设置 `STRATALEAF_DRILL_ALLOW_PSEUDO=1`，报告会在 `report.md` 顶部与 `volumes` 中声明该放宽及其影响范围。

## 1. 存储布局（必须）

```
<data volume>/                 # 持久卷，本地块设备
    strataleaf.db              # 主文件
    strataleaf.db-wal          # 预写日志；崩溃后由 SQLite 恢复
    strataleaf.db-shm          # 共享内存索引；不是数据
<state volume>/                # 另一个持久卷
    running.json               # 当前 pid/worker/revision（停机前删除）
    clean-stop.json            # 仅在干净关闭后写入
    service.log                # 逐行 JSON 事件
    preflight.json
```

硬性要求：

1. **主文件、`-wal`、`-shm` 必须在同一目录、同一持久卷上。** 它们不能跨设备拆分；把 WAL 放到另一个卷会让 SQLite 拒绝启动或产生 I/O 错误。
2. 目录**不能**是 `tmpfs`、`overlay` 的可写层、或任何网络/FUSE 共享。`overlay` 被视为伪文件系统：容器重启会丢弃该层。
3. 两个卷分开挂载。state 卷丢失不影响已提交数据，只丢失观测标记。
4. 不要在运行中复制主文件当作备份。用 `Database.archives.backup`；它通过 SQLite 在线备份 API 在临时文件上校验后原子发布。

`preflight` 会读取 `/proc/self/mountinfo`，按最长前缀判定实际文件系统类别，拒绝 `nfs/nfs4/cifs/smb3/9p/ceph/glusterfs/afs/sshfs`、`tmpfs/overlay/ramfs`、以及 `vfat/exfat/ntfs3` 等缺少 POSIX 锁语义的本地格式。

### 本地 FUSE 绑定的例外

Docker Desktop / Colima / OrbStack 常把宿主本地磁盘以 `virtiofs` 绑定进容器。这不是网络盘，但 `fstype` 不足以自证。默认拒绝；只有在**已审阅该挂载源确为本地块设备**后，才可设置：

```sh
STRATALEAF_ALLOW_FUSE=1
```

该开关只放宽"无法证明是本地块设备"的 FUSE 传输，不放过任何命名网络文件系统（`fuse.sshfs` 会在放宽前就被识别为网络盘）。

## 2. 用户与权限

- 镜像以固定非 root 身份运行：`STRATALEAF_UID=9042`、`STRATALEAF_GID=9042`。Dockerfile、`service.conf`、systemd 单元、compose 的 `user:` 四处必须一致。
- 容器**不会**隐式 `chown` 卷。所有权是一次性、显式的运维步骤：

```sh
install -d -o 9042 -g 9042 -m 0750 /srv/strataleaf/data /srv/strataleaf/state
```

- 目录模式以 `0750` 为基线，`UMask=0027`。`preflight` 拒绝世界可写目录（`o+w`）、非属主所有、以及缺失写权限。
- 拒绝 `uid=0`。不需要 `CAP_*`、不需要绑定特权端口。
- `entrypoint.sh` 在 preflight 之前先校验实际 uid/gid 与配置一致；不匹配直接 `exit 78`。这样"镜像被改成 root 运行"会在服务启动前暴露，而不是在写入后才暴露。

演练覆盖：`identity` 用例证明正确身份被接受、root 被拒、uid 不匹配被拒、世界可写目录被拒。

## 3. 启动门禁（preflight）

服务不得在门禁失败时"带病启动"。`preflight` 依次检查：

1. 运行身份与配置一致且非 root；
2. data/state 目录存在、属主正确、非世界可写、属主可写；
3. 目录所在挂载为持久本地文件系统（见上）；
4. data 与 state 是不同路径；
5. 可用空间 ≥ `STRATALEAF_MIN_FREE_BYTES` **且** ≥ `STRATALEAF_MIN_FREE_PERCENT`；
6. 若主文件已存在：是普通文件、属主正确、非世界可写；
7. 若存在 `-wal` 但主文件缺失：拒绝（这是手工拷文件或卷挂错的典型症状）。

失败以 `exit 78` 退出，消息列出**全部**问题，便于一次修复。systemd 单元的 `ExecStartPre` 与容器入口都调用它。

## 4. 健康判定（最容易做错的部分）

### 4.1 两个不同的问题

| 探针 | 问题 | 通过条件 | 失败时的正确动作 |
| --- | --- | --- | --- |
| `probe --kind liveness` | 这个进程还能不能推进？ | 卷门禁通过、库可打开、元数据与内容可见、**写锁能在 busy timeout 内取得** | 重启容器 |
| `probe --kind readiness` | 还能不能把新工作继续送进来？ | 上面全部 + checkpoint 完整（`log == checkpointed`，`busy == 0`）+ **WAL 占用低于 `STRATALEAF_MAX_WAL_PERCENT`** | 停止路由新流量，**不要重启** |

退出码：liveness 健康为 `0`，门禁失败为 `78`。readiness 健康为 `0`；被长读/慢写挡住为 `degraded`（`78`）；WAL 越过卷上限为 `not-ready`（`78`，`probe` 抛 `GateError`）。参考服务把 `degraded` 映射为 `/healthz` 200、`/readyz` 503。

```sh
strataleaf_ops.py --config ... probe --kind liveness  --json   # 0 健康 / 78 失败
strataleaf_ops.py --config ... probe --kind readiness --json   # 0 健康 / 78 降级或未就绪
```

`readiness` 返回非零的两种情况：

- **`degraded`**：checkpoint 被长读或慢写挡住，但 WAL 仍在上限内。此时写仍成功，重启只会丢掉那个正在推进的读者。
- **`not-ready`（抛 `GateError`）**：WAL 超过卷容量上限，继续写将逼近 ENOSPC。

### 4.2 为什么"文件存在/进程存活"是错的

WAL 模式下读与写本来就共存。一个长读事务会让 `PRAGMA wal_checkpoint(PASSIVE)` **不报错**地停在原地：

```
( busy=0, log_frames=1272, checkpointed_frames=42 )
```

调用没有异常、`quick_check` 仍是 `ok`、主文件与 `-wal` 都在、进程还在接受写。**这四件事同时为真，而 checkpoint 已无进展、WAL 正在增长。** 只有把 `(busy, log, checkpointed)` 三元组当作信号、并结合 WAL 占卷比例，才能把"健康空闲"与"静默耗尽空间"区分开。

反例同样重要：若你把 `wal_checkpoint` 的调用成功当作健康，会得到假阳性；若你用 `mode=ro` 连接去 checkpoint，会得到 `disk I/O error`——那不是库坏了，而是只读连接不能写 `-shm`。

### 4.3 探针不做的事

- 不读 `running.json` 判断存活（那是观测，不是健康）。
- 不检查 `-shm` 是否存在（SQLite 自行重建）。
- 不对副本要求写锁：`mode != primary` 时写锁检查跳过，因为副本拒绝主写是设计意图。
- 不把 `immutable=1` 只读打开当作检查手段：它会忽略 WAL 中的已提交内容，在有未 checkpoint 的 WAL 时读到旧头。

## 5. 退出顺序

`supervise`（参考服务）与 systemd `KillSignal=SIGKILL`、compose `stop_grace_period: 45s` 共用同一时序：

1. 收到 `SIGTERM`/`SIGINT`：置位 drain 标志，**不再接受新批次**。
2. 进行中的批次跑完（参考工作负载同步等待 `commit()` 返回；真实服务应在 `STRATALEAF_DRAIN_SECONDS` 内等待）。
3. 若 drain 预算耗尽：以 `exit 75` 退出，让编排器升级为 `SIGKILL`。此时**不得**写 clean 标记。
4. 否则在关闭前于本句柄上执行 `PASSIVE` checkpoint（记录三元组），然后 `close()`。
5. `close()` 返回后才写 `clean-stop.json`（含 `revision_before_close`、`head_on_disk`、`commits`、`checkpoint`）。**标记在关闭之后写**，因此标记存在即代表句柄已释放。
6. 删除 `running.json`。

注意：`-wal`/`-shm` 的移除由**最后一个**关闭的连接决定。若关闭前另开连接做了 checkpoint，服务退出时目录里仍会留下 0 字节的 `-wal` 与 `-shm`；这不代表没关干净。`verify` 会记录 `wal_bytes_at_verify`，同一 head 在有无这些文件时都可读。

不要发送第二次 `SIGTERM`。systemd 用 `KillSignal=SIGKILL` 保证超时后是硬杀，语义明确。

## 6. 受控强制终止与恢复

`SIGKILL` 是受控的，只要你知道恢复边界：

- 已 **返回给调用方** 的 `commit()`：一定存在（`synchronous=FULL`，提交已 fsync）。
- 信号落在提交中途：可能已经落盘、也可能回滚。**未确认的写不得被当作已确认。**
- 恢复后的 head 满足：`last_reported <= head <= last_reported + 1`。

恢复步骤（`verify`）：

```sh
strataleaf_ops.py --config ... verify --expect-head <期望修订> --json
```

`verify` 以只读连接执行并在任一失败时 `exit 78`：

- `PRAGMA integrity_check`（深检查）与 `foreign_key_check`；
- 保留区间 `commits` 修订连续（`floor+1 .. head`）；
- `0 <= floor <= head`，pin 与 consumer 位于保留区间内；
- 重算当前索引投影并与 `index_values` 比对；
- 计算指定读视图的文档指纹（集合、键、版本、内容、到期）。

还原/灾难恢复用 `Archives.restore`：先测来源 SHA-256，再写入未占用的目标；主库恢复会生成新 `database_id` 与新 `cursor_secret`，并清除原机 pin/consumer。**不要**直接复制活动主文件。

## 7. 空间观察

```sh
strataleaf_ops.py --config ... space --json
```

返回 data/state 两个卷的 `available_bytes`、`available_percent`，并按配置下限给出 `problems`；低于任一下限即 `exit 78`。与 `preflight` 使用同一阈值，避免"启动时合格、运行中悄悄越线"。

空间紧张时的处理顺序：

1. 先看 `verify` 里的 `pins` 与 `consumers`，确认是否有长读或未确认消费者；
2. 让消费者推进到当前 head、释放过期 pin（`retention.unpin`）；
3. 再 `retention.compact` 推进 floor。回收**不会自动缩小文件**，物理容量由 SQLite 空闲页复用；
4. `retention.sweep(now)` 只在显式调用时按墙钟删除到期值，时间流逝本身不改变读视图；
5. 若 WAL 已逼近上限，`readiness` 会先于磁盘写满返回 `degraded`/`not-ready`。

`space-pressure` 演练用 `RLIMIT_FSIZE` 作为确定性磁盘耗尽替身：写入以 `disk I/O error` 失败，已提交 head 不变、`quick_check` 仍 `ok`、未出现部分提交，且启动门禁在低空间时拒绝。

## 8. 演练

```sh
# 完整套件（在无持久卷的沙箱里需要显式放宽；报告中会记录真实文件系统类别）
STRATALEAF_DRILL_ALLOW_PSEUDO=1 python3 deploy/container/strataleaf_ops.py \
  --config deploy/container/service.conf drill --workspace /tmp/drill --mode all --json

# 只跑生命周期
STRATALEAF_DRILL_ALLOW_PSEUDO=1 ... drill --workspace /tmp/drill --mode lifecycle
```

| 演练 | 证明 |
| --- | --- |
| `identity` | 正确身份接受；root、uid 不匹配、世界可写目录被拒 |
| `netfs` | `nfs4/cifs/9p/tmpfs/overlay` 被拒，`ext4/xfs` 接受 |
| `normal-stop` | SIGTERM 后退出码 0、clean 标记在 close 后写、退出码非 -9、已报告提交全部在、日志连续、索引与文档一致、指纹匹配 |
| `sigkill` | 退出信号 -9、无 clean 标记、WAL 在、恢复完整性 ok、`last_reported <= head <= last_reported+1`、每个已报告提交在日志中 |
| `torn` | 未提交事务的帧确实已落盘，但恢复后 head 不变、phantom 文档/日志/索引均不可见 |
| `reader` | 长读下 checkpoint 静默滞后且 WAL 增长；naive 信号全部"正常"；liveness 仍健康而 **readiness 降级**；读者释放后二者恢复 |
| `http` | 真实 `/healthz` 在长读下保持 200，`/readyz` 返回 503 `degraded`；重启后二者恢复且数据完整 |
| `conflict` | 第二写者得到有界 busy 错误（约等于 busy timeout），释放后写入成功、读到新值 |
| `space` | 写失败为 `disk I/O error`，已提交 head 存活、完整性 ok、无部分提交、低空间启动被拒 |

`drill-report.json` 是机器可读全量证据；`index.json` 是各演练的检查索引；`report.md` 是人类摘要（含环境放宽声明）。

### 演练环境的诚实边界

- 若宿主无法切换 uid，演练以调用者身份运行并在报告 `identity.identity_simulated=true` 中声明；`identity` 用例另行证明不匹配会被拒。
- 若 data/state 不在持久卷上，必须显式设置 `STRATALEAF_DRILL_ALLOW_PSEUDO=1`；报告 `volumes.pseudo_waiver_applied=true`，并声明掉电、真实 ENOSPC、重新挂载这类依赖卷持久性的检查**不在该次演练范围内**。
- `RLIMIT_FSIZE` 与真实满盘共享 SQLite 的 `SQLITE_IOERR_WRITE` 路径，但不覆盖"fsync 在满盘时失败"的所有变体。

## 9. 明确不支持的边界

- **不支持网络共享盘，也不支持多主写入。** NFS/CIFS/9p/SMB/FUSE（sshfs 等）上的 SQLite 锁不可靠，且多主写入没有共识机制。`preflight` 会拒绝这些挂载；如果你绕过门禁，本手册不提供任何正确性承诺。
- 不提供跨主机复制协议。`replication` 是库内 API，副本包需经可信通道传输；副本拒绝主写，不提供提升、双向复制或网络传输。
- 不承诺常数内存：版本扫描、分组、逻辑导出会物化输入。单文档上限 1 MiB、深度 32。
- 不提供磁盘加密、租户鉴权、分布式共识。
- 不承诺"回收后文件变小"；物理收缩依赖 SQLite 空闲页复用。
- 探针不是权限凭据；游标也不是。

## 10. 常见误判与纠正

| 观察到 | 常见误判 | 正确处理 |
| --- | --- | --- |
| 文件存在、pid 存活 | 健康 | 用 `probe --kind liveness`，需写锁可取得 |
| `wal_checkpoint` 未抛异常 | checkpoint 正常 | 看三元组；`log > checkpointed` 即无进展，`readiness` 报 `degraded` |
| `-wal`/`-shm` 仍在 | 没关干净 | 最后一个连接关闭才移除；提交已 fsync，`verify` 读同一 head |
| 只读连接跑 checkpoint 报 `disk I/O error` | 库损坏 | 只读连接不能 checkpoint；改用可写连接 |
| 容器被 kill 后有孤儿 `running.json` | 进程仍在 | `running.json` 是观测，不是存活证明；以 `verify` + `head` 为准 |
| 空间紧张先扩容或清缓存 | 掩盖了保留压力 | 先核对 pin/consumer，推进确认后再 compact |
| 副本上写锁检查失败 | 副本不健康 | 副本本就拒写；探针在 `mode != primary` 时跳过写锁 |
