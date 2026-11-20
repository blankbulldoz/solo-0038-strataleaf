# 数据格式与兼容核对

本文是 StrataLeaf 格式 1 的可执行维护契约。它描述磁盘上实际存在的事实、每项事实的写入者和生命周期，以及未来修改字段、编码或回收策略时必须保留、可以重建、不能猜测的内容。所有结论都标注源码位置或随仓库提交的格式 1 样本中的可复现观察；本文不改变任何生产行为。

## 1. 如何执行核对

```bash
# 重新生成样本（仅通过公开 API 写库）
python3 -m compat.sample --write

# 全量核对：结构、读取、备份/副本、格式闸门、SQL 往返、逐字节再生成
python3 -m compat.verify

# 只核对已提交样本，不重新生成
python3 -m compat.verify --skip-regenerate
```

`compat.verify` 的退出码为 0 才表示样本与当前源码一致。每条检查输出 `check`、`ok` 和可观察 `detail`。核对只读已提交文件，在临时目录物化数据库，不接触生产路径。

样本文件位于 `compat/fixtures/format1/`：

| 文件 | 内容 |
| --- | --- |
| `primary.sql` | 主库终态：head=11、floor=7、catalog_epoch=12 |
| `replica.sql` | 副本追平后的终态：同身份、mode=replica |
| `backup-head8.sql` | 备份时刻基线：head=8、floor=7、catalog_epoch=7 |
| `*-packet.json` | 四种副本包：文档+目录、纯目录、文档、到期删除 |
| `cursor.json` | 签名游标令牌及其分页结果 |
| `manifest.json` | 全部表的事实快照、观察清单 O1–O11、运行环境 |

样本用固定身份生成（`compat/sample.py` 中的 `SAMPLE_DATABASE_ID`、`SAMPLE_CURSOR_SECRET`），只为让游标签名和包摘要可逐字节复现，不是生产密钥。生成时仅对这两个取值做了作用域内替换，所有集合、文档、索引、提交、回收和副本操作都走公开 API。

## 2. 三个水位不是同一个版本

维护者最容易犯的错误是把 `head`、`floor`、`catalog_epoch` 当成同一根计数器的不同名字。它们由不同代码写入、表示不同事实、可以独立变化：

| 事实 | 位置 | 含义 | 写入者 | 样本终值 |
| --- | --- | --- | --- | --- |
| `head` | `meta.head`（TEXT，十进制整数） | 最新**文档提交**的全局修订号；同一批次的全部操作共用它 | `transaction.py:82,93` | `11` |
| `floor` | `meta.floor`（TEXT） | 仍可读取的最早修订；`revision < floor` 的读取/导出/确认一律 `SnapshotTooOld` | `retention.py:38` | `7` |
| `catalog_epoch` | `meta.catalog_epoch`（TEXT） | 数据库级**目录变更**计数，只在集合或索引定义变化时前进，与文档修订无关 | `catalog.py:18,46`、`indexes.py:46,54` | `12` |
| `generation` | `collections.generation`（INTEGER） | **单个集合**的模式代数；创建为 1，每次 `evolve` 加一 | `catalog.py:17,43-44` | `segments=2`、`regions=2` |

样本观察：

- O1：终态 `head=11` 而 `catalog_epoch=12`；最后一次纯目录变更（`indexes.create("segments","by_seq",...)`）把 epoch 从 11 推到 12，`head` 保持 11。rev9 之后连续做“建集合 + 建索引 + 演进模式 + 建集合”四次目录变更，`head` 始终停在 9（`manifest.json` → `observed.packets.catalog-only`：`through=head=9`、`commits=[]`、`catalog_epoch=12`）。
- O2：`floor=7` 与 `head=11`、`catalog_epoch=12` 互不相等，`floor` 只由回收推进。
- 副本中间态把 `catalog_epoch` 写成 `-1` 作为“目录未追平”哨兵（`replica.py:61`）；因此该值**可能为负**，不能按无符号计数解析，也不能与 `generation` 互换。

