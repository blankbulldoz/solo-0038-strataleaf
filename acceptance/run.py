"""Build acceptance material for tightening a loose sample collection.

Runs against throwaway databases in a temporary directory only; the production
implementation (``strataleaf``) and the repository database are left untouched.

Deliverables are written to ``acceptance/out``:
  raw-records.json    fixed loose input, verbatim
  received.jsonl      importable lines for the tightened target documents
  dispositions.json   per-record decision, conversion basis and audit evidence
  report.md           human-readable summary and precision notes
"""

import calendar
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strataleaf import Database, InvalidDocument  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"

LOOSE_SCHEMA = {"fields": {"sensor": "string", "source": "object"}, "required": ["sensor", "source"]}
RECEIVING_SCHEMA = {"fields": {"sensor": "string", "value": "number", "sampled_ms": "integer",
                               "source": "object"}}
TIGHT_SCHEMA = {
    "fields": {"sensor": "string", "value": "number", "sampled_ms": "integer", "source": "object"},
    "required": ["sensor", "value", "sampled_ms"],
    "additional": False,
}
TARGET_COLLECTION = "samples_readings"

# Conversion bases asserted independently of the conversion routine.
FAHRENHEIT_SCALE = (Decimal("5"), Decimal("9"), Decimal("32"))
ROUNDING_STEP = Decimal("1e-9")
ISO_INSTANT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:\d{2})$")


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def document_key(record):
    return record["source"]["system"] + "~" + record["record_key"]


def to_celsius(unit, raw_value):
    """Return (celsius Decimal, basis text) or raise ValueError."""
    if unit == "C":
        return Decimal(str(raw_value)), "单位已是摄氏，未做算术变换"
    if unit == "F":
        numerator, denominator, offset = FAHRENHEIT_SCALE
        celsius = (Decimal(str(raw_value)) - offset) * numerator / denominator
        return (celsius.quantize(ROUNDING_STEP).normalize(),
                "(F - 32) * 5 / 9，十进制精确计算，仅最后一步按 1e-9 °C 量化")
    raise ValueError("不支持的单位：%r；无换算依据" % (unit,))


def to_epoch_ms(text):
    """Return (epoch milliseconds, UTC instant) or raise ValueError."""
    if not isinstance(text, str) or not text:
        raise ValueError("时间缺失；不能推断采样时刻")
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("时间不带 UTC 偏移；时刻无法唯一确定")
    utc = moment.astimezone(timezone.utc)
    # Integer arithmetic only: no float timestamp multiplication.
    milliseconds = (calendar.timegm(utc.timetuple()) * 1000) + utc.microsecond // 1000
    return milliseconds, utc.isoformat()


