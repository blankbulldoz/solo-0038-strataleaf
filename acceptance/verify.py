"""Independently verify the delivered acceptance material.

Reads only the files in ``acceptance/out`` (never the build script's helpers) and
re-checks the target contract, the unit conversion, the timezone mapping and the
identity mapping, then imports the JSONL into a throwaway database created with
the target schema and reads every key back.

Exit status is non-zero when any check fails.
"""

import calendar
import json
import re
import sys
import tempfile
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strataleaf import Database  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
TARGET_COLLECTION = "samples_readings"
TARGET_FIELDS = {"sensor": "string", "value": "number", "sampled_ms": "integer", "source": "object"}
REQUIRED = {"sensor", "value", "sampled_ms"}
INSTANT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$")


def instant_ms(text):
    """Convert an ISO-8601 instant with offset to epoch milliseconds without floats."""
    match = INSTANT.match(text)
    if not match:
        raise ValueError("unparsable instant: " + repr(text))
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    fraction = match.group(7) or ""
    zone = match.group(8)
    offset = 0 if zone == "Z" else (1 if zone[0] == "+" else -1) * (
        int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)
    seconds = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0)) - offset
    return seconds * 1000 + (int(fraction.ljust(6, "0")) // 1000 if fraction else 0)


def expected_celsius(original):
    unit, raw = original["unit"], original["value"]
    if unit == "C":
        return Fraction(str(raw))
    if unit == "F":
        return (Fraction(str(raw)) - 32) * Fraction(5, 9)
    raise ValueError("unsupported unit in fixture: " + repr(unit))


def check_document(line, originals):
    problems = []
    key, document = line.get("key"), line.get("document")
    if not isinstance(key, str) or not isinstance(document, dict):
        return [{"key": key, "check": "shape", "detail": "line needs a string key and object document"}]
    if set(document) != set(TARGET_FIELDS):
        problems.append({"key": key, "check": "top_level_fields", "found": sorted(document)})
    for name, kind in TARGET_FIELDS.items():
        value = document.get(name)
        if name not in document:
            problems.append({"key": key, "check": "missing_field", "field": name})
            continue
        if kind == "string" and not isinstance(value, str):
            problems.append({"key": key, "check": "type", "field": name})
        elif kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            problems.append({"key": key, "check": "type", "field": name})
        elif kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            problems.append({"key": key, "check": "type", "field": name})
        elif kind == "object" and not isinstance(value, dict):
            problems.append({"key": key, "check": "type", "field": name})
    if problems:
        return problems

    original = document["source"].get("original")
    if not isinstance(original, dict) or original != originals.get(key):
        problems.append({"key": key, "check": "original_record_preserved"})
        return problems

    # Value: exact rational arithmetic, independent of the build helpers.
    exact = expected_celsius(original)
    if Fraction(document["value"]) != exact:
        problems.append({"key": key, "check": "celsius_value", "expected": str(exact),
                         "found": document["value"]})

    # Time: recompute the instant from the retained original text.
    recomputed = instant_ms(original["time"])
    if recomputed != document["sampled_ms"]:
        problems.append({"key": key, "check": "sampled_ms", "expected": recomputed,
                         "found": document["sampled_ms"]})

    # Identity: key, sensor and source must agree with the raw record.
    if document["source"].get("system") + "~" + document["source"].get("record_key") != key:
        problems.append({"key": key, "check": "document_key"})
    if document["sensor"] != original.get("sensor"):
        problems.append({"key": key, "check": "sensor_mapping"})

    # Conversion record must state unit and basis.
    conversion = document["source"].get("conversion", {})
    if conversion.get("to_unit") != "C" or conversion.get("from_unit") != original.get("unit"):
        problems.append({"key": key, "check": "conversion_basis"})
    if not conversion.get("method"):
        problems.append({"key": key, "check": "conversion_method_missing"})
    return problems


def main():
    scenario = json.loads((OUT / "raw-records.json").read_text(encoding="utf-8"))
    received = [json.loads(line) for line in (OUT / "received.jsonl").read_text(encoding="utf-8").splitlines() if line]
    report = json.loads((OUT / "dispositions.json").read_text(encoding="utf-8"))

    problems = []
    originals = {record["source"]["system"] + "~" + record["record_key"]: record
                 for record in scenario["records"]}

    if len({line["key"] for line in received}) != len(received):
        problems.append({"check": "duplicate_document_keys"})
    for line in received:
        problems.extend(check_document(line, originals))

    accepted = {item["origin_key"] for item in report["dispositions"] if item["decision"] == "accepted"}
    if accepted != {line["key"] for line in received}:
        problems.append({"check": "accepted_set_differs_from_jsonl",
                         "accepted": sorted(accepted), "jsonl": sorted(line["key"] for line in received)})

    pending = [item for item in report["dispositions"] if item["decision"] == "pending"]
    for item in pending:
        if item["origin_key"] in originals and "source" not in item["original"]:
            problems.append({"key": item["origin_key"], "check": "pending_source_key_lost"})
        if "reasons" not in item or not item["reasons"]:
            problems.append({"key": item["origin_key"], "check": "pending_without_reason"})

    schema = report["target_schema"]
    if schema.get("additional") is not False or set(schema.get("required", [])) != REQUIRED:
        problems.append({"check": "target_schema_contract"})
    if set(schema.get("fields", {})) != set(TARGET_FIELDS):
        problems.append({"check": "target_field_set"})

    # Co-temporal cross-source readings must both be present and distinct.
    pairs = {}
    for line in received:
        pairs.setdefault((line["document"]["sensor"], line["document"]["sampled_ms"]), []).append(line)
    for (sensor, sampled), group in pairs.items():
        if len(group) > 1:
            systems = {line["document"]["source"]["system"] for line in group}
            if len(systems) != len(group):
                problems.append({"sensor": sensor, "sampled_ms": sampled, "check": "sources_collide"})

    with tempfile.TemporaryDirectory(prefix="strataleaf-verify-") as directory:
        path = Path(directory) / "verify.db"
        with Database(path) as db:
            db.catalog.create(TARGET_COLLECTION, schema)
            imported = db.transfer.import_lines(
                TARGET_COLLECTION, [json.dumps(line, ensure_ascii=False) for line in received])
            readback = {row.key: row.document for row in db.query(TARGET_COLLECTION, None, limit=1000).records}
            if readback != {line["key"]: line["document"] for line in received}:
                problems.append({"check": "import_readback_differs"})
            inspection = db.diagnostics.inspect(deep=True)
            if not inspection["ok"]:
                problems.append({"check": "inspection", "issues": inspection["issues"]})
            if imported.count != len(received):
                problems.append({"check": "import_count", "count": imported.count})

    print(json.dumps({"ok": not problems, "documents": len(received), "pending": len(pending),
                      "problems": problems}, ensure_ascii=False))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