推论：任何迁移或诊断都不得用 `head` 推导目录版本，也不得用 `catalog_epoch` 判断文档可见性；两者不可互相重建。

## 3. 磁盘对象

DDL 的唯一来源是 `storage.py:9-27`。核对项 `structure/schema-matches-source-ddl` 会逐对象比较已提交样本与当前 DDL 文本（忽略空白）。样本环境的物理参数为 SQLite WAL、页大小 4096、UTF-8、`user_version=0`、`application_id=0`（`manifest.json` → `environment`：Python 3.11.2、SQLite 3.40.1）。页大小与文件头参数由 SQLite 构建决定，不属于格式 1 的语义契约；逻辑事实以表和 `meta` 为准。

### 3.1 `meta`

`key TEXT PRIMARY KEY, value TEXT NOT NULL`。全部取值以 TEXT 保存，整数也是十进制字符串。初始化在 `storage.py:42-46`：先 `INSERT OR IGNORE` 八个键，再要求 `format == "1"`，否则 `DatabaseError`。

| 键 | 写入者 | 生命周期 | 缺失时 |
| --- | --- | --- | --- |
| `format` | 初始化，固定 `"1"` | 永不变更 | 打开即拒绝 |
| `head` | `transaction.py:93` | 文档提交推进 | 初始化 `"0"`（会静默丢水位） |
| `floor` | `retention.py:38` | 回收推进 | 初始化 `"0"` |
| `database_id` | 初始化；主库恢复时更换（`archive.py:85`） | 主库恢复换新；副本保留来源身份 | 初始化新 UUID |
| `cursor_secret` | 初始化；主库恢复时更换（`archive.py:86`） | 令牌签名密钥 | 初始化新随机值 |
| `mode` | 初始化 `"primary"`；恢复写 `"primary"`/`"replica"`（`archive.py:82`） | 控制写入权限 | 初始化 `"primary"` |
| `upstream` | 恢复为副本时写来源 `database_id`（`archive.py:83`） | 绑定副本来源 | 初始化 `""` |
| `catalog_epoch` | 见第 2 节 | 目录变更推进 | 初始化 `"0"` |

兼容要点：

- `INSERT OR IGNORE` 对**新增** meta 键意味着“旧库自动补默认值”。如果新键承载不可推导的事实，必须同时升级 `format` 并在 `inspect_archive` 的必需键集合中加入它，否则旧库会在事实缺失的情况下被静默当成默认值打开。
- 归档校验 `archive.py:24-25` 要求 `{head, floor, format, database_id, cursor_secret, mode, upstream}`，**不要求** `catalog_epoch`。因此一个缺 `catalog_epoch` 的合法备份会被接受，打开后补成 `"0"`（核对项与观察均已复现）。这是已知的格式弱点：目录水位无法从其余数据猜测，只能保留或显式迁移。
- 额外 meta 键会原样保留，当前源码不拒绝；核对项 `guards` 之外的人工实验确认未知键可读。新增键可以向后兼容，但**删除或改义**已有键不可以。

### 3.2 `collections`

`name TEXT PRIMARY KEY, schema TEXT NOT NULL, generation INTEGER NOT NULL`。`schema` 是 `normalize_schema` 产出的规范 JSON 文本：`{"additional": bool, "fields": {path: type}, "required": [sorted paths]}`（`schema.py:7-23`）。历史文档不随模式演进重写，模式只约束新写入。没有软删除：集合一旦创建即存在。

### 3.3 `versions`

`collection TEXT NOT NULL, key TEXT NOT NULL, rev INTEGER NOT NULL, body TEXT, expires REAL`，主键 `(collection,key,rev)`，`version_revision(rev)` 索引，外键指向 `collections(name)`。

