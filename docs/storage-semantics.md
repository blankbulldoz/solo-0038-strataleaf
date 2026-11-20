# 数据与兼容边界

物理表包括 meta、collections、versions、commits、receipts、index_defs、index_values、pins、consumers；仅在迁移期间存在 migration 日志表。versions 的主键为集合、键、修订；body 的 SQL NULL 表示删除，JSON null 只能作为文档字段。collections 保存当前模式；index_values 是当前 head 的投影。commits 保存完整的操作序列；receipts 保存不受历史回收影响的请求摘要与结果。幂等键不能被不同操作复用，回放不重新执行前置条件。

## 载荷编码与格式标识

`meta.format` 标识载荷编码：`"1"` 为规范 JSON 文本（旧库），`"2"` 为 MessagePack（新库）。包含文档的列只有三处：`collections.schema`、`versions.body` 和 `commits.payload`。format 1 下它们是 TEXT；format 2 下是以 `00 02` 开头的 BLOB。`versions.body` 的 SQL NULL 仍是删除墓碑，与编码无关。判断一行属于哪种编码只看 SQLite 的 `typeof` 和起始字节，因此部分迁移的库也能逐行解释。

MessagePack 仅使用确定性子集：映射按键排序；字符串写为 `str`；浮点固定为 float64；布尔、整数、浮点互不合并；超出有符号/无符号 64 位范围的整数用私有 ext 45 保存十进制文本，因此 Python 大整数精确往返。解码拒绝非字符串映射键、非有限浮点、未知 ext 和多余字节。

逻辑摘要（`codec.digest`）始终对规范 JSON 文本计算，而不是对存储字节计算。因此请求指纹、查询摘要、索引 token、诊断指纹与副本包校验和在 format 1 和 format 2 下含义相同；迁移不会改变幂等回执的匹配、游标的谓词绑定，也不会改变索引候选。文档大小上限仍按规范 JSON 文本计 1 MiB。

floor 是仍可读取的最早修订，不代表每条物理版本的最小修订；每个键在 floor 之前的最后版本可能是构造 floor 读视图的基准，必须保留。commits 仅需保存大于 floor 的提交，消费者从自己的 ack 之后读取。pin 与消费者位置不能低于现存 floor。

模式包含 fields、required、additional，支持点路径类型；additional=false 限制顶层未知根字段，不递归禁止已声明对象中的子字段。字段不允许 null，除非该字段未声明类型；缺失可通过 required 控制。integer 排除 bool，number 接受整数及有限浮点。JSON 对象不允许非字符串键；浮点必须有限。索引忽略缺失字段与对象/数组，空值作为可索引标量，数值 1 和 1.0 共用索引值，true 与 1 不共用。

游标携带数据库身份、谓词摘要、读版本和最后键，并由数据库私有密钥签名。载荷部分固定为规范 JSON 文本而非数据库载荷编码，所以迁移前签发的游标在迁移后仍解析为同一查询、同一读版本和同一键边界。它不是权限凭据，不冻结模式，也不是保留租约。需要稳定的长时间读取，调用者自行管理具名保留点。

副本包是内部可信通道数据，校验和只用于检测传输损坏。包格式 `format` 从 1 升到 2：格式 2 的 `collections[].schema` 是已解码的逻辑模式对象，与源库和副本的载荷编码无关；格式 1（旧构建）的包携带原始存储表示，接收方仅在包格式为 1 时按存储表示解析，所以两种包都不会被错误解释。正常备份恢复新主库保留逻辑内容而更换身份；副本初始化保留上游身份。分段追赶未完成时暂不提供当前索引，避免最新唯一约束错误约束中间历史视图。目录 epoch 可在没有文档提交时前进，最终空提交包仍能传递新的模式与索引。
