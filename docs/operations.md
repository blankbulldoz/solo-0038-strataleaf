# 恢复与观测

备份必须调用 archives.backup，不能只拷贝活动主文件。backup 与 restore 都不覆盖存在目标；恢复使用新路径，先校验来源 SHA-256，再打开新句柄。主库恢复生成新身份，副本恢复绑定来源。恢复清理原机保留点与消费者；消费方重新注册时须根据已恢复的 floor/head 判断是否需要重建。

## 副本重基线

副本落后到主库 floor 之上、export 无法再提供所需提交时，不必停掉旧副本手工换路径：`replication.rebaseline_prepare(备份)` 在副本路径旁（`<路径>.rebaseline`）暂存下一代基线，旧代继续只读服务；主库继续写入期间用 `rebaseline_apply(包)` 向暂存代追赶；`rebaseline_switch(verification)` 核对暂存代的文档指纹、目录（集合、索引、目录 epoch）与上游关系，全部一致才原子切换。verification 由主库 `replication.verification()` 生成，其修订必须等于暂存代 head；主库在核对期间继续前进时重新追赶并重取 verification 即可。

切换以 `<路径>.rebaseline.json` 日志的 switching 记录为提交点，随后单次原子改名完成；崩溃或重开后由打开路径时的恢复逻辑补完，继续使用哪一代始终明确（`rebaseline_status` 可观察服务代与暂存代）。两个数据文件始终处于 replica 模式，切换不经过可主写状态。被替换的旧句柄保持可读但拒绝一切写入（ReadOnly），调用方关闭并重开路径即落到新代。确认位置不低于新 floor 的消费者随切换携带并保留原位置；低于新 floor 的消费者在切换结果中具名列出，须按新 floor 重新登记并自行重建。读保留点不随代携带，需要时重新 pin。`rebaseline_abort` 丢弃暂存代，服务代不受影响。切换要求旧代上没有其他活跃写者；忙碌时切换明确失败并可重试。

diagnostics.inspect(deep=True) 检查 SQLite 完整性、外键、提交连续性、当前索引与文档投影；fingerprint 比较指定读视图的键、文档版本、内容及到期事实。前者不能证明所有业务数据正确，后者相同也不能代替模式与索引定义比对。checkpoint 返回 SQLite 的 busy、log、checkpointed 信息，并不承诺截断 WAL。

examples.observations 的 contention 会让独立连接暂持 IMMEDIATE 锁，记录第二句柄的 busy 超时及释放后的成功；retention_pressure 记录读保留点和消费者分别阻止水位推进的现象。它们用独立临时文件，不接触生产路径。观察包含真实修订和异常，适合复核运行边界。

关闭所有调用方句柄再移动数据目录；不支持共享网络盘或多主写入。在空间紧张时先核实 pin/消费者，推进已完成的确认后再回收；回收不自动缩小文件，物理容量由 SQLite 空闲页复用。到期清理只在显式调用时执行，不能把墙钟到期等同于读视图变动。