- 一个提交修订对每个 `(collection,key)` 最多一行；同批次的多集合、多键写入共享 `rev`。
- `body` 为 SQL `NULL` 表示**墓碑**；JSON `null` 只能出现在 `body` 文本内部作为文档字段值。二者不可互换（`mvcc.py:37-40`、`transaction.py:84`）。
- `expires` 是 REAL 或 `NULL`；只在到期删除前对当前存活版本有意义，TTL 本身不改变读取结果，`retention.sweep` 才生成删除提交（`retention.py:42-57`）。
- 可见性：`latest_row` 取 `rev<=读版本` 的最大行，`visible_rows` 再过滤 `body IS NOT NULL`（`mvcc.py:25-34`）。因此墓碑仍参与“当前修订”判断，删除后重建必须以墓碑版本为前置（样本 `c`：rev3 删除，rev5 以 `expected=3` 重建）。

### 3.4 `commits`

`rev INTEGER PRIMARY KEY, payload TEXT NOT NULL, request_key TEXT UNIQUE, request_hash TEXT`。`payload` 是 `{"revision": n, "operations": [...]}` 的规范 JSON，每个 operation 含 `collection/key/document/expected/expires_at`（`transaction.py:88-90`）。只有 `rev > floor` 的提交被保留；回收按 `rev <= boundary` 删除（`retention.py:37`），与版本“严格小于 boundary”不同（`retention.py:34-36`）。

`request_key` 可为 `NULL`（如 `retention.sweep` 生成的删除提交），样本 rev11 即如此。

### 3.5 `receipts`

`request_key TEXT PRIMARY KEY, rev INTEGER NOT NULL, request_hash TEXT NOT NULL, count INTEGER NOT NULL`。幂等回执，**不随历史回收删除**。`request_hash` 是 `digest(operations)`（`transaction.py:61`），即规范 JSON 的 SHA-256，不是提交负载的摘要。回放只比较新摘要，命中后直接返回 `(rev, count, replayed=True)`，不重跑前置条件（`transaction.py:64-70`）。样本 O5：`commits` 只剩 8–11，`receipts` 仍保有 rev1 的 `seed`，且回收后按原参数回放返回 rev1。

### 3.6 `index_defs` / `index_values`

`index_defs(collection,name,field,unique_flag)`，主键 `(collection,name)`，`unique_flag` 为 0/1 INTEGER。
`index_values(collection,name,token,key)`，主键 `(collection,name,token,key)`，级联外键到 `index_defs`，`index_document(collection,key)` 索引。

`token` 由 `scalar_token` 生成（`codec.py:65-72`）：布尔 `b:0`/`b:1`；整数 `n:<十进制>`；浮点若为整数值也归一为 `n:<整数>`，否则 `n:<repr>`；其余 JSON 标量 `v:` + 规范 JSON。缺失字段、对象、数组不建立索引项。

样本 O6 与核对项 `structure/index-token-encoding`：`true` → `b:1`（m1），整数 `1` 与浮点 `1.0` 合并为 `n:1`（m2、m3），`1.5` → `n:1.5`（m4），显式 `null` → `v:null`（m5），字符串 → `v:"x"`（m6），缺字段/数组/对象（m7/m8/m9）没有索引项；中文键 `东` 以原样 UTF-8 写入 `v:"东"`。

`index_values` 只是**当前 head 的投影**：`refresh_document` 每次写入先删该文档旧项再重建（`indexes.py:6-19`），历史读不消费它。它可以从 `versions` 在 head 的可见集通过同一 `scalar_token` 完全重建。

### 3.7 `pins` / `consumers`

`pins(name TEXT PRIMARY KEY, rev INTEGER)`、`consumers(name TEXT PRIMARY KEY, ack INTEGER)`。二者都只能落在 `[floor, head]`（`mvcc.py:15-22` 被 `retention.pin`、`feeds.register/acknowledge` 复用）；`ack` 单调不回退。`compact` 取 `min(target, 所有 pin.rev, 所有 consumer.ack)` 作为边界（`retention.py:29-31`）。恢复会清空两表（`archive.py:87-88`）。

