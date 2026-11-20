"""Grouped aggregation with explicit, selectable numeric semantics.

Every aggregation runs against one MVCC read revision.  The numeric mode
decides how measure values are interpreted::

    native  Python int/float accumulation; pure-integer groups stay arbitrary
            precision, mixed groups require integers to be exactly binary64
    float   every measure is binary64; integers must be exactly representable
    exact   integers and binary64 floats are accumulated as exact rationals

A group whose measures contain inapplicable or unrepresentable values never
reports a partial sum as if it were complete.  Such values are attributed to
the group, document key and revision, and the affected results are ``None``.
Missing and JSON-null measures are tracked separately because they are normal
exclusions, not errors.
"""
import copy
import math
from fractions import Fraction

from .codec import MISSING, dumps, field, loads
from .errors import AggregateError, InvalidDocument, NotFound
from .mvcc import checked_revision, visible_rows
from .predicates import matches, validate_predicate

MODES = ("native", "float", "exact")
OPERATIONS = ("sum", "average", "min", "max")

_SEMANTICS = {
    "native": {
        "mode": "native",
        "summary": ("Python numeric-tower accumulation: the historical simple path, but a group that "
                    "mixes integers with floats refuses integers binary64 cannot represent exactly."),
        "supported_operations": list(OPERATIONS),
        "inputs": ("arbitrary-size integers and finite binary64 floats; when a group mixes "
                   "integers with floats, each integer must be exactly representable as binary64"),
        "sum": {"supported": True, "representation": "int when every measure is an int, float when a float is present"},
        "average": {"supported": True, "representation": "float; None when the quotient cannot be represented"},
        "min": {"supported": True, "representation": "the original int or float value"},
        "max": {"supported": True, "representation": "the original int or float value"},
        "empty": "all numeric results are None when a group has no valid measure",
        "inapplicable": "booleans and non-numeric measures are reported as attributed errors and withhold every numeric result for that group",
        "unrepresentable": "a binary64 sum that becomes non-finite, an integer that cannot share a float group exactly, or an average that overflows is attributed and not returned",
    },
    "float": {
        "mode": "float",
        "summary": "IEEE-754 binary64 semantics for inputs, sums, averages and extrema.",
        "supported_operations": list(OPERATIONS),
        "inputs": "finite JSON numbers; integers must be exactly representable as binary64",
        "sum": {"supported": True, "representation": "float; None when the sum is not finite"},
        "average": {"supported": True, "representation": "float; None when the quotient is not finite"},
        "min": {"supported": True, "representation": "float"},
        "max": {"supported": True, "representation": "float"},
        "empty": "all numeric results are None when a group has no valid measure",
        "inapplicable": "booleans and non-numeric measures are reported as attributed errors and withhold every numeric result for that group",
        "unrepresentable": "integers that round as binary64 and non-finite sums are attributed and not returned",
    },
    "exact": {
        "mode": "exact",
        "summary": "Exact rational accumulation; no rounding, overflow or cancellation loss.",
        "supported_operations": list(OPERATIONS),
        "inputs": "any finite JSON number; a float is interpreted as its exact binary64 value",
        "sum": {"supported": True, "representation": "int when integral, otherwise {numerator, denominator}"},
        "average": {"supported": True, "representation": "int when integral, otherwise {numerator, denominator}"},
        "min": {"supported": True, "representation": "the original int or float value"},
        "max": {"supported": True, "representation": "the original int or float value"},
        "empty": "all numeric results are None when a group has no valid measure",
        "inapplicable": "booleans and non-numeric measures are reported as attributed errors and withhold every numeric result for that group",
        "unrepresentable": "none for finite inputs; sums and averages are returned as exact rationals so they remain JSON-safe",
    },
}


def _value_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _scalar_value(value):
    """Keep attribution useful without copying large containers into errors."""
    return value if isinstance(value, (str, bool, int, float)) else None


def _as_exact_float(value):
    """Return binary64(value) only when it represents the integer exactly."""
    try:
        converted = float(value)
    except OverflowError:
        return None
    if not math.isfinite(converted):
        return None
    return converted if int(converted) == value else None


