from .codec import MISSING, dumps, field, loads
from .errors import InvalidDocument, NotFound
from .mvcc import checked_revision, visible_rows
from .predicates import matches, validate_predicate

ARRAY_MODES = ("whole", "members")
MISSING_POLICIES = ("separate", "merge", "exclude")

# Missing and merged missing/null need identities that no JSON value can
# produce. Present values are always "value:" followed by canonical JSON, while
# these sentinels are bare tokens, so a document string such as "missing" maps
# to 'value:"missing"' and never collides.
MISSING_IDENTITY = ("missing", "missing", "<missing>", None)
NULLISH_IDENTITY = ("nullish", "nullish", "<null or missing>", None)

EXCLUSION_REASONS = ("missing_group", "null_group", "empty_array")
MEASURE_STATUSES = ("numeric", "missing", "null", "nonnumeric")


def _numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _display(value):
    # Labels are for humans and may deliberately coincide: the string "1" and
    # the number 1 both display as 1. Group identity comes from _identity, never
    # from this label.
    return value if isinstance(value, str) else dumps(value)


def _identity(value):
    return ("value:" + dumps(value), _kind(value), _display(value), value)


def _new_group(identity):
    identity_token, kind, label, key = identity
    return {"id": identity_token, "kind": kind, "label": label, "key": key,
            "contributions": 0, "numeric_contributions": 0,
            "sum": 0, "min": None, "max": None, "duplicate_contributions": 0}


def _observe(group, number):
    group["contributions"] += 1
    if _numeric(number):
        group["numeric_contributions"] += 1
        group["sum"] += number
        group["min"] = number if group["min"] is None else min(group["min"], number)
        group["max"] = number if group["max"] is None else max(group["max"], number)


def _measure_status(document, measure):
    if measure is None:
        return None
    value = field(document, measure)
    if value is MISSING:
        return "missing"
    if value is None:
        return "null"
    return "numeric" if _numeric(value) else "nonnumeric"


