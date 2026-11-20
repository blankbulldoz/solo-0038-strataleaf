# 状态语义验证（verification）

本目录是一套**可重复运行的差分状态机验证**，用于核对 `strataleaf` 的真实
SQLite 实现是否符合文档固定的状态语义。它不是生产代码的一部分，也不修改
`strataleaf/` 的任何行为。

## 方法：独立的预期依据

核心不是拿 `query` 和 `get` 互相证明（它们共享生产实现，可能一起错），
而是一个**独立预期模型** `verification/model.py`：

- 纯标准库 Python，**不导入 `strataleaf` 的任何模块**，按
  `PROJECT_SPEC.md` / `README.md` / `docs/` 固定的语义独立重写：
  全局修订计数、每键版本链与墓碑、批次前置条件与原子回滚、
  唯一索引标量令牌（`1`≡`1.0`、`true`≠`1`、null 可索引、缺失/对象/数组不索引）、
  模式校验（integer/number 排除布尔、声明字段拒绝 null、additional=false 只管顶层）、
  谓词匹配（缺失≠null、布尔不参与数值比较）、查询计划选择（仅当前 head 用等值索引）、
  键序分页与 examined 计数、游标绑定（数据库身份/谓词摘要/读修订/键边界）、
  pin/消费者阻塞的回收水位、物理版本与提交删除规则、TTL sweep、merge patch、
  幂等回执跨回收存活。
- 模型错误类与 `strataleaf.errors` **同名但独立**，运行器按类名比对结果。

`runner.py` 把同一操作序列同时施加到真实 `Database`（临时目录里的真实
SQLite WAL 文件，含关闭重开）和模型，**逐操作**用类型严格的结构相等
（`1` 与 `1.0` 不等）比对：返回值、文档内容、错误类型、查询计划与
examined 计数、每一页内容、游标是否被接受。游标是不透明令牌，两侧各自
签发，运行器只维护槽位映射并比对"是否有下一页 + 页面内容 + 拒绝行为"。

`gen.py` 用种子确定性生成操作序列（`--seed` 可复现），符号化引用
（head/floor/pin/键当前版本）由运行器按各侧已观察状态解析。覆盖要求
（见 `__main__.py: COVERAGE_REQUIRED`）不达标时运行以退出码 2 报警。

## 覆盖的语义面

- **跨集合原子提交**：一个批次横跨 alpha/beta/gamma，任一操作前置失败
  整批回滚（随后逐键读验证无部分写入）。
- **陈旧前置版本**：expected 取零值/字面旧值/当前值/非法布尔与负值，
  比对 Conflict 与成功结果。
- **墓碑复用**：删除后以墓碑版本为前置重建、过期零前置拒绝、历史修订
  仍可读旧值。
- **唯一索引**：同值冲突、批内"先删后写"成功与"先写后删"失败（顺序敏感）、
  建索引时现存重复拒绝且不留部分索引、`1`/`1.0`/`true`/null 的令牌规则。
- **按不同修订分页**：head/head-k/floor/字面修订起页，翻页之间交错写入、
  删除、重开与回收，游标继续消费旧读视图；换谓词/换修订/外来游标拒绝。
- **交错干扰**：写入、删除、重开（同文件关闭重开，游标与 pin 仍然有效）、
  compact（pin/消费者阻塞与推进）、sweep 到期删除、幂等回执跨回收回放。
- **代表输入**：缺失 vs null、整数/浮点/布尔混淆、Unicode 键与字符串、
  嵌套对象/数组、超深嵌套（>32 拒绝）、NaN（拒绝）、空批次、重复键批次、
  非法名称/修订类型，全部走真实 SQLite 路径。
- 每个场景末尾：全量分页与逐键读取比对最终状态，并执行
  `diagnostics.inspect(deep=True)` 内部一致性检查（500/500 ok）。

## 失败收缩

任何分歧都会触发 `shrink.py`：ddmin 删除操作块 + 批次内子操作删除，
交替至不动点（结果对"删除任一操作/任一子操作"都是 1-极小），并保留
原始分歧签名（操作类型 + 两侧结果类别）避免跳到另一个问题。产出是
一段可直接阅读的最小操作序列，例如缺陷注入实验（临时让模型忽略唯一
冲突）把 196 个操作收缩为 4 个：

```
000 create_collection alpha {...}
001 commit [put alpha/k4 {code:"u3",...}]
002 index_create alpha idx_code code unique
>> 003 commit [put alpha/k3 {code:"u3",...}]
real : {"error": "Conflict"}   model: {"ok": {...}}
```

## 运行

```bash
python3 -m verification run --scenarios 500 --ops 240 --seed 1   # 主验证
python3 -m verification replay verification-repro.json           # 复现失败
python3 -m verification shrink verification-repro.json           # 进一步收缩
python3 -m verification.shrink                                   # 收缩器自测
```

退出码：0 全部通过；1 发现分歧（自动写出可复现 repro 与最小片段）；
2 覆盖缺口。同种子两次运行结果逐字节一致（已验证）。

## 预期模型的适用边界

模型是**文档状态语义的预言**，以下方面有意不覆盖或不等价：

1. **备份/恢复、副本、transfer**：涉及第二数据库身份与文件级操作，不在
   差分范围。跨库游标拒绝用独立 scratch 库单独覆盖。
2. **aggregate 分组计算**、**diagnostics.fingerprint 哈希值**：未建模；
   状态等价通过逐键逐修订内容比对建立，而非比对生产哈希。
3. **并发与锁**：执行是单写者串行的；WAL 竞争、busy 超时、checkpoint
   属于 `examples.observations` 的观测面，不在状态模型内。
4. **游标伪造/篡改**：只使用系统签发的游标；模型用自己的不透明令牌
   复现同等绑定语义（身份/摘要/修订/键边界），不复制 HMAC 实现。
5. **资源上限**：1 MiB 文档、512 字节键、10000 操作批的边界值未逐一
   探测（超深嵌套与 NaN 已覆盖）；不验证性能与内存特性。
6. **compact 的 Conflict 分支**（boundary < 现存 floor）经公开 API
   不可达：pin/consumer 在登记与推进时都校验 ≥ floor，归纳不变量保证
   blocker 永不低于 floor。模型同样实现该检查，但它只是防御性分支，
   因此未计入必需覆盖。
7. **错误消息文本**：只比对异常类型，不比对消息字符串。
8. **SQLite 物理层**：页面布局、WAL 文件物理状态不建模；
   `inspect(deep=True)` 在场景末作为内部不变量检查补充。
9. **时间**：TTL 只通过显式 `sweep(now)` 触发（与生产一致），墙钟流逝
   本身不建模。
10. **符号引用解析**：`expected=current` 等引用按各侧已观察状态解析；
    两侧解析一致本身也是比较的一部分，但若某缺陷恰好让两侧以相同方式
    错解析，理论上可能漏报——场景末的全量状态比对用于兜底。

结果见 [RESULTS.md](RESULTS.md)。