def _exact_number(value):
    return Fraction(value) if isinstance(value, int) else Fraction.from_float(value)


def _exact_result(value):
    """Render an exact rational without losing precision or emitting JSON floats."""
    if value.denominator == 1:
        return int(value)
    return {"numerator": value.numerator, "denominator": value.denominator}


def _operation(status, reason=None):
    result = {"status": status}
    if reason is not None:
        result["reason"] = reason
    return result


class Aggregates:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _checked_mode(numeric):
        if not isinstance(numeric, str) or numeric not in _SEMANTICS:
            raise InvalidDocument("numeric mode must be one of: " + ", ".join(MODES))
        return numeric

    def semantics(self, numeric=None):
        """Return the declared support range and result representation for a mode."""
        if numeric is None:
            return copy.deepcopy(_SEMANTICS)
        return copy.deepcopy(_SEMANTICS[self._checked_mode(numeric)])

    def group(self, collection, by, measure=None, predicate=None, revision=None,
              numeric="native", on_error="report"):
        field({}, by)
        if measure is not None:
            field({}, measure)
        validate_predicate(predicate)
        mode = self._checked_mode(numeric)
        if on_error not in ("report", "raise"):
            raise InvalidDocument("on_error must be 'report' or 'raise'")
        with self.store.read() as connection:
            revision = checked_revision(self.store, revision)
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection")
            groups = {}
            skipped = 0
            documents = 0
            for row in visible_rows(connection, collection, revision):
                document = loads(row["body"])
                if not matches(document, predicate):
                    continue
                group_value = field(document, by)
                if group_value is MISSING:
                    skipped += 1
                    continue
                token = dumps(group_value)
                item = groups.get(token)
                if item is None:
                    item = groups[token] = {
                        "key": group_value, "count": 0, "missing": 0, "null": 0,
                        "invalid": 0, "values": [], "errors": [],
                    }
                item["count"] += 1
                documents += 1
                if measure is None:
                    continue
                value = field(document, measure)
                if value is MISSING:
                    item["missing"] += 1
                elif value is None:
                    item["null"] += 1
                elif isinstance(value, bool):
                    self._measure_error(collection, item, row, measure, "boolean_measure", value)
                elif isinstance(value, (int, float)):
                    item["values"].append({"key": row["key"], "revision": row["rev"], "value": value})
                else:
                    self._measure_error(collection, item, row, measure, "non_numeric_measure", value)

            output = []
            errors = []
            valid_measures = missing_measures = null_measures = invalid_measures = 0
            for token in sorted(groups):
                group = self._finalize(collection, groups[token], mode, measure)
                output.append(group)
                errors.extend(group["errors"])
                valid_measures += group["numeric_count"]
                missing_measures += group["missing_measure_count"]
                null_measures += group["null_measure_count"]
                invalid_measures += group["invalid_measure_count"]
            attributed = sum(1 for error in errors if error["scope"] == "measure")
            arithmetic = len(errors) - attributed
            summary = {
                "group_count": len(output),
                "document_count": documents,
                "skipped_missing_group_count": skipped,
                "numeric_count": valid_measures,
                "missing_measure_count": missing_measures,
                "null_measure_count": null_measures,
                "invalid_measure_count": invalid_measures,
                "attributed_error_count": attributed,
                "aggregate_error_count": arithmetic,
                "error_count": len(errors),
                "complete": not errors,
            }
            result = {
                "revision": revision,
                "selection": {"collection": collection, "by": by, "measure": measure, "predicate": predicate},
                "numeric": self.semantics(mode),
                "summary": summary,
                "groups": output,
                "errors": errors,
            }
        if on_error == "raise" and errors:
            raise AggregateError(
                "aggregation has %d attribution/arithmetic error(s); use on_error='report' to inspect them" % len(errors),
                errors=errors, summary=summary, revision=revision)
        return result

    @staticmethod
    def _measure_error(collection, item, row, measure, reason, value):
        item["invalid"] += 1
        item["errors"].append({
            "scope": "measure",
            "collection": collection,
            "group": item["key"],
            "key": row["key"],
            "revision": row["rev"],
            "field": measure,
            "reason": reason,
            "value_type": _value_type(value),
            "value": _scalar_value(value),
        })

    @staticmethod
    def _aggregate_error(collection, item, measure, operation, reason):
        item["errors"].append({
            "scope": "aggregate",
            "collection": collection,
            "group": item["key"],
            "key": None,
            "revision": None,
            "field": measure,
            "operation": operation,
            "reason": reason,
        })

    def _convert(self, collection, item, mode, measure):
        """Return values usable by the mode, attributing unrepresentable ones."""
        converted = []
        native_has_float = any(isinstance(entry["value"], float) for entry in item["values"])
        for entry in item["values"]:
            value = entry["value"]
            row = {"key": entry["key"], "rev": entry["revision"]}
            if isinstance(value, float) and not math.isfinite(value):
                self._measure_error(collection, item, row, measure, "non_finite_measure", value)
                continue
            if mode == "float":
                if isinstance(value, int):
                    number = _as_exact_float(value)
                    if number is None:
                        self._measure_error(collection, item, row, measure, "measure_not_representable", value)
                        continue
                else:
                    number = value
                converted.append((entry, number))
            elif mode == "native":
                if native_has_float and isinstance(value, int) and _as_exact_float(value) is None:
                    self._measure_error(collection, item, row, measure, "measure_not_representable", value)
                    continue
                converted.append((entry, value))
            else:
                converted.append((entry, _exact_number(value)))
        return converted

    def _finalize(self, collection, item, mode, measure):
        converted = [] if measure is None else self._convert(collection, item, mode, measure)
        group = {
            "key": item["key"],
            "count": item["count"],
            "numeric_count": len(converted),
            "missing_measure_count": item["missing"],
            "null_measure_count": item["null"],
            "invalid_measure_count": item["invalid"],
            "sum": None,
            "average": None,
            "min": None,
            "max": None,
            "applicable": False,
            "complete": False,
            "operations": {operation: _operation("not_requested") for operation in OPERATIONS},
        }
        if measure is None:
            group["errors"] = list(item["errors"])
            group["complete"] = True
            return group
        if item["errors"]:
            group["errors"] = list(item["errors"])
            group["operations"] = {operation: _operation("error", "measure_errors") for operation in OPERATIONS}
            return group

        if not converted:
            group["errors"] = list(item["errors"])
            group["operations"] = {operation: _operation("empty", "no_valid_measures") for operation in OPERATIONS}
            group["complete"] = True
            return group
        group["applicable"] = True

        if mode == "exact":
            total = sum((number for _, number in converted), Fraction(0))
            average = total / len(converted)
            group["sum"] = _exact_result(total)
            group["average"] = _exact_result(average)
            group["min"] = min(entry["value"] for entry, _ in converted)
            group["max"] = max(entry["value"] for entry, _ in converted)
            group["operations"] = {operation: _operation("ok") for operation in OPERATIONS}
            group["errors"] = list(item["errors"])
            group["complete"] = True
            return group

        total = 0
        for _, number in converted:
            total += number
        if isinstance(total, float) and not math.isfinite(total):
            self._aggregate_error(collection, item, measure, "sum", "sum_not_finite")
            group["operations"]["sum"] = _operation("error", "sum_not_finite")
            group["operations"]["average"] = _operation("error", "average_requires_sum")
        else:
            group["sum"] = total
            group["operations"]["sum"] = _operation("ok")
            average = None
            try:
                average = total / len(converted)
            except OverflowError:
                pass
            if average is None or not math.isfinite(average):
                self._aggregate_error(collection, item, measure, "average", "average_not_representable")
                group["operations"]["average"] = _operation("error", "average_not_representable")
            else:
                group["average"] = average
                group["operations"]["average"] = _operation("ok")

        if mode == "float":
            numbers = [number for _, number in converted]
        else:
            numbers = [entry["value"] for entry, _ in converted]
        group["min"] = min(numbers)
        group["max"] = max(numbers)
        group["operations"]["min"] = _operation("ok")
        group["operations"]["max"] = _operation("ok")
        group["errors"] = list(item["errors"])
        group["complete"] = not group["errors"]
        return group