### 3.8 游标（不落盘，但属于持久契约）

游标是 `base64url(hmac_sha256(cursor_secret, payload) || payload)`，payload 规范 JSON 为 `{"after","database","query","revision"}`（`cursors.py:8-26`）。`database` 必须等于 `database_id`；`query` 是 `digest({"collection","predicate"})`（`query.py:29`）。它不是权限凭据，也不注册保留点；读取时仍会 `checked_revision`，所以 `floor` 超过游标修订时明确 `SnapshotTooOld`（已复现）。

样本 `cursor.json` 的令牌可解码核对：4 个键、`database=00000000-0000-4000-8000-000000000001`、`revision=11`、`after="c"`；篡改末字节会得到 `InvalidCursor`。主库恢复更换 `database_id`/`cursor_secret` 后，旧令牌被拒；同一令牌在副本上仍可用，因为副本保留来源身份。

### 3.9 副本包与副本目录时序

`replication.export` 产出 `{"body": {...}, "sha256": digest(body)}`，`body` 含 `format`（整数 1，注意与 `meta.format` 的字符串 `"1"` 不是同一表示）、`source`、`since`、`through`、`catalog_epoch`、`head`、`floor`、`collections[]`、`indexes[]`、`commits[]`（`replica.py:11-23`）。`apply` 的硬约束（`replica.py:26-66`）：

- 摘要与 `format==1` 必须匹配；
- 提交修订必须是从 `since+1` 到 `through` 的连续序列，且 `through <= head`；
- 本库 `mode=="replica"` 且 `upstream==body.source`；
- `through < 本库 head`，或 `through==head` 且 `catalog_epoch` 不更大时，视为重复/过时，返回 `applied=0`；
- 只有 `through==body.head`（追平）时才安装 `index_defs` 并重建全部 `index_values`；中间态写 `catalog_epoch=-1` 且不提供当前索引；
- `collections` 每次应用都按包覆盖。

样本四个包分别覆盖：文档+目录（since 8→through 9，带 1 个提交）、纯目录（since=through=head=9，`commits=[]`，epoch 11→12）、纯文档（9→10）、到期删除（10→11，提交载荷 `document=null`）。重复应用均 `applied=0`，最终副本与主库指纹一致、`inspect(deep)` 无问题。

两个必须记住的不对称：

- **副本不从包更新 `floor`**。`apply` 只写 `head` 和 `catalog_epoch`；样本副本 floor=7 来自恢复的基线，而非包。因此副本的 `floor` 是它自己的保留事实，不能由包中的 `floor` 或提交历史推导。
- **副本不接收 `receipts`，且由副本包写入的提交 `request_key/request_hash` 为 NULL**。样本主库 `receipts=9`、副本 `receipts=7`；副本 rev9–11 的 commits 只保留 `rev/payload`（`replica.py:58`），而恢复基线自带的 rev8 行仍保留原 `after_floor` 键。因此幂等回执不是复制事实，副本无法据此回放追平期间的请求；但仍能回放它从基线继承的回执（实验确认副本上按原 `seed` 操作提交返回 `replayed=True`，`head` 不变）。

### 3.10 备份与恢复

`archives.backup` 用 SQLite 在线备份 API 写临时文件，`inspect_archive` 校验完整性、外键、必需 meta 键和 `format`，对**文件**取 SHA-256，再硬链接原子发布（`archive.py:41-63`）。`restore` 只读复制来源，按 `follower` 决定身份：

- 主库恢复：更换 `database_id` 与 `cursor_secret`，`mode=primary`、`upstream=""`；
- 副本恢复：保留 `database_id`，`mode=replica`、`upstream=<来源 id>`；
- 两者都清空 `pins`、`consumers`。

返回的 `sha256` 是**来源**文件的摘要，不是目标文件摘要；因此“恢复后再取目标文件摘要”必然不同。核对项 `backup/api-receipt-and-no-overwrite` 同时验证了“回执摘要等于备份文件摘要”、恢复后身份更换、以及目标已存在时拒绝覆盖。

