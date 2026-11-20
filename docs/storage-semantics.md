# 数据与兼容边界

物理表包括 meta、collections、versions、commits、receipts、index_defs、index_values、pins、consumers。versions 的主键为集合、键、修订；body 的 SQL NULL 表示删除，JSON null 只能作为文档字段。collections 保存当前模式；index_values 是当前 head 的投影。commits 保存完整的操作序列；receipts 保存不受历史回收影响的请求摘要与结果。幂等键不能被不同操作复用，回放不重新执行前置条件。

floor 是仍可读取的最早修订，不代表每条物理版本的最小修订；每个键在 floor 之前的最后版本可能是构造 floor 读视图的基准，必须保留。commits 仅需保存大于 floor 的提交，消费者从自己的 ack 之后读取。pin 与消费者位置不能低于现存 floor。

模式包含 fields、required、additional，支持点路径类型；additional=false 限制顶层未知根字段，不递归禁止已声明对象中的子字段。字段不允许 null，除非该字段未声明类型；缺失可通过 required 控制。integer 排除 bool，number 接受整数及有限浮点。JSON 对象不允许非字符串键；浮点必须有限。索引忽略缺失字段与对象/数组，空值作为可索引标量，数值 1 和 1.0 共用索引值，true 与 1 不共用。

游标携带数据库身份、谓词摘要、读版本和最后键，并由数据库私有密钥签名。它不是权限凭据，不冻结模式，也不是保留租约。需要稳定的长时间读取，调用者自行管理具名保留点。

副本包是内部可信通道数据，校验和只用于检测传输损坏。正常备份恢复新主库保留逻辑内容而更换身份；副本初始化保留上游身份。分段追赶未完成时暂不提供当前索引，避免最新唯一约束错误约束中间历史视图。目录 epoch 可在没有文档提交时前进，最终空提交包仍能传递新的模式与索引。

# APSW 驱动迁移与异常兼容边界

持久层自标准库 `sqlite3` 迁至 APSW 3.54.1.0（内置 SQLite 3.54.1）。所有 APSW 专有细节收敛在 `strataleaf/driver.py`，其余模块只面向该文件提供的连接、行与辅助函数；物理表结构、WAL 文件布局与格式标识 1 均不变，迁移前由 `sqlite3` 创建的真实库文件可直接重开并继续写入，无需转换。

## 行为等价的内部适配（不外泄到调用方）

- 行访问：APSW 没有 `sqlite3.Row`，驱动通过 row tracer 安装 `driver.Row`，同时支持位置下标 `row[0]`、列名 `row["body"]`、`dict(row)` 与迭代；重名列取第一个，与 `sqlite3.Row` 一致。
- 事务结束：APSW 连接始终处于 autocommit，且没有 `Connection.commit/rollback`、没有隐式事务。`Storage.atomic` 与 `Storage.read` 显式发出 `BEGIN IMMEDIATE`/`BEGIN` 并以 `COMMIT`/`ROLLBACK` 语句结束，失败路径保证回滚，语义与迁移前 `isolation_level=None` 完全相同；嵌套 `BEGIN` 仍报错。
- 影响行数：APSW 游标没有 `rowcount`，删除/更新的行数统一经 `Connection.changes()`（`driver.changes`）取得，`feeds`、`retention`、`indexes` 的“未命中即 NotFound”判断不变。
- DDL：APSW 没有 `executescript`，建表脚本按分号拆分逐条执行；DDL 仍在显式事务外、autocommit 下运行。
- 忙超时：`Database(path, timeout=秒)` 经 `set_busy_timeout` 换算为毫秒；锁等待超时由 APSW 抛出 `apsw.BusyError`，释放后重试立即成功。句柄创建本身会执行一次 IMMEDIATE 元数据事务，在锁被持有时打开新句柄同样受该超时约束。
- 在线备份：改用 APSW 备份对象（`Connection.backup("main", source, "main")` 后 step/finish），仍是 SQLite 在线备份 API，临时文件校验通过后才硬链接发布，语义不变。
- 只读连接：归档检查与恢复来源以 `SQLITE_OPEN_READONLY | SQLITE_OPEN_URI` 打开 `file:...?mode=ro`；写只读句柄由 APSW 抛 `ReadOnlyError`。副本拒绝主写仍是本库的 `strataleaf.errors.ReadOnly`，与文件级只读是两层不同机制。
- checkpoint：仍执行 `PRAGMA wal_checkpoint(PASSIVE)` 并返回 `(busy, log, checkpointed)` 三元组；APSW 自带的 `wal_checkpoint()` 方法只返回两元组，故不替换该 PRAGMA 路径。

## 异常兼容边界（调用方需要知道的变化）

- 本库自有异常体系（`DatabaseError`、`Conflict`、`NotFound`、`InvalidDocument`、`SnapshotTooOld`、`InvalidCursor`、`ReadOnly`、`ReplicationError`）的名称、触发条件与消息语义保持不变，正常业务路径捕获这些类型的代码无需修改。
- 底层驱动异常类型发生变化：锁竞争过去是 `sqlite3.OperationalError("database is locked")`，现在是 `apsw.BusyError`（同为 `Exception` 子类，不是 `sqlite3` 异常的子类）。需要直接处理锁超时的调用方应捕获 `apsw.BusyError`/`apsw.LockedError`，或经 `strataleaf.driver` 引用同名导出；不要再捕获 `sqlite3.OperationalError`。
- 唯一/外键/非空约束冲突过去是 `sqlite3.IntegrityError`，现在是 `apsw.ConstraintError`。本库在写路径上把可预期的唯一索引冲突转译为 `Conflict`，但直接使用驱动连接的代码会见到该新类型。
- 归档损坏统一包装为本库 `DatabaseError("invalid database archive")`（迁移前为 `DatabaseError` 捕获 `sqlite3.DatabaseError`），原因异常经 `__cause__` 保留，具体类型可能是 `apsw.CorruptError`、`apsw.NotADBError` 等。
- 连接关闭后再用抛 `apsw.ConnectionClosedError`；正常经 `Storage` 的调用在关闭后先得到本库 `DatabaseError("database handle is closed")`，不会触达驱动层。
- 不兼容的调用方式：依赖 `sqlite3.Row` 具体类型做 `isinstance` 判断、读取游标 `rowcount`、调用连接 `commit()/rollback()/executescript()`、或按 `sqlite3` 异常类名分支的代码不受支持；这些面都不在本库公开 API 承诺内。

