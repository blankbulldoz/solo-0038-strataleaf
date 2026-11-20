# 数据与兼容边界

物理表包括 meta、collections、versions、commits、receipts、index_defs、index_values、pins、consumers、export_sessions、export_segments。versions 的主键为集合、键、修订；body 的 SQL NULL 表示删除，JSON null 只能作为文档字段。collections 保存当前模式；index_values 是当前 head 的投影。commits 保存完整的操作序列；receipts 保存不受历史回收影响的请求摘要与结果。幂等键不能被不同操作复用，回放不重新执行前置条件。

export_sessions 记录一次分段逻辑导出的集合、冻结修订、段数、总文档数、整体摘要、读保留点名、状态（open/complete/abandoned）与已接收进度；export_segments 保存每段的段序、首末文档键、文档数与段摘要。段内容本身不落盘，始终按冻结修订从 versions 重新读取并与段摘要比对，因此导出条目不构成新的数据副本。导出标识由集合、数据库身份与修订决定，同一 (集合, 修订) 只允许一个 open/complete 导出；abandoned 导出可在同修订重新开始。恢复备份会清理这两张表，恢复后的数据库身份也不再匹配旧导出标识。

floor 是仍可读取的最早修订，不代表每条物理版本的最小修订；每个键在 floor 之前的最后版本可能是构造 floor 读视图的基准，必须保留。commits 仅需保存大于 floor 的提交，消费者从自己的 ack 之后读取。pin 与消费者位置不能低于现存 floor。

模式包含 fields、required、additional，支持点路径类型；additional=false 限制顶层未知根字段，不递归禁止已声明对象中的子字段。字段不允许 null，除非该字段未声明类型；缺失可通过 required 控制。integer 排除 bool，number 接受整数及有限浮点。JSON 对象不允许非字符串键；浮点必须有限。索引忽略缺失字段与对象/数组，空值作为可索引标量，数值 1 和 1.0 共用索引值，true 与 1 不共用。

游标携带数据库身份、谓词摘要、读版本和最后键，并由数据库私有密钥签名。它不是权限凭据，不冻结模式，也不是保留租约。需要稳定的长时间读取，调用者自行管理具名保留点。分段导出则自动以其 `export:<标识>` 读保留点承担同样的保护：只要导出处于 open 或 complete 而尚未释放，回收水位不能越过冻结修订；只有所有段到齐且合并摘要匹配，或调用方明确放弃，保留点才删除。

副本包是内部可信通道数据，校验和只用于检测传输损坏。正常备份恢复新主库保留逻辑内容而更换身份；副本初始化保留上游身份。分段追赶未完成时暂不提供当前索引，避免最新唯一约束错误约束中间历史视图。目录 epoch 可在没有文档提交时前进，最终空提交包仍能传递新的模式与索引。
