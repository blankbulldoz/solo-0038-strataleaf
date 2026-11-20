from dataclasses import dataclass
from .codec import loads
from .errors import InvalidDocument, NotFound, SnapshotTooOld


@dataclass(frozen=True)
class Record:
    collection: str
    key: str
    revision: int
    document: dict
    expires_at: float | None = None


def checked_revision(store, revision=None):
    head, floor = int(store.get("head")), int(store.get("floor"))
    revision = head if revision is None else revision
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0 or revision > head:
        raise InvalidDocument("read revision must be between zero and current head")
    if revision < floor:
        raise SnapshotTooOld(f"revision {revision} is older than retained floor {floor}")
    return revision


def latest_row(connection, collection, key, revision):
    return connection.execute("SELECT * FROM versions WHERE collection=? AND key=? AND rev<=? ORDER BY rev DESC LIMIT 1",
                              (collection, key, revision)).fetchone()


def visible_rows(connection, collection, revision):
    return connection.execute("""SELECT v.* FROM versions v JOIN
      (SELECT key,MAX(rev) AS r FROM versions WHERE collection=? AND rev<=? GROUP BY key) latest
      ON v.collection=? AND v.key=latest.key AND v.rev=latest.r
      WHERE v.body IS NOT NULL ORDER BY v.key""", (collection, revision, collection)).fetchall()


def record(row):
    if row is None or row["body"] is None:
        return None
    return Record(row["collection"], row["key"], row["rev"], loads(row["body"]), row["expires"])


class ReadView:
    """固定读事务内的版本化读视图。

    只收敛读侧调用方的三项共同依赖，结果计算（查询计划、分组口径、
    导出行格式、诊断汇总）不属于本类，留在各自模块：

    - 读取资源生命周期：with 进入/退出委托 store.read()，一个视图对应
      一个 SQLite 读事务，离开 with 块后视图即失效；
    - 读修订检查：fix_revision 委托 checked_revision，固定本视图的读修订；
    - 集合可见文档：require_collection / rows / row 委托 visible_rows /
      latest_row，墓碑行在 rows 中隐藏、在 row 中保留。

    视图不缓存行、不跨 with 块复用、不注册读保留点：每次读取都在新的
    读事务中重新检查修订，因此跨句柄写入与回收水位推进对后续调用立即
    可见，本抽象不扩大任何既有读取承诺。历史读（含 floor 处保留的基准
    版本）始终走 versions 扫描，不消费当前 head 的索引投影。
    """

    def __init__(self, store, revision=None):
        self.store = store
        self.requested = revision
        self.connection = None
        self.revision = None
        self._resource = None

    def __enter__(self):
        self._resource = self.store.read()
        self.connection = self._resource.__enter__()
        return self

    def __exit__(self, *exc_info):
        resource, self._resource = self._resource, None
        self.connection = None
        return resource.__exit__(*exc_info)

    def fix_revision(self, revision=None):
        """校验并固定本视图的读修订；缺省使用构造时给定的修订。"""
        self.revision = checked_revision(self.store, self.requested if revision is None else revision)
        return self.revision

    def require_collection(self, collection):
        """集合不存在时拒绝。"""
        if not self.connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
            raise NotFound("unknown collection")

    def rows(self, collection, revision=None):
        """集合在固定修订下的可见行（墓碑隐藏），按键升序。"""
        return visible_rows(self.connection, collection, self._fixed(revision))

    def row(self, collection, key, revision=None):
        """单键在固定修订下的最后版本行（含墓碑行）。"""
        return latest_row(self.connection, collection, key, self._fixed(revision))

    def _fixed(self, revision):
        if revision is not None:
            return revision
        if self.revision is None:
            raise RuntimeError("read view revision is not fixed")
        return self.revision


def read_record(store, collection, key, revision=None):
    with ReadView(store) as view:
        if not view.connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
            raise NotFound("unknown collection: " + collection)
        view.fix_revision(revision)
        return record(view.row(collection, key))
