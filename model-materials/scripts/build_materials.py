#!/usr/bin/env python3
"""生成 StrataLeaf 查询写法示例数据集与结果材料。

在仓库外运行，只通过公开的生产 API（strataleaf.Database 及其模块）操作一个
临时数据库文件，不修改仓库内任何文件。所有结果均为本次运行的真实 API 输出；
每个谓词结果单元格经过三重核对（query API == strataleaf.predicates.matches ==
本脚本解释器），任一不一致即中止生成。

用法：
    python3 -I build_materials.py [--repo /workspace] [--out <材料目录>]
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path


def _preparse_repo():
    for i, arg in enumerate(sys.argv):
        if arg == "--repo" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--repo="):
            return arg.split("=", 1)[1]
    return "/workspace"


sys.path.insert(0, _preparse_repo())

from strataleaf import Database, Conflict, InvalidDocument, NotFound, InvalidCursor  # noqa: E402
from strataleaf.codec import MISSING, field, scalar_token  # noqa: E402
from strataleaf.predicates import equal, matches  # noqa: E402

# ---------------------------------------------------------------------------
# 数据集定义
# ---------------------------------------------------------------------------

SCHEMA = {
    "fields": {
        "sku": "string",
        "qty": "integer",
        "active": "boolean",
        "tags": "array",
        "profile": "object",
    },
    "required": ["sku", "qty", "active"],
}
# price / note / profile 子字段 / history 刻意不声明类型：
# 只有未声明类型的字段才允许出现 null，缺失与空值的区别才能放进同一集合。

DOCS = {
    "item_01": (
        {"sku": "A-01", "qty": 5, "price": 9.5, "active": True,
         "tags": ["red", "sale"], "profile": {"score": 10, "flagged": False}, "note": "fresh"},
        "基准文档：各字段均为常规值；rev4 被补丁（qty 5→1，note→fresh-v2）",
    ),
    "item_02": (
        {"sku": "A-02", "qty": 0, "active": False, "tags": [], "profile": {}},
        "缺失组：无 price、无 note，profile 为空对象（score 缺失）",
    ),
    "item_03": (
        {"sku": "A-03", "qty": 3, "price": None, "active": True,
         "tags": ["red"], "profile": {"score": None}, "note": None},
        "空值组：price、note、profile.score 都是显式 null（与 item_02 的缺失对照）",
    ),
    "item_04": (
        {"sku": "A-04", "qty": 1, "price": 1, "active": True,
         "tags": [1, 2], "profile": {"score": 1}, "note": "1"},
        "数值 1 组：qty/price/score 都是数值 1，note 是字符串 \"1\"（与 item_05 的布尔对照）",
    ),
    "item_05": (
        {"sku": "A-05", "qty": 7, "price": True, "active": False,
         "tags": [True, False], "profile": {"score": True}, "note": "true"},
        "布尔组：price/score/tags 用布尔占位，检验“布尔不当数值、true 不等于 1”",
    ),
    "item_06": (
        {"sku": "A-06", "qty": 2, "price": 4.0, "active": True,
         "tags": ["blue"], "profile": {"score": 10, "flagged": False}, "note": "dup"},
        "同内容组 A：与 item_07 内容完全相同、键不同",
    ),
    "item_07": (
        {"note": "dup", "profile": {"flagged": False, "score": 10}, "tags": ["blue"],
         "active": True, "price": 4.0, "qty": 2, "sku": "A-06"},
        "同内容组 B：与 item_06 内容相同且字段书写顺序不同（验证键序无关）",
    ),
    "item_08": (
        {"sku": "A-08", "qty": 12, "price": "9.5", "active": True,
         "tags": ["9", "10"], "profile": {"score": "10"}, "note": 9.5},
        "字符串组：price/score/tags 是数字外观的字符串，note 反而是数值 9.5",
    ),
    "item_09": (
        {"sku": "A-09", "qty": 8, "price": 10, "active": False,
         "tags": [1.0, 2.5], "profile": {"score": 10.0}, "note": None},
        "浮点组：score 10.0、tags 1.0，检验 int/float 在相等与索引中统一",
    ),
    "item_10": (
        {"sku": "A-10", "qty": 4, "price": 2.0, "active": True,
         "tags": ["legacy"], "profile": {"score": 3}, "note": "original"},
        "删除重建组：rev2 删除留墓碑，rev3 以新内容重建（见 ITEM10_RECREATED）",
    ),
    "item_11": (
        {"sku": "A-11", "qty": 0, "price": 0.0, "active": False,
         "tags": [], "profile": {}, "note": ""},
        "零值组：0、0.0、空串、空数组、空对象——都是“存在但为假值”，与缺失/null 对照",
    ),
    "item_12": (
        {"sku": "A-12", "qty": 6, "price": 3.25, "active": True,
         "tags": ["red", "blue"], "profile": {"score": 8, "flagged": True, "audit": {"count": 2}},
         "note": "nested", "history": [{"qty": 5}, {"qty": 6}]},
        "嵌套组：三层点路径 profile.audit.count 与对象数组 history",
    ),
}

ITEM10_RECREATED = {"sku": "A-10", "qty": 40, "price": 20.0, "active": False,
                    "tags": ["legacy", "restocked"], "profile": {"score": 30}, "note": "recreated"}
ITEM01_PATCH = {"qty": 1, "note": "fresh-v2"}
ITEM13 = {"sku": "A-13", "qty": 9, "price": 1.5, "active": True,
          "tags": ["new"], "profile": {"score": 1}, "note": "late"}

ALL_KEYS = sorted(DOCS) + ["item_13"]

# 键生命周期（用于区分“尚不存在”与“墓碑”）：key -> (创建修订, [删除修订...])
LIFECYCLE = {key: (1, []) for key in DOCS}
LIFECYCLE["item_10"] = (1, [2])   # rev2 删除，rev3 重建
LIFECYCLE["item_13"] = (5, [])

# ---------------------------------------------------------------------------
# 谓词组
# ---------------------------------------------------------------------------

PREDICATES = [
    {"id": "A1", "group": "缺失与空值", "point": "exists:true 只问“字段在不在”，null 也算存在",
     "predicate": {"field": "price", "op": "exists", "value": True}},
    {"id": "A2", "group": "缺失与空值", "point": "exists:false 只匹配完全缺失的字段",
     "predicate": {"field": "price", "op": "exists", "value": False}},
    {"id": "A3", "group": "缺失与空值", "point": "eq null 只匹配显式 null；缺失字段不参与相等比较",
     "predicate": {"field": "price", "op": "eq", "value": None}},
    {"id": "A4", "group": "缺失与空值", "point": "陷阱：ne 同样要求字段存在，缺失字段连“不等于”都不成立",
     "predicate": {"field": "price", "op": "ne", "value": None}},
    {"id": "A5", "group": "缺失与空值", "point": "not(eq …) 才是“非 null 或缺失”的写法，与 A4 对照",
     "predicate": {"not": {"field": "price", "op": "eq", "value": None}}},
    {"id": "B1", "group": "数值与布尔", "point": "数值 1 的正常匹配",
     "predicate": {"field": "qty", "op": "eq", "value": 1}},
    {"id": "B2", "group": "数值与布尔", "point": "true 不等于 1：布尔不与数值相等",
     "predicate": {"field": "qty", "op": "eq", "value": True}},
    {"id": "B3", "group": "数值与布尔", "point": "反向同样成立：1 不等于 true；索引 token 命名空间也不相交",
     "predicate": {"field": "active", "op": "eq", "value": 1}},
    {"id": "B4", "group": "数值与布尔", "point": "布尔字段的正确写法：直接用 true/false",
     "predicate": {"field": "active", "op": "eq", "value": True}},
    {"id": "B5", "group": "数值与布尔", "point": "范围比较悄悄排除布尔、字符串、null 与缺失，不报错",
     "predicate": {"field": "price", "op": "ge", "value": 0}},
    {"id": "B6", "group": "数值与布尔", "point": "同上：lt 只看数值，其余类型一律排除",
     "predicate": {"field": "price", "op": "lt", "value": 10}},
    {"id": "C1", "group": "int/float 与字符串", "point": "eq 4 匹配 4.0：int/float 在相等中统一",
     "predicate": {"field": "price", "op": "eq", "value": 4}},
    {"id": "C2", "group": "int/float 与字符串", "point": "10 与 10.0 都匹配；\"10\" 与 true 不匹配",
     "predicate": {"field": "profile.score", "op": "eq", "value": 10}},
    {"id": "C3", "group": "int/float 与字符串", "point": "字符串 \"9.5\" 不等于数值 9.5",
     "predicate": {"field": "price", "op": "eq", "value": "9.5"}},
    {"id": "C4", "group": "int/float 与字符串", "point": "未声明类型的 note 里，数值 9.5 与字符串 \"9.5\" 是两种输入",
     "predicate": {"field": "note", "op": "eq", "value": 9.5}},
    {"id": "C5", "group": "int/float 与字符串", "point": "字符串范围按字典序；数值 note 被静默排除",
     "predicate": {"field": "note", "op": "lt", "value": "2"}},
    {"id": "D1", "group": "数组与整体值", "point": "contains 做数组成员判断",
     "predicate": {"field": "tags", "op": "contains", "value": "red"}},
    {"id": "D2", "group": "数组与整体值", "point": "成员比较仍守类型规则：[1,2] 与 [1.0,2.5] 匹配，[true,false] 不匹配",
     "predicate": {"field": "tags", "op": "contains", "value": 1}},
    {"id": "D3", "group": "数组与整体值", "point": "contains true 只认布尔成员",
     "predicate": {"field": "tags", "op": "contains", "value": True}},
    {"id": "D4", "group": "数组与整体值", "point": "eq [] 是整体相等，能匹配空数组；contains 永远匹配不了空数组",
     "predicate": {"field": "tags", "op": "eq", "value": []}},
    {"id": "D5", "group": "数组与整体值", "point": "对象整体相等：键集相同且递归相等，键序无关（item_07 顺序不同仍匹配）",
     "predicate": {"field": "profile", "op": "eq", "value": {"score": 10, "flagged": False}}},
    {"id": "D6", "group": "数组与整体值", "point": "contains 的目标可以是对象：逐成员做整体相等",
     "predicate": {"field": "history", "op": "contains", "value": {"qty": 5}}},
    {"id": "D7", "group": "数组与整体值", "point": "字符串字段上 contains 是子串判断；非字符串 note 被排除",
     "predicate": {"field": "note", "op": "contains", "value": "e"}},
    {"id": "E1", "group": "嵌套与组合", "point": "三层点路径；中间层缺失同样按缺失处理",
     "predicate": {"field": "profile.audit.count", "op": "eq", "value": 2}},
    {"id": "E2", "group": "嵌套与组合", "point": "in 是逐个候选值的相等判断",
     "predicate": {"field": "qty", "op": "in", "value": [0, 1, 8]}},
    {"id": "E3", "group": "嵌套与组合", "point": "and 组合；索引只取其中的等值项，最终谓词仍整体复核",
     "predicate": {"and": [{"field": "qty", "op": "ge", "value": 1},
                           {"field": "active", "op": "eq", "value": True}]}},
    {"id": "E4", "group": "嵌套与组合", "point": "or 组合：qty=0 或 price 为 null",
     "predicate": {"or": [{"field": "qty", "op": "eq", "value": 0},
                          {"field": "price", "op": "eq", "value": None}]}},
    {"id": "E5", "group": "嵌套与组合", "point": "not 包 contains：取“不含 red”的文档",
     "predicate": {"not": {"field": "tags", "op": "contains", "value": "red"}}},
]

# ---------------------------------------------------------------------------
# 解释器：为每个文档说明“为什么匹配/被排除”
# ---------------------------------------------------------------------------


def short(value):
    if value is MISSING:
        return "<缺失>"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "布尔"
    if isinstance(value, (int, float)):
        return "数值"
    if isinstance(value, str):
        return "字符串"
    if isinstance(value, list):
        return "数组"
    return "对象"


def equality_note(value, other):
    if value is None and other is None:
        return "null_eq_null", "两侧都是 null，相等"
    if value is None or other is None:
        return "null_vs_value", "null 只与 null 相等"
    if isinstance(value, bool) or isinstance(other, bool):
        if isinstance(value, bool) and isinstance(other, bool):
            return "bool_eq", "布尔与布尔比较"
        return "bool_separated", "布尔与数值/其他类型互不相等（true 不等于 1）"
    if isinstance(value, (int, float)) and isinstance(other, (int, float)):
        return "numeric_unified", "数值比较统一 int/float（1 与 1.0 相等）"
    if isinstance(value, str) and isinstance(other, str):
        return "string_eq", "字符串精确比较"
    if isinstance(value, list) and isinstance(other, list):
        return "array_whole", "数组整体相等：长度相同且逐元素相等"
    if isinstance(value, dict) and isinstance(other, dict):
        return "object_whole", "对象整体相等：键集相同且递归相等，键序无关"
    return "type_mismatch", "类型不同，不相等"


def explain_leaf(document, pred):
    path, op, other = pred["field"], pred["op"], pred["value"]
    value = field(document, path)
    node = {"kind": "leaf", "field": path, "op": op, "value": other}
    if op == "exists":
        present = value is not MISSING
        result = present == other
        node.update(result=result, code="exists_present" if present else "exists_missing",
                    detail=f"{path} {'存在，值为 ' + short(value) if present else '不存在'}；"
                           f"exists:{str(other).lower()} {'成立' if result else '不成立'}")
        return result, node
    if value is MISSING:
        node.update(result=False, code="missing_field",
                    detail=f"{path} 缺失：除 exists 外的叶子谓词对缺失字段一律不匹配")
        return False, node
    if op in ("eq", "ne"):
        same = equal(value, other)
        result = same if op == "eq" else not same
        code, note = equality_note(value, other)
        node.update(result=result, code=code,
                    detail=f"{path}={short(value)}（{type_name(value)}）vs {short(other)}（{type_name(other)}）："
                           f"{note} → {op} {'匹配' if result else '排除'}")
        return result, node
    if op == "in":
        hit = next((choice for choice in other if equal(value, choice)), MISSING)
        result = hit is not MISSING
        node.update(result=result, code="in_set" if result else "not_in_set",
                    detail=f"{path}={short(value)}（{type_name(value)}）："
                           f"{'等于候选值 ' + short(hit) if result else '不等于任何候选值 ' + short(other)}"
                           f" → {'匹配' if result else '排除'}")
        return result, node
    if op == "contains":
        if isinstance(value, list):
            notes = [f"{short(item)}:{equality_note(item, other)[1]}" for item in value]
            hit = next((item for item in value if equal(item, other)), MISSING)
            result = hit is not MISSING
            detail = (f"{path}={short(value)}：成员 {short(hit)} 与目标相等 → 匹配" if result
                      else f"{path}={short(value)}：没有成员与 {short(other)} 相等（{'；'.join(notes) or '空数组'}）→ 排除")
            node.update(result=result, code="array_member_equal" if result else "array_member_absent",
                        detail=detail)
            return result, node
        if isinstance(value, str) and isinstance(other, str):
            result = other in value
            node.update(result=result, code="substring" if result else "substring_absent",
                        detail=f"{path}={short(value)}：子串 {short(other)} {'存在' if result else '不存在'}"
                               f" → {'匹配' if result else '排除'}")
            return result, node
        node.update(result=False, code="contains_type_excluded",
                    detail=f"{path}={short(value)}（{type_name(value)}）：contains 只作用于数组（成员）"
                           f"或字符串（子串）→ 排除")
        return False, node
    # 范围比较 lt/le/gt/ge
    if isinstance(value, bool) or isinstance(other, bool):
        node.update(result=False, code="range_bool_excluded",
                    detail=f"{path}={short(value)}（{type_name(value)}）：布尔不参与范围比较 → 排除")
        return False, node
    numeric = isinstance(value, (int, float)) and isinstance(other, (int, float))
    strings = isinstance(value, str) and isinstance(other, str)
    if not numeric and not strings:
        node.update(result=False, code="range_type_excluded",
                    detail=f"{path}={short(value)}（{type_name(value)}）：范围比较要求两侧同为数值或同为字符串，"
                           f"目标是{type_name(other)} → 排除")
        return False, node
    result = {"lt": value < other, "le": value <= other,
              "gt": value > other, "ge": value >= other}[op]
    node.update(result=result, code="range_numeric" if numeric else "range_string",
                detail=f"{path}={short(value)}：{'数值' if numeric else '字符串字典序'}比较 "
                       f"{short(value)} {op} {short(other)} {'成立' if result else '不成立'}"
                       f" → {'匹配' if result else '排除'}")
    return result, node


def explain_node(document, pred):
    if "and" in pred:
        children = [explain_node(document, child) for child in pred["and"]]
        result = all(child[0] for child in children)
        return result, {"kind": "and", "result": result, "children": [child[1] for child in children]}
    if "or" in pred:
        children = [explain_node(document, child) for child in pred["or"]]
        result = any(child[0] for child in children)
        return result, {"kind": "or", "result": result, "children": [child[1] for child in children]}
    if "not" in pred:
        result_child, child = explain_node(document, pred["not"])
        return (not result_child), {"kind": "not", "result": not result_child, "children": [child]}
    return explain_leaf(document, pred)


def decisive_leaves(node):
    """提取能解释最终结果的叶子（排除时=导致排除的叶子，匹配时=支撑匹配的叶子）。"""
    if node["kind"] == "leaf":
        return [node]
    if node["kind"] == "not":
        return decisive_leaves(node["children"][0])
    if node["kind"] == "and":
        chosen = node["children"] if node["result"] else [c for c in node["children"] if not c["result"]]
    else:  # or
        chosen = [c for c in node["children"] if c["result"]] if node["result"] else node["children"]
    return [leaf for child in chosen for leaf in decisive_leaves(child)]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def capture(call):
    try:
        call()
    except (Conflict, InvalidDocument, NotFound, InvalidCursor) as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
    raise RuntimeError("预期失败的操作却成功了: " + getattr(call, "__name__", repr(call)))


def key_state(key, revision):
    created, deleted = LIFECYCLE[key]
    if revision < created:
        return "absent"
    if key == "item_10" and 2 <= revision < 3:
        return "tombstone"
    if any(revision >= d for d in deleted) and not (key == "item_10" and revision >= 3):
        return "tombstone"
    return "present"


def key_list(keys):
    return "、".join(keys) if keys else "（无）"


def key_suffix_list(keys):
    return " ".join(k.replace("item_", "") for k in keys) if keys else "—"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="/workspace")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args()
    out = Path(args.out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    (out / "results").mkdir(parents=True, exist_ok=True)

    errors = []
    timeline_events = []

    with tempfile.TemporaryDirectory(prefix="strataleaf-materials-") as tmp:
        db = Database(Path(tmp) / "materials.db")

        # --- 时间线 -------------------------------------------------------
        db.catalog.create("items", SCHEMA)
        timeline_events.append({"revision": 0, "event": "创建集合 items（目录操作，不推进文档修订）",
                                "head": db.revision})

        seed = db.batch("seed_items")
        for key, (document, _) in DOCS.items():
            seed.put("items", key, document, expected=0)
        rev1 = seed.commit().revision
        timeline_events.append({"revision": rev1, "event": "种子写入 item_01..item_12（单批次 12 个 put）",
                                "head": db.revision})

        rev2 = db.batch().delete("items", "item_10", expected=rev1).commit().revision
        timeline_events.append({"revision": rev2, "event": "删除 item_10（写入墓碑）", "head": db.revision})

        errors.append({"id": "E1", "scenario": "用 expected=0 重建已删除的键（0 只允许从未出现过的键）",
                       "result": capture(lambda: db.batch().put("items", "item_10", ITEM10_RECREATED,
                                                                expected=0).commit())})
        rev3 = db.batch().put("items", "item_10", ITEM10_RECREATED, expected=rev2).commit().revision
        timeline_events.append({"revision": rev3, "event": "以 expected=2（墓碑版本）重建 item_10，内容全部更换",
                                "head": db.revision})

        rev4 = db.patch("items", "item_01", ITEM01_PATCH, expected=rev1).revision
        timeline_events.append({"revision": rev4, "event": "补丁 item_01：qty 5→1，note→fresh-v2"
                                                             "（expected 是文档自身版本 rev1，不是全局 head）",
                                "head": db.revision})

        # --- 索引 ---------------------------------------------------------
        indexes = [db.indexes.create("items", "by_qty", "qty"),
                   db.indexes.create("items", "by_active", "active"),
                   db.indexes.create("items", "by_score", "profile.score")]
        errors.append({"id": "E2", "scenario": "item_06/item_07 内容相同（sku 同为 A-06）时建唯一索引 uniq_sku",
                       "result": capture(lambda: db.indexes.create("items", "uniq_sku", "sku", unique=True))})
        timeline_events.append({"revision": None, "event": "创建索引 by_qty/by_active/by_score（目录操作，不推进文档修订）；"
                                                           "唯一索引 uniq_sku 被拒绝", "head": db.revision})

        # --- 查询层错误（不改数据） ---------------------------------------
        errors.append({"id": "E3", "scenario": "读取超过 head 的修订",
                       "result": capture(lambda: db.get("items", "item_01", db.revision + 100))})
        errors.append({"id": "E4", "scenario": "查询不存在的集合",
                       "result": capture(lambda: db.query("nope", None))})
        errors.append({"id": "E5", "scenario": "谓词使用未定义的操作符 like",
                       "result": capture(lambda: db.query("items", {"field": "qty", "op": "like", "value": 1}))})
        errors.append({"id": "E6", "scenario": "页大小 limit=0",
                       "result": capture(lambda: db.query("items", None, limit=0))})
        errors.append({"id": "E7", "scenario": "in 的候选值不是数组",
                       "result": capture(lambda: db.query("items", {"field": "qty", "op": "in", "value": 1}))})

        # --- 谓词电池：rev1..rev4 × 28 个谓词 ------------------------------
        head = db.revision
        assert head == 4, head
        battery = []
        for spec in PREDICATES:
            entry = {"id": spec["id"], "group": spec["group"], "point": spec["point"],
                     "predicate": spec["predicate"], "runs": {}}
            for rev in (1, 2, 3, 4):
                page = db.query("items", spec["predicate"], revision=rev, limit=1000)
                scan = db.query("items", spec["predicate"], revision=rev, limit=1000, force_scan=True)
                keys = [record.key for record in page.records]
                scan_keys = [record.key for record in scan.records]
                if keys != scan_keys:
                    raise RuntimeError(f"{spec['id']}@rev{rev}: 索引与全扫结果不一致 {keys} != {scan_keys}")
                matrix = []
                for key in ALL_KEYS:
                    record = db.get("items", key, revision=rev)
                    state = key_state(key, rev)
                    if record is None:
                        matrix.append({"key": key, "visible": False, "state": state, "matched": False,
                                       "excluded_by": "visibility", "evaluations": []})
                        continue
                    explained, tree = explain_node(record.document, spec["predicate"])
                    library = matches(record.document, spec["predicate"])
                    api = key in keys
                    if not (explained == library == api):
                        raise RuntimeError(f"{spec['id']}@rev{rev}/{key}: 解释={explained} "
                                           f"库={library} API={api}")
                    matrix.append({"key": key, "visible": True, "state": "present", "matched": explained,
                                   "excluded_by": None if explained else "predicate", "evaluations": tree})
                entry["runs"][str(rev)] = {"plan": page.plan, "matched_keys": keys,
                                           "scan": {"plan": scan.plan, "matched_keys": scan_keys},
                                           "matrix": matrix}
            battery.append(entry)

        # --- 分页演示：翻页过程中写入新文档 --------------------------------
        page1 = db.query("items", None, limit=5)
        rev5 = db.batch().put("items", "item_13", ITEM13).commit().revision
        timeline_events.append({"revision": rev5, "event": "分页演示过程中新增 item_13（验证游标固定读视图）",
                                "head": db.revision})
        page2 = db.query("items", None, limit=5, cursor=page1.cursor)
        page3 = db.query("items", None, limit=5, cursor=page2.cursor)
        pages = [page1, page2, page3]
        page_keys = [[record.key for record in page.records] for page in pages]
        if any("item_13" in keys for keys in page_keys):
            raise RuntimeError("游标分页读到了翻页期间写入的文档")
        if not all(page.revision == rev4 for page in pages):
            raise RuntimeError("游标页未固定在首页的读修订")
        errors.append({"id": "E8", "scenario": "把首页游标用于另一个谓词（游标绑定谓词摘要）",
                       "result": capture(lambda: db.query("items", {"field": "qty", "op": "ge", "value": 0},
                                                          cursor=page1.cursor))})
        fresh = db.query("items", None, limit=1000)
        pagination = {"predicate": None, "limit": 5,
                      "pages": [{"index": i + 1, "keys": keys, "revision": page.revision,
                                 "cursor_issued": page.cursor is not None}
                                for i, (page, keys) in enumerate(zip(pages, page_keys))],
                      "write_during_pagination": {"key": "item_13", "revision": rev5},
                      "item_13_in_pages": False,
                      "fresh_head_query": {"revision": fresh.revision,
                                           "keys": [record.key for record in fresh.records]}}

        # --- 版本链与完整性 ----------------------------------------------
        def chain(key, revisions):
            rows = []
            for rev in revisions:
                record = db.get("items", key, revision=rev)
                rows.append({"revision": rev,
                             "state": "visible" if record else key_state(key, rev),
                             "document": record.document if record else None})
            return rows

        timeline = {"events": timeline_events,
                    "item_10_chain": chain("item_10", (1, 2, 3, 4, 5)),
                    "item_01_chain": chain("item_01", (1, 4)),
                    "item_13_chain": chain("item_13", (4, 5))}

        integrity = db.diagnostics.inspect(True)
        integrity = {k: integrity[k] for k in ("ok", "issues", "head", "floor", "counts", "sqlite")}
        db.close()

    # --- 索引使用对照（执行层） --------------------------------------------
    comparison = []
    for entry in battery:
        run = entry["runs"]["4"]
        comparison.append({"id": entry["id"], "access": run["plan"]["access"], "index": run["plan"]["index"],
                           "examined": run["plan"]["examined"],
                           "scan_access": run["scan"]["plan"]["access"],
                           "scan_examined": run["scan"]["plan"]["examined"],
                           "identical_results": run["matched_keys"] == run["scan"]["matched_keys"]})
    historical_access = {rev: sorted({entry["runs"][rev]["plan"]["access"] for entry in battery})
                         for rev in ("1", "2", "3")}

    token_values = [None, True, False, 0, 1, 1.0, 2.5, "1", "true", "red", "", [], [1], {}, {"a": 1}]
    def token_note(value):
        if value is None:
            return "null 是可索引标量（eq null 可走索引）"
        if isinstance(value, bool):
            return "布尔有独立命名空间 b:，与数值不相交"
        if isinstance(value, int):
            return "数值命名空间 n:，整数直接表示"
        if isinstance(value, float):
            if value.is_integer():
                return f"整数值浮点与整数 {int(value)} 共用同一 token n:{int(value)}"
            return "非整浮点保留完整表示"
        if isinstance(value, str):
            return "字符串命名空间 v:，与数值/布尔不相交"
        return "数组与对象不产生 token，不可索引（eq 对象/数组只能全扫）"

    scalar_tokens = [{"value": json.dumps(v, ensure_ascii=False), "token": scalar_token(v),
                      "note": token_note(v)} for v in token_values]

    index_usage = {"indexes": indexes, "unique_index_conflict": errors[1]["result"],
                   "head_revision_at_battery": 4,
                   "comparison_at_head": comparison,
                   "historical_access": historical_access,
                   "historical_note": "rev1–3 的全部谓词都走 version_scan：索引只服务当前 head，历史读回退版本扫描",
                   "scalar_tokens": scalar_tokens,
                   "integrity": integrity}

    # --- 落盘 JSON ---------------------------------------------------------
    dataset = {"collection": "items", "schema": SCHEMA,
               "documents": {key: {"purpose": purpose, "document": document}
                             for key, (document, purpose) in DOCS.items()},
               "item_10_recreated": ITEM10_RECREATED,
               "item_01_patch": ITEM01_PATCH,
               "item_13_late": ITEM13,
               "keys_in_order": ALL_KEYS}
    write_json(out / "data" / "dataset.json", dataset)
    write_json(out / "results" / "timeline.json", timeline)
    write_json(out / "results" / "predicates.json",
               {"collection": "items", "head_at_battery": 4, "revisions": [1, 2, 3, 4],
                "keys": ALL_KEYS,
                "verification": "每个单元格经三重核对：query API 结果 == strataleaf.predicates.matches == "
                                "scripts/build_materials.py 解释器；任一不一致即中止生成",
                "predicates": battery})
    write_json(out / "results" / "index_usage.json", index_usage)
    write_json(out / "results" / "errors.json", errors)
    write_json(out / "results" / "pagination.json", pagination)

    report = build_report(dataset, timeline, battery, index_usage, errors, pagination, integrity)
    (out / "REPORT.md").write_text(report, encoding="utf-8")
    print("materials written to", out)
    print("integrity:", integrity["ok"], "| predicates:", len(battery),
              "| matrix cells:", sum(len(e["runs"][r]["matrix"]) for e in battery for r in e["runs"]))


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# REPORT.md 生成
# ---------------------------------------------------------------------------


def build_report(dataset, timeline, battery, index_usage, errors, pagination, integrity):
    lines = []
    add = lines.append

    add("# StrataLeaf 查询写法选择材料（实测报告）")
    add("")
    add("本报告由 `scripts/build_materials.py` 用 StrataLeaf 生产 API 在临时数据库上实际运行生成，"
        "仓库代码未被修改。每个谓词结果单元格都经过三重核对（查询 API == 库内 `predicates.matches` == "
        "脚本解释器），不一致即中止生成，因此下表中的“排除原因”与引擎行为一一对应。")
    add("")
    add("- 结果含义（谓词语义）见第 3、4 节；索引使用（执行方式）见第 5 节，两者刻意分开。")
    add("- 完整数据：`results/predicates.json`（28 谓词 × 4 修订 × 13 键的完整矩阵）。")
    add("- 复现：`python3 -I scripts/build_materials.py --repo /workspace`（每次在全新临时库上重建）。")
    add("")

    # --- 1 数据集 ---------------------------------------------------------
    add("## 1. 数据集：让每对易混概念各有一个对照文档")
    add("")
    add("集合 `items`，模式只声明 `sku/qty/active/tags/profile` 五个字段的类型"
        "（`sku/qty/active` 必填）。`price`、`note`、`profile` 子字段、`history` 刻意不声明类型——"
        "只有未声明类型的字段才允许为 null，缺失与空值才能放进同一集合对照。")
    add("")
    add("| 键 | 设计目的 |")
    add("| --- | --- |")
    for key in sorted(DOCS):
        add(f"| `{key}` | {DOCS[key][1]} |")
    add(f"| `item_13` | 分页演示：rev5 才写入，验证游标固定读视图 |")
    add("")
    add("完整文档内容见 `data/dataset.json`。要点对照：")
    add("")
    add("- **缺失 vs null**：`item_02` 没有 `price`/`note`；`item_03` 的 `price`/`note`/`profile.score` 是显式 null。")
    add("- **数值 vs 布尔**：`item_04` 全是数值 1；`item_05` 在同样位置放布尔 true。")
    add("- **int vs float**：`item_09` 的 `score` 是 10.0、`tags` 含 1.0。")
    add("- **字符串 vs 数值**：`item_08` 的 `price`/`score` 是数字外观的字符串，`note` 反而是数值。")
    add("- **同内容不同键**：`item_06` 与 `item_07` 内容完全相同，且 `item_07` 字段书写顺序不同。")
    add("- **删除后重建**：`item_10` 在 rev2 删除、rev3 以全新内容重建。")
    add("")

    # --- 2 时间线 ---------------------------------------------------------
    add("## 2. 修订时间线")
    add("")
    add("| 修订 | 事件 |")
    add("| --- | --- |")
    for event in timeline["events"]:
        rev = "—" if event["revision"] is None else f"rev{event['revision']}"
        add(f"| {rev} | {event['event']} |")
    add("")
    add("可见文档数：rev1=12，rev2=11（item_10 墓碑），rev3=12（重建），rev4=12，rev5=13（item_13）。")
    add("")
    add("`item_10` 版本链（实际 `db.get` 结果）：")
    add("")
    add("| 读取修订 | 结果 | 说明 |")
    add("| --- | --- | --- |")
    notes10 = {1: "原始内容", 2: "墓碑：当前读不可见", 3: "重建后的新内容", 4: "重建内容保持", 5: "重建内容保持"}
    for row in timeline["item_10_chain"]:
        doc = "null（不可见）" if row["document"] is None else "`" + json.dumps(row["document"], ensure_ascii=False, sort_keys=True) + "`"
        add(f"| rev{row['revision']} | {doc} | {notes10[row['revision']]} |")
    add("")
    add("`item_01` 在 rev1 为 `qty=5, note=fresh`，rev4 起为 `qty=1, note=fresh-v2`（补丁生效）。")
    add("")

    # --- 3 谓词语义 -------------------------------------------------------
    add("## 3. 结果含义：每个谓词到底排除了谁")
    add("")
    add("以下按主题分组。每个谓词给出 rev4（建电池时的 head）的匹配键与**完整排除清单**——"
        "被排除的每个键都列出原因，而不是只展示几个成功返回。历史修订间的差异附在小节末尾；"
        "全部修订的完整矩阵见 `results/predicates.json`。")
    add("")
    current_group = None
    for entry in battery:
        if entry["group"] != current_group:
            current_group = entry["group"]
            add(f"### {current_group}")
            add("")
        run = entry["runs"]["4"]
        pred_json = json.dumps(entry["predicate"], ensure_ascii=False, sort_keys=True)
        add(f"#### {entry['id']} · `{pred_json}`")
        add("")
        add(f"要点：{entry['point']}")
        add("")
        add(f"**匹配（{len(run['matched_keys'])} 个）**：{key_list(run['matched_keys'])}")
        add("")
        excluded_pred = [row for row in run["matrix"] if row["excluded_by"] == "predicate"]
        excluded_vis = [row for row in run["matrix"] if row["excluded_by"] == "visibility"]
        add(f"**被谓词排除（{len(excluded_pred)} 个）**：")
        add("")
        if excluded_pred:
            add("| 键 | 排除原因 |")
            add("| --- | --- |")
            for row in excluded_pred:
                reasons = "；".join(leaf["detail"] for leaf in decisive_leaves(row["evaluations"]))
                add(f"| `{row['key']}` | {reasons} |")
        else:
            add("（无）")
        add("")
        if excluded_vis:
            vis_text = "、".join(f"`{row['key']}`（{'墓碑' if row['state'] == 'tombstone' else '该修订时尚未写入'}）"
                                 for row in excluded_vis)
            add(f"**该修订不可见（与谓词无关）**：{vis_text}")
            add("")
        history = [entry["runs"][str(rev)]["matched_keys"] for rev in (1, 2, 3, 4)]
        if any(keys != history[-1] for keys in history):
            add("**历史差异**：" + " → ".join(f"rev{rev} 匹配 {key_list(keys)}"
                                             for rev, keys in zip((1, 2, 3, 4), history)))
        else:
            add("**历史差异**：rev1–4 匹配集合相同（各修订的可见性差异见上表与第 4 节）"
                if excluded_vis else "**历史差异**：rev1–4 匹配集合相同")
        add("")

    # --- 4 历史对照总表 ----------------------------------------------------
    add("## 4. 历史修订对照总表")
    add("")
    add("每个谓词在四个修订上的匹配键（只写键后缀，如 `01 03` = item_01、item_03）：")
    add("")
    add("| 谓词 | rev1 | rev2 | rev3 | rev4 |")
    add("| --- | --- | --- | --- | --- |")
    for entry in battery:
        cells = [key_suffix_list(entry["runs"][str(rev)]["matched_keys"]) for rev in (1, 2, 3, 4)]
        add(f"| {entry['id']} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} |")
    add("")
    add("可见性背景：item_10 在 rev2 为墓碑（任何谓词都匹配不到）；item_13 在 rev1–4 尚未写入。"
        "修订间结果差异只来自三类原因：文档被更新（item_01）、删除/重建（item_10）、可见性本身。")
    add("")

    # --- 5 索引使用 -------------------------------------------------------
    add("## 5. 索引使用（执行方式，不改变结果含义）")
    add("")
    add("已建索引：`by_qty(qty)`、`by_active(active)`、`by_score(profile.score)`，均非唯一。"
        "以下对照在 rev4（建电池时的 head）实测：")
    add("")
    add("| 谓词 | 访问方式 | 使用索引 | 检查行数 | 全扫检查行数 | 两种执行结果一致 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for row in index_usage["comparison_at_head"]:
        index = row["index"] or "—"
        add(f"| {row['id']} | {row['access']} | {index} | {row['examined']} | "
            f"{row['scan_examined']} | {'✓' if row['identical_results'] else '✗'} |")
    add("")
    add("读法：")
    add("")
    add("- `equality_index` 只表示**候选行来自索引投影**；每一行仍用完整谓词复核，"
        "所以两种执行方式的结果逐键相同（上表最后一列，脚本亦断言，不一致即中止生成）。")
    add("- 只有谓词顶层的 `eq` 项（含 `and` 之下的）且字段建有索引、目标值是标量时才走索引；"
        "范围、成员、整体对象相等（如 D5）与无索引字段（如 price）都回退 `version_scan`。")
    add("- B3（`active eq 1`）走了索引但检查 0 行：数值 token `n:1` 在布尔索引里不存在，"
        "索引层面就编码了“true 不等于 1”。")
    add("- E3 只有 `active eq true` 是等值项：索引先缩到 7 行候选，`qty ge 1` 在复核阶段生效。")
    add("- **历史修订不用索引**：rev1–3 的全部 28 个谓词实测均为 `version_scan`"
        "（见 `results/predicates.json` 中各 run 的 plan），索引只服务当前 head。")
    add("")
    add("标量索引 token 规则（`strataleaf.codec.scalar_token` 实测）：")
    add("")
    add("| 值 | token | 说明 |")
    add("| --- | --- | --- |")
    for row in index_usage["scalar_tokens"]:
        token = "（无，不索引）" if row["token"] is None else f"`{row['token']}`"
        add(f"| `{row['value']}` | {token} | {row['note']} |")
    add("")
    conflict = index_usage["unique_index_conflict"]
    add(f"唯一索引：对 `sku` 建唯一索引 `uniq_sku` 被拒绝（`{conflict['error']}: {conflict['message']}`）——"
        "item_06 与 item_07 内容相同、键不同，唯一约束在现有数据上即冲突；"
        "缺失字段与对象/数组不进入索引，null 作为标量参与唯一性。")
    add("")
    add(f"深度自检：`diagnostics.inspect(True)` → ok={integrity['ok']}，issues={integrity['issues']}，"
        f"head={integrity['head']}，floor={integrity['floor']}（含索引投影与当前文档的逐条重算核对）。")
    add("")

    # --- 6 分页 -----------------------------------------------------------
    add("## 6. 分页与游标")
    add("")
    add(f"谓词 `null`（匹配全部）、页大小 5，首页读修订 rev{pagination['pages'][0]['revision']}；"
        f"翻页期间写入 `item_13`（rev{pagination['write_during_pagination']['revision']}）：")
    add("")
    add("| 页 | 键 | 读修订 | 签发后续游标 |")
    add("| --- | --- | --- | --- |")
    for page in pagination["pages"]:
        add(f"| {page['index']} | {key_list(page['keys'])} | rev{page['revision']} | "
            f"{'是' if page['cursor_issued'] else '否'} |")
    add("")
    fresh = pagination["fresh_head_query"]
    add(f"三页均未出现 `item_13`：游标把读视图固定在首页的 rev{pagination['pages'][0]['revision']}。"
        f"重新不带游标查询（rev{fresh['revision']}）则返回 {len(fresh['keys'])} 个键，包含 `item_13`。"
        "游标只固定读视图，不是保留租约；跨长时操作应自行 `retention.pin`。")
    add("")

    # --- 7 错误 -----------------------------------------------------------
    add("## 7. 观测到的错误（实际 API 拒绝）")
    add("")
    add("| 编号 | 场景 | 错误 | 消息 |")
    add("| --- | --- | --- | --- |")
    for item in errors:
        add(f"| {item['id']} | {item['scenario']} | `{item['result']['error']}` | {item['result']['message']} |")
    add("")

    # --- 8 文件清单 -------------------------------------------------------
    add("## 8. 文件清单")
    add("")
    add("| 文件 | 内容 |")
    add("| --- | --- |")
    add("| `data/dataset.json` | 集合模式、12+1 个文档及各自设计目的 |")
    add("| `results/timeline.json` | rev0–5 事件、item_10/item_01/item_13 版本链（实际 get 结果） |")
    add("| `results/predicates.json` | 28 谓词 × 4 修订 × 13 键完整矩阵：plan、匹配键、逐键评估树 |")
    add("| `results/index_usage.json` | 索引对照、标量 token 规则、唯一索引冲突、深度自检 |")
    add("| `results/errors.json` | 8 个实际 API 拒绝的类型与消息 |")
    add("| `results/pagination.json` | 游标分页全过程与翻页期间写入的可见性 |")
    add("| `scripts/build_materials.py` | 生成脚本；重跑即在全新临时库上重建全部材料 |")
    add("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