def numeric_value(raw_value):
    """Return a Decimal for a JSON number or numeric string, else raise ValueError."""
    if raw_value is None:
        raise ValueError("值是 JSON null；目标要求数值")
    if isinstance(raw_value, bool):
        raise ValueError("值是布尔值；布尔不是温度，且被排除在数值之外")
    if isinstance(raw_value, str):
        try:
            candidate = Decimal(raw_value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("值是非数值字符串") from exc
        if not candidate.is_finite():
            raise ValueError("值字符串不是有限数")
        return candidate
    if not is_number(raw_value):
        raise ValueError("值不是 JSON 数值，而是 " + type(raw_value).__name__)
    candidate = Decimal(str(raw_value))
    if not candidate.is_finite():
        raise ValueError("值不是有限数")
    return candidate


def decide(records):
    """Classify every raw record; only a uniquely determined reading is accepted."""
    dispositions = []
    for record in records:
        outcome = {
            "origin_key": document_key(record),
            "sensor": record["sensor"],
            "record_key": record["record_key"],
            "source_system": record["source"]["system"],
            "original": record,
            "reasons": [],
            "notes": [],
        }

        nested = record.get("reading")
        if isinstance(nested, dict) and "value" in nested:
            try:
                top = numeric_value(record.get("value"))
                inner = numeric_value(nested["value"])
            except ValueError:
                top = inner = None
            if top is not None and inner is not None:
                if top != inner:
                    outcome["reasons"].append(
                        "同一条记录内数值矛盾（value=%s 与 reading.value=%s），且没有可信的优先级说明"
                        % (record["value"], nested["value"]))
                else:
                    outcome["reasons"].append("数值有重复表示，需确认唯一取值")

        celsius = basis = None
        try:
            celsius, basis = to_celsius(record.get("unit"), numeric_value(record.get("value")))
        except ValueError as exc:
            outcome["reasons"].append(str(exc))

        try:
            sampled_ms, utc_text = to_epoch_ms(record.get("time"))
            outcome["sampled_ms"], outcome["utc"] = sampled_ms, utc_text
        except (ValueError, TypeError) as exc:
            outcome["reasons"].append(str(exc))

        if celsius is not None:
            outcome["celsius"], outcome["unit_basis"] = str(celsius), basis
        outcome["decision"] = "pending" if outcome["reasons"] else "accepted"
        dispositions.append(outcome)

    # Two sources reporting the same sensor at the same instant are both kept:
    # distinct document keys, no overwrite, recorded as peers.
    by_instant = {}
    for outcome in dispositions:
        if "sampled_ms" in outcome:
            by_instant.setdefault((outcome["sensor"], outcome["sampled_ms"]), []).append(outcome)
    for group in by_instant.values():
        systems = {item["source_system"] for item in group}
        if len(group) > 1 and len(systems) > 1:
            for item in group:
                item["peer_sources"] = sorted(systems - {item["source_system"]})
                item["notes"].append(
                    "同一 sensor、同一采样时刻另有来源 %s 的记录；以不同文档键并存，未互相覆盖"
                    % "/".join(item["peer_sources"]))
    return dispositions


def accepted_line(item):
    return {
        "key": item["origin_key"],
        "document": {
            "sensor": item["sensor"],
            "value": float(item["celsius"]),
            "sampled_ms": item["sampled_ms"],
            "source": {
                "system": item["source_system"],
                "record_key": item["record_key"],
                "original": item["original"],
                "conversion": {
                    "from_unit": item["original"].get("unit"),
                    "to_unit": "C",
                    "method": item["unit_basis"],
                    "time_base": "Unix epoch milliseconds, UTC",
                    "original_time": item["original"].get("time"),
                    "utc_time": item["utc"],
                },
            },
        },
    }


def independent_epoch_ms(text):
    """Recompute the instant with calendar.timegm, bypassing datetime.timestamp()."""
    matched = ISO_INSTANT.match(text)
    if not matched:
        raise ValueError("independent parser rejected instant: " + repr(text))
    year, month, day, hour, minute, second = (int(part) for part in matched.groups()[:6])
    zone = matched.group(7)
    offset = 0 if zone == "Z" else (1 if zone[0] == "+" else -1) * (
        int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)
    return (calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0)) - offset) * 1000


def independent_checks(lines, dispositions):
    """Re-derive value, instant and identity with tools the builder does not share."""
    problems = []
    mapping = {item["origin_key"]: item for item in dispositions if item["decision"] == "accepted"}

    for line in lines:
        key, document = line["key"], line["document"]
        item = mapping[key]
        original = item["original"]
        unit, raw = original.get("unit"), original["value"]

        # Value: exact rational transcription, independent of Decimal and float.
        exact = (Fraction(str(raw)) - 32) * Fraction(5, 9) if unit == "F" else Fraction(str(raw))
        if Fraction(document["value"]) != exact:
            problems.append({"key": key, "check": "value", "expected": str(exact),
                             "stored": document["value"]})

        # Timezone: recompute from the retained original text with calendar.timegm.
        recomputed = independent_epoch_ms(original["time"])
        if recomputed != document["sampled_ms"]:
            problems.append({"key": key, "check": "sampled_ms", "expected": recomputed,
                             "stored": document["sampled_ms"]})
        if document["source"]["conversion"]["original_time"] != original["time"]:
            problems.append({"key": key, "check": "original_time_preserved"})

        # Identity: document key must round-trip to the retained source fields.
        if document["source"]["system"] + "~" + document["source"]["record_key"] != key:
            problems.append({"key": key, "check": "identity"})
        if document["source"]["original"] != original:
            problems.append({"key": key, "check": "original_record_preserved"})
        if document["sensor"] != item["sensor"]:
            problems.append({"key": key, "check": "sensor"})

    # Co-temporal peers must remain distinct documents with distinct source systems.
    peers = {}
    for line in lines:
        peers.setdefault((line["document"]["sensor"], line["document"]["sampled_ms"]), []).append(line)
    for (sensor, sampled), group in peers.items():
        if len(group) < 2:
            continue
        if len({line["key"] for line in group}) != len(group):
            problems.append({"sensor": sensor, "sampled_ms": sampled, "check": "peer_keys_collide"})
        if len({line["document"]["source"]["system"] for line in group}) != len(group):
            problems.append({"sensor": sensor, "sampled_ms": sampled,
                             "check": "peer_sources_collide"})
    return problems