class Aggregates:
    def __init__(self, store):
        self.store = store

    def group(self, collection, by, measure=None, predicate=None, revision=None):
        """The original single-group aggregation contract.

        A document belongs to at most one group; a missing group field is
        skipped; explicit null is grouped; an array is grouped as one value.
        Use report() for member expansion, missing/null policies, accounting
        and per-document provenance.
        """
        field({}, by)
        if measure is not None:
            field({}, measure)
        validate_predicate(predicate)
        with self.store.read() as connection:
            revision = checked_revision(self.store, revision)
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection")
            groups = {}
            for row in visible_rows(connection, collection, revision):
                document = loads(row["body"])
                if not matches(document, predicate):
                    continue
                value = field(document, by)
                if value is MISSING:
                    continue
                token = dumps(value)
                item = groups.setdefault(token, {"key": value, "count": 0, "numeric_count": 0, "sum": 0, "min": None, "max": None})
                item["count"] += 1
                number = field(document, measure) if measure is not None else MISSING
                if isinstance(number, (int, float)) and not isinstance(number, bool):
                    item["numeric_count"] += 1
                    item["sum"] += number
                    item["min"] = number if item["min"] is None else min(item["min"], number)
                    item["max"] = number if item["max"] is None else max(item["max"], number)
            output = []
            for token in sorted(groups):
                item = groups[token]
                item["average"] = item["sum"] / item["numeric_count"] if item["numeric_count"] else None
                output.append(item)
            return {"revision": revision, "groups": output}

    def report(self, collection, by, measure=None, predicate=None, revision=None,
               arrays="whole", missing="separate", trace=False):
        """Build an auditable grouping report over one fixed read revision.

        arrays="members" expands a tag array so one document can contribute to
        several groups; a repeated value inside one document contributes once.
        arrays="whole" keeps the original behaviour of grouping an array as one
        value. missing selects how the group field's absent value and explicit
        null are treated: "separate", "merge" or "exclude".

        Accounting keeps separate facts that are easy to confuse:

        - scanned / filtered / matched document counts;
        - covered_documents: matched documents that reached at least one group
          (this is the deduplicated coverage), plus excluded_documents;
        - contributions: group entries written across all documents. One
          document in several groups raises this above covered_documents, so the
          sum of group contributions must never be reported as coverage;
        - numeric_documents: distinct documents with a valid numeric measure;
          numeric_contributions: numeric group entries. A numeric document that
          joins several groups is one numeric document but several numeric
          contributions.

        Group entries expose contributions and numeric_contributions rather than
        an ambiguous count. trace=True adds one row per scanned document
        recording whether it was filtered by the predicate, excluded (with the
        reason) or grouped (with its group ids and duplicate labels).
        """
        field({}, by)
        if measure is not None:
            field({}, measure)
        validate_predicate(predicate)
        if arrays not in ARRAY_MODES:
            raise InvalidDocument("arrays must be one of: " + ", ".join(ARRAY_MODES))
        if missing not in MISSING_POLICIES:
            raise InvalidDocument("missing must be one of: " + ", ".join(MISSING_POLICIES))
        if not isinstance(trace, bool):
            raise InvalidDocument("trace must be a boolean")
        with self.store.read() as connection:
            revision = checked_revision(self.store, revision)
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection")
            groups = {}
            accounting = {"scanned_documents": 0, "filtered_documents": 0, "matched_documents": 0,
                          "covered_documents": 0, "excluded_documents": 0, "contributions": 0,
                          "numeric_documents": 0, "numeric_contributions": 0,
                          "duplicate_contributions": 0, "groups": 0}
            excluded = {reason: 0 for reason in EXCLUSION_REASONS}
            measures = {status: 0 for status in MEASURE_STATUSES}
            provenance = []
            for row in visible_rows(connection, collection, revision):
                document = loads(row["body"])
                accounting["scanned_documents"] += 1
                if not matches(document, predicate):
                    accounting["filtered_documents"] += 1
                    if trace:
                        provenance.append({"key": row["key"], "outcome": "filtered", "reason": "predicate",
                                           "groups": [], "duplicate_contributions": 0,
                                           "measure": _measure_status(document, measure)})
                    continue
                accounting["matched_documents"] += 1
                status = _measure_status(document, measure)
                if measure is not None:
                    measures[status] += 1

                value = field(document, by)
                selected = {}
                duplicates = 0
                reason = None
                if value is MISSING:
                    if missing == "separate":
                        selected[MISSING_IDENTITY[0]] = [MISSING_IDENTITY, 0]
                    elif missing == "merge":
                        selected[NULLISH_IDENTITY[0]] = [NULLISH_IDENTITY, 0]
                    else:
                        reason = "missing_group"
                elif value is None:
                    if missing == "merge":
                        selected[NULLISH_IDENTITY[0]] = [NULLISH_IDENTITY, 0]
                    elif missing == "exclude":
                        reason = "null_group"
                    else:
                        identity = _identity(None)
                        selected[identity[0]] = [identity, 0]
                elif isinstance(value, list) and arrays == "members":
                    for member in value:
                        identity = _identity(member)
                        if identity[0] in selected:
                            selected[identity[0]][1] += 1
                            duplicates += 1
                        else:
                            selected[identity[0]] = [identity, 0]
                    if not selected:
                        reason = "empty_array"
                else:
                    identity = _identity(value)
                    selected[identity[0]] = [identity, 0]

                if reason is not None:
                    accounting["excluded_documents"] += 1
                    excluded[reason] += 1
                else:
                    number = field(document, measure) if measure is not None else MISSING
                    accounting["covered_documents"] += 1
                    accounting["contributions"] += len(selected)
                    accounting["duplicate_contributions"] += duplicates
                    if status == "numeric":
                        accounting["numeric_documents"] += 1
                        accounting["numeric_contributions"] += len(selected)
                    for identity_token in selected:
                        identity, repeated = selected[identity_token]
                        group = groups.setdefault(identity_token, _new_group(identity))
                        _observe(group, number)
                        group["duplicate_contributions"] += repeated
                if trace:
                    provenance.append({"key": row["key"],
                                       "outcome": "excluded" if reason is not None else "grouped",
                                       "reason": reason,
                                       "groups": sorted(selected),
                                       "duplicate_contributions": duplicates,
                                       "measure": status})
            output = []
            for identity_token in sorted(groups):
                item = groups[identity_token]
                item["average"] = (item["sum"] / item["numeric_contributions"]
                                   if item["numeric_contributions"] else None)
                output.append(item)
            accounting["groups"] = len(output)
            result = {"revision": revision,
                      "policy": {"arrays": arrays, "missing": missing},
                      "accounting": accounting,
                      "excluded": excluded,
                      "measures": measures if measure is not None else None,
                      "groups": output}
            if trace:
                result["trace"] = provenance
            return result
