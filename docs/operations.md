# 恢复与观测

备份必须调用 archives.backup，不能只拷贝活动主文件。backup 与 restore 都不覆盖存在目标；恢复使用新路径，先校验来源 SHA-256，再打开新句柄。主库恢复生成新身份，副本恢复绑定来源。恢复清理原机保留点与消费者；消费方重新注册时须根据已恢复的 floor/head 判断是否需要重建。

## 备份保管库（BackupVault）

`BackupVault(root)` 在一个目录内用 `vault-registry.json` 登记受管备份，所有判断只依据登记事实，不按文件名新旧猜测。登记条目有 `pending -> published -> quarantined` 三态，另由 `lease_holder` 表达恢复占用。目录用 `flock` 串行化跨进程操作，注册表先写临时文件再原子替换，进程崩溃只可能留下上一版完整注册表。

- 登记：`prepare(database)` 通过在线备份 API 写出 `.staged-<id>.db` 并登记 pending；`publish(entry_id)` 重新执行完整性检查、外键检查与 SHA-256 校验，确认 `database_id` 属于本保管库绑定的唯一来源后才改名为正式文件并写入摘要。`capture` 是两者的连续封装。首次登记绑定来源，之后其他数据库的备份一律拒绝。
- 失败重开：`reconcile()` 逐条复核登记文件，并扫描无登记的 staged 文件。pending 文件仍完整则保持 pending 等待发布或丢弃；已发布文件摘要不符、来源不符或无法打开时移入 `quarantine-<id>.db` 并从可恢复集合剔除；没有登记的 staged 孤儿文件，校验通过则收养为 pending，损坏则隔离；不认识的普通 .db 文件原样保留并在 `foreign_files` 报告，绝不代为删除。
- 使用中保护：`lease(entry_id, holder)` 是上下文管理器，租约持久化在注册表内，进程异常退出后租约仍可见。租约内的备份不会被任何 prune 计划删除；可恢复保证只统计未租用的已发布备份，因此当租约使未租用数量低于 `retain` 时，整个 prune 以 Conflict 拒绝。`restore_verified` 全程持有租约并强制核对登记摘要；同名持有者复用已有租约，不同持有者互相拒绝。
- 保留清理：`prune(retain)` 先去重再按年龄收缩。同一修订的多份备份只保留登记时间最新的一份，其余作为 same-revision duplicate 删除；去重后最旧的唯一修订备份按超龄删除，直到剩余 `retain` 份。隔离区文件不计入可恢复数量；当前已发布数量低于 `retain` 时 prune 直接报错，调用方必须先补齐备份。删除先 unlink 文件再更新注册表，保证不存在“登记在册但文件缺失”的已发布条目。
- 与历史回收的边界：`retention.compact` 回收的是主库内的旧版本与提交记录，不触碰保管库目录；保管库 prune 删除的是独立快照文件，不改变主库 floor。两者是不同事实，compact 成功从不等于备份清理，示例 `vault` 在 prune 之后仍单独执行 compact 并分别打印结果。

diagnostics.inspect(deep=True) 检查 SQLite 完整性、外键、提交连续性、当前索引与文档投影；fingerprint 比较指定读视图的键、文档版本、内容及到期事实。前者不能证明所有业务数据正确，后者相同也不能代替模式与索引定义比对。checkpoint 返回 SQLite 的 busy、log、checkpointed 信息，并不承诺截断 WAL。

examples.observations 的 contention 会让独立连接暂持 IMMEDIATE 锁，记录第二句柄的 busy 超时及释放后的成功；retention_pressure 记录读保留点和消费者分别阻止水位推进的现象。它们用独立临时文件，不接触生产路径。观察包含真实修订和异常，适合复核运行边界。

关闭所有调用方句柄再移动数据目录；不支持共享网络盘或多主写入。在空间紧张时先核实 pin/消费者，推进已完成的确认后再回收；回收不自动缩小文件，物理容量由 SQLite 空闲页复用。到期清理只在显式调用时执行，不能把墙钟到期等同于读视图变动。