## 4. 变更兼容矩阵

“必须保留”指旧数据在新代码下语义不变；“可重建”指能从其他事实重新计算；“不能猜测”指一旦丢失没有算法可恢复。

### 4.1 新增字段 / 列 / 表

| 变更 | 必须保留 | 可重建 | 不能猜测 | 结论 |
| --- | --- | --- | --- | --- |
| `versions` 加列（如 `checksum`） | `(collection,key,rev)` 主键与 `body IS NULL` 墓碑含义 | 新列若由 `body` 派生 | 写入时的外部事实（如校验时刻） | DDL 是 `CREATE TABLE IF NOT EXISTS`，旧库不会自动加列；必须写 `ALTER TABLE` 迁移。若新列语义必需或旧代码会误读，应同时升级 `format` |
| 新表 | 现有表主键与外键 | 新表若纯投影（如缓存） | 历史操作意图 | 可加，但若旧二进制读到新库会静默忽略；因此只要语义必需就应升 `format` |
| 新 meta 键 | 已有键的值与类型 | 若可由其它事实推导 | 目录/身份/水位类事实 | 新代码打开旧库时会由 `INSERT OR IGNORE` 补默认值，旧代码打开新库则保留但不校验新键。只要新键承载必需事实，就必须把键加入 `inspect_archive` 必需集合并升级 `format`，否则缺失会被静默当成默认值 |
| `collections.schema` 新增键 | 现有 `fields/required/additional` 语义 | 可由文档扫描重算一致性，但**不能**恢复旧模式意图 | 原始模式文本若被覆盖 | 读取方需对未知键宽容；`normalize_schema` 目前拒绝未知键，写入方需迁移旧行 |

### 4.2 改编码

| 变更 | 必须保留 | 可重建 | 不能猜测 | 结论 |
| --- | --- | --- | --- | --- |
| JSON 规范形式（`ensure_ascii=False, sort_keys=True, separators=(",",":")`，`codec.py:41-43`） | 已存 `body`/`schema`/`payload` 的文本可被 `json.loads` | 文档语义 | 既有 `request_hash`、包 `sha256`、游标签名、指纹摘要 | 改分隔符/排序/转义会改变**摘要身份**：旧回执与新摘要不匹配，回放被判冲突；旧游标、旧包摘要全部失效。必须保留读取兼容或整体升 `format` |
| `scalar_token` 前缀/数值格式（`codec.py:65-72`） | `index_values` 由当前可见文档按同一函数重建 | 全部索引项 | 无（纯派生） | 可改，但必须全量重建 `index_values`；否则旧令牌与查询候选不一致，**不能**只增量迁移 |
| 浮点表示（`1` 与 `1.0` 合并） | 数值等价语义 | 索引令牌 | 原始字面量（`1` 还是 `1.0`）在 `body` 中保留，但令牌已合并 | 令牌不是文档事实，不能反推原文 |
| 模式代数/目录 epoch 的数值基 | 单调性 | epoch 可由目录操作计数校验，但不能从文档恢复精确值 | 历史目录变更次数 | 不要重编号；`catalog_epoch` 可回退为 `-1` 哨兵，不能当无符号数 |

### 4.3 改回收策略

