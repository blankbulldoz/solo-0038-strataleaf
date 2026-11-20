from .codec import MISSING, dumps, field, loads
from .mvcc import ReadView
from .predicates import matches, validate_predicate


class Aggregates:
    def __init__(self, store):
        self.store = store

    def group(self, collection, by, measure=None, predicate=None, revision=None):
        field({}, by)
        if measure is not None:
            field({}, measure)
        validate_predicate(predicate)
        with ReadView(self.store, revision) as view:
            revision = view.fix_revision()
            view.require_collection(collection)
            groups = {}
            for row in view.rows(collection):
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