def render_report(report):
    out = ["# 采样接收材料（宽松集合 → 收紧模式）", ""]
    out.append("本材料只用临时数据库（`tempfile.TemporaryDirectory`）建立，生产实现与仓库内数据库均未改动。")
    out.append("")
    out.append("## 目标模式")
    out.append("")
    out.append("```json")
    out.append(json.dumps(TIGHT_SCHEMA, ensure_ascii=False, indent=2))
    out.append("```")
    out.append("")
    out.append("正文只允许 `sensor`（字符串）、`value`（数值）、`sampled_ms`（整数）、`source`（对象）；"
               "前三项必填，`value` 统一为摄氏温度，`source` 保存原始记录与转换依据。")
    out.append("")
    out.append("## 逐项处置")
    out.append("")
    out.append("| 来源键 | 文档键 | 处置 | value(°C) | sampled_ms | 依据 |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for item in report["dispositions"]:
        state = "接收" if item["decision"] == "accepted" else "待确认"
        value = item.get("celsius", "—")
        sampled = item.get("sampled_ms", "—")
        if item["decision"] != "accepted":
            value = value + "（候选）" if value != "—" else value
            sampled = "—"
        reason = "；".join(item["reasons"] + item["notes"]) if (item["reasons"] or item["notes"]) else "含义唯一"
        out.append("| %s:%s | `%s` | %s | %s | %s | %s |"
                   % (item["source_system"], item["record_key"], item["origin_key"], state, value, sampled, reason))
    out.append("")
    out.append("## 精度说明")
    out.append("")
    out.append("- 华氏换算采用精确十进制 `(F − 32) × 5 ÷ 9`，只在最后一步按 1e-9 °C 量化；"
               "70.7 °F 的精确结果为 21.5 °C，落库为 JSON 数值 21.5（IEEE-754 双精度可精确表示）。")
    out.append("- 摄氏字符串 `\"21.5\"` 解析为数值 21.5，无精度损失；非数值字符串不会被猜测为数字。")
    out.append("- `sampled_ms` 为 UTC 纪元毫秒：`+08:00` 与 `Z` 两种写法分别按各自偏移换算到同一时刻；"
               "缺时间或时区不明的记录一律不下推断。")
    out.append("- 独立核对不复用构建函数：数值用 `Fraction` 精确有理数复算，时刻用 `calendar.timegm` 复算，"
               "身份用 `system~record_key` 复算，并与落库值逐项比对。")
    out.append("")
    staging = report["staging"]
    out.append("## 导入、确认与收紧证据")
    out.append("")
    out.append("- 宽松集合 `%s` 保持修订 %s，指纹 `%s`；再次打开仍可读到原始记录：%s。"
               % (report["scenario"], staging["seeded_revision"],
                  staging["loose_fingerprint"]["sha256"][:16], staging["old_rows_intact"]))
    out.append("- 新集合 `%s` 先按宽松接收模式导入 %s 条，提交修订 %s 并逐键回读确认；随后模式代数由 %s 升至 %s。"
               % (TARGET_COLLECTION, len(report["accepted_lines"]), staging["received_revision"],
                  staging["submitted_generation"], staging["schema_generation"]))
    out.append("- 收紧后 `diagnostics.inspect(deep=True)`：ok=%s；收紧后内容指纹 `%s`。"
               % (staging["inspection"]["ok"], staging["tightened_fingerprint"]["sha256"][:16]))
    out.append("- 旧修订仍可读：%s（%s）。"
               % (all(entry["old_value"] is not None for entry in staging["history"].values()),
                  "、".join("%s=%s" % (key, entry["old_value"]) for key, entry in staging["history"].items())))
    out.append("- 收紧前对宽松集合直接应用目标模式被拒绝：`%s`；收紧后写入旧形状文档被拒绝：`%s`。"
               % (staging["violations"]["loose_evolve"]["message"],
                  staging["violations"]["probe_write"]["message"]))
    out.append("- 收紧后额外根字段被拒绝：`%s`；字符串温度被拒绝：`%s`。"
               % (staging["violations"]["probe_extra"]["message"],
                  staging["violations"]["probe_string"]["message"]))
    fresh = staging["fresh_import"]
    out.append("- 交付的 JSONL 可独立导入：在只含目标模式的空库中提交修订 %s，共 %s 条文档，"
               "内容与逐键回读结果一致：%s。"
               % (fresh["revision"], len(fresh["documents"]),
                  fresh["documents"] == staging["stored_documents"]))
    out.append("")
    out.append("## 独立核对")
    out.append("")
    out.append("- 数值、时区、身份映射不一致项：%s" % (report["independent_problems"] or "无"))
    out.append("- 待确认记录未写入任何集合，也不产生占位文档；补齐唯一取值与可信时间后可重新提交。")
    out.append("")
    return "\n".join(out)


def main(fixtures_path):
    scenario = json.loads(Path(fixtures_path).read_text(encoding="utf-8"))
    raw_records = scenario["records"]
    dispositions = decide(raw_records)
    lines = [accepted_line(item) for item in dispositions if item["decision"] == "accepted"]
    problems = independent_checks(lines, dispositions)

    OUT.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="strataleaf-acceptance-") as directory:
        folder = Path(directory)
        base = folder / "loose.db"
        with Database(base) as db:
            # 1. Fixed loose input through the ordinary batch path.
            db.catalog.create(scenario["collection"], LOOSE_SCHEMA)
            batch = db.batch("seed_loose_samples")
            for record in raw_records:
                batch.put(scenario["collection"], document_key(record), record)
            seeded = batch.commit()
            db.retention.pin("acceptance_reader", seeded.revision)
            loose_revision = seeded.revision
            loose_fingerprint = db.diagnostics.fingerprint(seeded.revision)
            loose_schema = db.catalog.describe(scenario["collection"])

            # 2. The tightened schema cannot be applied to the loose collection: the gap is real.
            try:
                db.catalog.evolve(scenario["collection"], TIGHT_SCHEMA, loose_schema["generation"])
            except InvalidDocument as exc:
                gap = {"error": type(exc).__name__, "message": str(exc)}
            else:
                raise RuntimeError("loose collection unexpectedly accepted the tightened schema")

            # 3. New collection receives normalised documents under a permissive schema.
            db.catalog.create(TARGET_COLLECTION, RECEIVING_SCHEMA)
            imported = db.transfer.import_lines(
                TARGET_COLLECTION, [json.dumps(line, ensure_ascii=False) for line in lines])
            received_revision = imported.revision
            submitted_schema = db.catalog.describe(TARGET_COLLECTION)

            # 4. Import is confirmed by reading every key back at the commit revision.
            confirmation = {}
            for line in lines:
                stored = db.get(TARGET_COLLECTION, line["key"], received_revision)
                if stored is None or stored.document != line["document"]:
                    raise RuntimeError("import confirmation failed for " + line["key"])
                confirmation[line["key"]] = stored.revision
            if imported.count != len(lines):
                raise RuntimeError("import receipt count differs from accepted lines")

            # 5. Import confirmed, then tighten the target schema (scan current documents only).
            generation = db.catalog.evolve(TARGET_COLLECTION, TIGHT_SCHEMA, submitted_schema["generation"])
            tightened_schema = db.catalog.describe(TARGET_COLLECTION)
            tightened_fingerprint = db.diagnostics.fingerprint()

            # 6. Old revisions stay readable and unchanged.
            history = {}
            for line in lines:
                key = line["key"]
                old = db.get(TARGET_COLLECTION, key, received_revision)
                origin = db.get(scenario["collection"], key, loose_revision)
                if old is None or origin is None:
                    raise RuntimeError("historical read lost data for " + key)
                history[key] = {"read_revision": old.revision, "old_value": old.document["value"],
                                "origin_record": origin.document}

            # 7. Old-shaped writes are now rejected.
            probe = {"sensor": "probe", "value": "21.5", "unit": "C",
                     "time": "2026-11-18T08:00:00+08:00"}
            try:
                db.batch("probe_tight").put(TARGET_COLLECTION, "probe", probe).commit()
            except InvalidDocument as exc:
                probe_violation = {"error": type(exc).__name__, "message": str(exc)}
            else:
                raise RuntimeError("tightened schema accepted a loose document")

            # 8. Extra root fields and wrong value types are rejected too.
            extra = {"sensor": "probe", "value": 1.0, "sampled_ms": 0, "unit": "C"}
            try:
                db.batch("probe_extra").put(TARGET_COLLECTION, "probe-extra", extra).commit()
            except InvalidDocument as exc:
                extra_violation = {"error": type(exc).__name__, "message": str(exc)}
            else:
                raise RuntimeError("tightened schema accepted an undeclared root field")

            string_value = {"sensor": "probe", "value": "21.5", "sampled_ms": 0}
            try:
                db.batch("probe_string").put(TARGET_COLLECTION, "probe-string", string_value).commit()
            except InvalidDocument as exc:
                string_violation = {"error": type(exc).__name__, "message": str(exc)}
            else:
                raise RuntimeError("tightened schema accepted a string temperature")

            inspection = db.diagnostics.inspect(deep=True)
            stored_documents = {row.key: row.document
                                for row in db.query(TARGET_COLLECTION, None, limit=100).records}

        # Reopen: the original loose collection is still intact at its original revision.
        with Database(base) as reopened:
            original_fingerprint = reopened.diagnostics.fingerprint(loose_revision)
            old_rows = [reopened.get(scenario["collection"], document_key(r), loose_revision).document
                        for r in raw_records]

        # Deliverable check: the JSONL alone, imported into an empty database that already
        # carries the tightened schema, must produce the same documents at revision 1.
        with Database(folder / "fresh.db") as fresh:
            fresh.catalog.create(TARGET_COLLECTION, TIGHT_SCHEMA)
            fresh_import = fresh.transfer.import_lines(
                TARGET_COLLECTION, [json.dumps(line, ensure_ascii=False) for line in lines])
            fresh_fingerprint = fresh.diagnostics.fingerprint()
            fresh_documents = {row.key: row.document
                               for row in fresh.query(TARGET_COLLECTION, None, limit=100).records}

    report = {
        "scenario": scenario["collection"],
        "target_collection": TARGET_COLLECTION,
        "target_schema": TIGHT_SCHEMA,
        "receiving_schema": RECEIVING_SCHEMA,
        "dispositions": dispositions,
        "accepted_lines": lines,
        "independent_problems": problems,
        "staging": {
            "seeded_revision": loose_revision,
            "received_revision": received_revision,
            "confirmation": confirmation,
            "submitted_generation": submitted_schema["generation"],
            "schema_generation": generation,
            "tightened_schema": tightened_schema,
            "loose_schema": loose_schema,
            "loose_fingerprint": loose_fingerprint,
            "tightened_fingerprint": tightened_fingerprint,
            "reopen_fingerprint": original_fingerprint,
            "history": history,
            "stored_documents": stored_documents,
            "inspection": inspection,
            "violations": {"loose_evolve": gap, "probe_write": probe_violation,
                           "probe_extra": extra_violation, "probe_string": string_violation},
            "fresh_import": {"revision": fresh_import.revision,
                             "fingerprint": fresh_fingerprint,
                             "documents": fresh_documents},
            "old_rows_intact": old_rows == raw_records,
        },
    }

    (OUT / "raw-records.json").write_text(
        json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "received.jsonl").write_text(
        "".join(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n" for line in lines),
        encoding="utf-8")
    (OUT / "dispositions.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    (OUT / "report.md").write_text(render_report(report), encoding="utf-8")

    print(json.dumps({
        "accepted": len(lines),
        "pending": len(dispositions) - len(lines),
        "received_revision": received_revision,
        "generation": generation,
        "inspection_ok": inspection["ok"],
        "old_rows_intact": report["staging"]["old_rows_intact"],
        "independent_problems": problems,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent / "fixtures.json"))