| 变更 | 必须保留 | 可重建 | 不能猜测 | 结论 |
| --- | --- | --- | --- | --- |
| `compact` 删除条件 | floor 可读性：每个键在 `rev<=floor` 的最后一行（基准，含墓碑）必须保留 | 可见内容可由基准 + 后续版本重建 | 被删的中间版本内容 | 样本 floor=7 保留 `segments/a` rev2、`segments/c` rev5、`segments/b` rev4 墓碑等基准；若改成删净，floor 读视图会缺键 |
| 提交保留 | `rev > floor` 的连续提交供 feed/复制 | 文档状态可从 `versions` 重建 | 操作意图、`expected`、原始 `request_key` | 提交删了就不能再导出该范围；`replication.export(since<floor)` 直接拒绝 |
| 回执保留 | `receipts` 跨回收存在（O5） | 无 | `request_hash` 是单向摘要，且 `count/rev` 只能来自原回执 | 删回执会破坏幂等；不能由提交或版本反推 |
| 到期删除 | 显式删除提交、历史版本仍可读（样本 `ttl@10` 可见、`ttl@11` 为墓碑） | 到期值本身在 `versions.expires` | 若删除时未写提交，删除时刻与旧值均丢失 | 时间经过不得直接改读视图；只能生成删除提交 |
| 文件空间 | 逻辑回收不承诺缩小文件 | SQLite 空闲页复用 | 物理页历史 | 不要用文件大小判断是否已回收 |

### 4.4 `floor`、`head`、`catalog_epoch` 的独立迁移规则

| 若只改… | 允许 | 禁止 |
| --- | --- | --- |
| `head` 语义 | 仍表示文档提交修订 | 用目录变更推进 `head`；用 `catalog_epoch` 代替读版本 |
| `floor` 语义 | 仍表示最早可读修订，且保留每键基准 | 用 `head` 或 `catalog_epoch` 推导 floor；回收后仍宣称可读旧版本 |
| `catalog_epoch` 语义 | 单调计数器，允许副本 `-1` 哨兵 | 与 `generation` 合并；用 epoch 做大小时序比较时忽略哨兵 |

## 5. 样本走查

`compat/sample.py` 的写入顺序即样本契约，任一修订号偏移都会让生成断言失败：

1. 建 `segments`（模式 v1）、`marks`、`metrics`；以 `seed` 一次写入 14 个操作 → rev1。
2. `segments` 演进到含 `note` 的模式，代数 1→2；建 3 个索引。这些只推进 `catalog_epoch`（3→7），`head` 仍为 1。
3. rev2 更新 `a`；rev3 删除 `c`；rev4 删除 `b`；rev5 以 `expected=3` 重建 `c`；rev6 写入 `expires_at=100` 的 `t`。
4. `sweep(100)` 生成删除提交 → rev7。此时 `head=7`、`floor=0`。
5. 首次 `compact(7)` 被 `reader_a`(rev4) 与 `mirror`(ack4) 挡住，floor 只到 4（删除 3 个版本、4 条提交）；重复调用无进展。释放保留点、把 ack 推到 7 后，第三次 `compact(7)` 把 floor 推进到 7，再删除 2 个版本、3 条提交。
6. rev8 写 `d` 和 `ttl`（`expires_at=200`）→ head=8、floor=7、epoch=7；`archives.backup` 生成 `backup.db`。
7. rev9 写 `e`；随后“建集合 + 建索引 + 演进模式 + 建集合”四次目录变更把 epoch 推到 11，head 仍 9。主库在这之后导出包。
8. 副本由上述备份初始化（head=8、floor=7、epoch=7、同身份）。依次应用四个包：rev9+epoch11（文档+目录）、epoch12 的同 head 纯目录包、rev10 文档包、rev11 到期删除包。重复应用同一包返回 `applied=0`。
9. 主库 rev10 写 `f`；`sweep(250)` 删除 `ttl` → rev11。副本最终 head=11、floor=7、epoch=12，与主库指纹一致。

核对项覆盖：DDL 一致、主/副本 meta 与行数、版本链与三种墓碑、回执跨 floor、索引令牌、floor 边界读取、游标分页与签名、feed 边界、诊断与指纹、主库恢复换身份、四个包重放并与 `replica.sql` 逐行比对、备份回执与不覆盖、格式 2 拒绝、SQL 往返稳定、逐字节再生成。

## 6. 已确认的边界与弱点

这些不是预埋缺陷，而是当前源码的真实行为，迁移和诊断时必须知道：

