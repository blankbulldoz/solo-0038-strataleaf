# 恢复与观测

备份必须调用 archives.backup，不能只拷贝活动主文件。backup 与 restore 都不覆盖存在目标；恢复使用新路径，先校验来源 SHA-256，再打开新句柄。主库恢复生成新身份，副本恢复绑定来源。恢复清理原机保留点与消费者；消费方重新注册时须根据已恢复的 floor/head 判断是否需要重建。

## 载荷格式与旧备份接收

新库以 MessagePack（`meta.format = "2"`）保存文档、模式和提交；旧库是规范 JSON 文本（`meta.format = "1"`）。两种载荷的物理表示不同：JSON 文本存为 SQLite TEXT，MessagePack 存为以 `00 02` 开头的 BLOB，因此任何一行都能被独立识别为哪一种编码，不存在"看似文本实为二进制"的歧义。

旧库和新库不能在同一句柄里混用。打开 format 1 文件会在写入任何字节（包括 PRAGMA 与建表）之前抛出 `FormatMigrationRequired`，文件保持原样。先用 `strataleaf.inspect_format(path, counts=True)` 观察声明格式、每张表的文本/二进制行数，再调用 `strataleaf.migrate(path)`。

旧备份和旧快照同样是 format 1 归档，`inspect_archive(path)` 会返回 `format`、`format_name` 和 `migration: "required"`。接收路径有两条，都保留逻辑内容：

- 恢复为主库后运行 `migrate`，得到 format 2 的独立主库（新数据库标识与游标签名密钥）。
- 恢复为副本（follower）时绑定原上游标识，仍是 format 1；在副本上运行 `migrate` 后再追赶。`restore` 的返回值中 `migration` 字段明确写出 `required` 或 `legacy-archive-follows-upstream`。

`migrate(path, snapshot=...)` 会在第一次写入前，用 SQLite 在线备份 API 生成并校验一份 format 1 快照；快照的逻辑摘要必须与主库一致，否则拒绝开始。失败的预检不改变任何字节，因此任意预迁移快照、旧归档或旧备份都是明确的回退点。

## 中断、继续与安全重来

迁移的边界固定为：

1. 只读预检：解码全部载荷、校验模式、提交结构、幂等回执、物化索引和保留水位，计算逻辑摘要。任何一项不通过都在 SQLite TEXT 状态下失败。
2. 在同一个 IMMEDIATE 事务内写入迁移日志行并把 `meta.format` 改为 `migrating:1->2`。
3. 按 `chunk_size` 分批把载荷改写为 MessagePack，每批与日志计数一同提交。
4. 发布事务核对：没有遗留旧载荷、逻辑摘要与预检一致、回执与索引重新校验通过，才把 `meta.format` 设为 `2` 并删除日志。

在第 2 步之后、第 4 步之前被终止，文件带 `migrating:1->2`：旧构建和新构建都拒绝作为普通数据库打开，`inspect_format` 报告 `migration: "in-progress"`，并显示每张表的文本/二进制行数。此时只有 `migrate(path)` 能继续，它按剩余旧行接着转换，最后重新核对完整摘要。从未开始（仍是 format 1）则可以从快照或归档重来；已经发布（format 2 且无日志）再调用 `migrate` 是幂等的空操作。

`interrupt_after` 是确定性中断接缝：提交到指定数量后抛出 `MigrationInterrupted`，用于演示上述边界。`chunk_size` 限制单次事务行数。`dry_run=True` 执行完整只读校验并报告待转换行数，不写任何字节。

diagnostics.inspect(deep=True) 检查 SQLite 完整性、外键、提交连续性、当前索引与文档投影；fingerprint 比较指定读视图的键、文档版本、内容及到期事实。`strataleaf.fingerprint(path)` 在打开句柄之前对磁盘文件计算同一摘要，可直接比较迁移前后与新旧归档。前者不能证明所有业务数据正确，后者相同也不能代替模式与索引定义比对。checkpoint 返回 SQLite 的 busy、log、checkpointed 信息，并不承诺截断 WAL。

examples.observations 的 contention 会让独立连接暂持 IMMEDIATE 锁，记录第二句柄的 busy 超时及释放后的成功；retention_pressure 记录读保留点和消费者分别阻止水位推进的现象。它们用独立临时文件，不接触生产路径。观察包含真实修订和异常，适合复核运行边界。

关闭所有调用方句柄再移动数据目录；不支持共享网络盘或多主写入。迁移需要独占写锁：仍有活动句柄时 `migrate` 报写锁错误，文件保持原样。在空间紧张时先核实 pin/消费者，推进已完成的确认后再回收；回收不自动缩小文件，物理容量由 SQLite 空闲页复用。到期清理只在显式调用时执行，不能把墙钟到期等同于读视图变动。