1. **格式闸门在 DDL 之后**。`Storage.__init__` 先 `executescript(DDL)` 再检查 `format`（`storage.py:40-47`）。核对项观察到一个 `format=2` 文件被拒绝，但拒绝前 9 张空表已被创建；其 `meta` 未被改写。不要假设“拒绝打开”等于“文件未被触碰”。
2. **普通 SQLite 文件会被接管**。DDL 全是 `IF NOT EXISTS`，meta 用 `INSERT OR IGNORE` 初始化；打开一个含业务表的普通 SQLite 文件不会报错，而是把它当作空库接管（核对项 `guards/plain-sqlite-adopted`）。因此不能把“能打开”当作“这是 StrataLeaf 库”。
3. **`inspect_archive` 不要求 `catalog_epoch`**（`archive.py:24`）。缺该键的备份可通过校验，打开后被补成 `"0"`，目录时序事实丢失且无法重建。
4. **副本不传播 `floor`，也不复制回执与 request key**（见 3.9）。副本的回收水位和追平期间的幂等能力与主库天然不同步；只有从备份基线继承的回执仍可回放。
5. **`catalog_epoch` 可为 `-1`**，只出现在副本分段追赶的中间态；该中间态不安装 `index_defs`。
6. **指纹不覆盖模式与索引定义**。`fingerprint` 只比较读视图中的键、修订、文档与到期事实（`diagnostics.py:48-57`）；相同指纹不能证明目录一致，必须另比对 `collections`/`index_defs`。
7. **主库恢复后旧游标与旧回执的关系不同**：游标因 `database_id`/`cursor_secret` 更换而失效；回执（以及已保留提交上的 `request_key`）原样留在新库中，按原操作回放仍返回原修订（实验确认 `replayed=True` 且 `head` 不变）。不要以为换身份会清除回执，也不要依赖旧游标。

## 7. 证据索引

| 主题 | 源码 | 样本 |
| --- | --- | --- |
| DDL、meta 初始化、格式闸门 | `storage.py:9-47` | `primary.sql` 的 `CREATE` 与 `INSERT INTO meta` |
| 提交推进 head、回执摘要 | `transaction.py:61-93` | rev1–11 的 `commits`/`receipts` |
| 可见性、墓碑、读边界 | `mvcc.py:15-48` | `manifest.json` → `observed.primary_reads` |
| floor 计算与删除条件 | `retention.py:26-40` | `observed.compaction` |
| 目录 epoch、模式代数 | `catalog.py:11-47`、`indexes.py:26-54` | `meta.catalog_epoch=12`、`collections.generation` |
| 索引令牌 | `codec.py:65-72`、`indexes.py:6-19` | `observed.index_values`/`primary.index_values` |
| 游标签名与绑定 | `cursors.py:8-26`、`query.py:29-37` | `cursor.json` |
| 归档校验与恢复身份 | `archive.py:19-101` | `backup-head8.sql`、核对项 backup/* |
| 副本包与目录时序 | `replica.py:11-66` | 四个 `*-packet.json`、核对项 replica/* |

改动持久层之前的检查清单：

1. 是否改了 DDL、meta 键集合或键的取值类型？→ 升级 `format`，写迁移，并把新必需键加入 `inspect_archive`。
2. 是否改了规范 JSON、令牌格式、摘要输入或游标载荷？→ 视为编码变更：旧回执/游标/包摘要会失效，需要保留读取路径或整体升版。
3. 是否改了 `compact` 或 `sweep` 的删除条件？→ 用样本验证 floor 每个键的基准版本仍在、回执仍在、墓碑仍与 JSON null 区分。
4. 是否让 `head`、`floor`、`catalog_epoch`、`generation` 中任意两个产生耦合？→ 不允许；分别核对写入者与副本中间态。
5. 完成后运行 `python3 -m compat.sample --write` 与 `python3 -m compat.verify`，确认逐字节再生成与全部检查通过；若样本有意变化，先更新本文与 `manifest.json` 的观察，再提交新样本。
