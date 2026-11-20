import hashlib
import math
from dataclasses import dataclass

from .codec import digest, dumps, key as checked_key, loads
from .cursors import decode_cursor, encode_cursor
from .errors import InvalidCursor, InvalidDocument, NotFound, StalePreview
from .mvcc import checked_revision, latest_row, record, visible_rows
from .schema import validate_document
from .transaction import WriteBatch

MAX_LINES = 10000
MAX_LINE_BYTES = 2 * 1024 * 1024

DECISIONS = ("keep", "replace", "drop")
EXISTING_POLICIES = ("keep", "replace", "drop")
DUPLICATE_POLICIES = ("first", "last", "drop")
INCOMPATIBLE_POLICIES = ("reject", "drop")

PREVIEW_KIND = "transfer-preview"


@dataclass(frozen=True)
class ImportPreview:
    """A read-snapshot classification of an incoming document stream.

    The object is JSON-friendly through ``to_dict`` so it can be inspected,
    stored for review, and later handed back to ``Transfer.apply_preview``.
    """
    collection: str
    revision: int
    generation: int
    database_id: str
    input_digest: str
    entries: list
    counts: dict
    token: str

    def summary(self):
        conflicts = [dict(entry) for entry in self.entries if entry["disposition"] != "create"]
        for entry in conflicts:
            entry.pop("document", None)
        return {"collection": self.collection, "revision": self.revision,
                "generation": self.generation, "counts": dict(self.counts), "conflicts": conflicts}

    def to_dict(self, include_documents=True):
        """Lossless mapping form accepted by ``apply_preview``.

        ``database_id`` is included so the token can be traced to its database;
        keep documents (the default) when the mapping will be confirmed later,
        and use ``summary()`` for a display form without document bodies.
        """
        entries = []
        for entry in self.entries:
            item = dict(entry)
            if not include_documents:
                item.pop("document", None)
            entries.append(item)
        return {"collection": self.collection, "revision": self.revision,
                "generation": self.generation, "database_id": self.database_id,
                "input_digest": self.input_digest, "entries": entries,
                "counts": dict(self.counts), "token": self.token}


@dataclass(frozen=True)
class ImportResult:
    revision: int | None
    count: int
    replayed: bool
    written: list
    skipped: list

    @property
    def skipped_keys(self):
        return sorted({item["key"] for item in self.skipped if item.get("key") is not None})


def _plan_payload(collection, revision, generation, input_digest, entries):
    return {"collection": collection, "revision": revision, "generation": generation,
            "input_digest": input_digest, "entries": entries}


def _coerce_preview(value):
    if isinstance(value, ImportPreview):
        return value
    if not isinstance(value, dict):
        raise InvalidDocument("preview must be an ImportPreview or its mapping form")
    required = {"collection", "revision", "generation", "database_id", "input_digest", "entries", "token"}
    if not required.issubset(value):
        raise InvalidDocument("preview mapping is missing required fields")
    for field_name in ("database_id", "input_digest", "token"):
        if not isinstance(value[field_name], str) or not value[field_name]:
            raise InvalidDocument("preview " + field_name + " must be a nonempty string")
    for field_name in ("revision", "generation"):
        number = value[field_name]
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise InvalidDocument("preview " + field_name + " must be a nonnegative integer")
    entries = value["entries"]
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise InvalidDocument("preview entries must be an array of objects")
    for entry in entries:
        if entry.get("disposition") not in {"create", "existing", "duplicate", "incompatible"}:
            raise InvalidDocument("preview entry has an unknown disposition")
        line = entry.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise InvalidDocument("preview entry needs a positive line number")
        if entry["disposition"] == "incompatible":
            continue
        checked_key(entry.get("key"))
        current = entry.get("current_revision")
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise InvalidDocument("preview entry needs a nonnegative current revision")
        if not isinstance(entry.get("document"), dict):
            raise InvalidDocument("preview entry needs a document object")
        expires_at = entry.get("expires_at")
        if expires_at is not None and (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                                       or not math.isfinite(expires_at)):
            raise InvalidDocument("preview entry has an invalid expires_at")
        if entry["disposition"] == "duplicate" and entry.get("base") not in {"create", "existing"}:
            raise InvalidDocument("duplicate preview entry needs a create/existing base")
    counts = value.get("counts")
    if not isinstance(counts, dict):
        counts = _counts(entries)
    return ImportPreview(value["collection"], value["revision"], value["generation"],
                         value["database_id"], value["input_digest"], entries, counts, value["token"])


def _counts(entries):
    counts = {"total": len(entries), "create": 0, "existing": 0, "duplicate": 0, "incompatible": 0}
    for entry in entries:
        counts[entry["disposition"]] += 1
    return counts


class Transfer:
    def __init__(self, store):
        self.store = store

    def export_lines(self, collection, revision=None):
        with self.store.read() as connection:
            revision = checked_revision(self.store, revision)
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection")
            output = []
            for row in visible_rows(connection, collection, revision):
                item = record(row)
                output.append(dumps({"key": item.key, "document": item.document, "expires_at": item.expires_at}))
            return {"revision": revision, "lines": output}

    def preview_lines(self, collection, lines):
        """Classify an incoming stream without writing anything.

        Each input line becomes one entry: ``create`` (key absent),
        ``existing`` (key already present, including tombstones), ``duplicate``
        (the key occurs more than once in the input), or ``incompatible``
        (malformed line or a document rejected by the current schema). The
        returned preview is bound to this database, collection, read revision,
        schema generation and the exact input digest by a signed token.
        """
        try:
            items = list(lines)
        except TypeError as exc:
            raise InvalidDocument("import input must be an iterable of lines") from exc
        if len(items) > MAX_LINES:
            raise InvalidDocument("import exceeds bounded batch/line size")

        input_hash = hashlib.sha256()
        parsed = []
        invalid = []
        for number, line in enumerate(items, 1):
            encoded = line.encode() if isinstance(line, str) else ("nonstring:" + repr(line)).encode()
            input_hash.update(len(encoded).to_bytes(8, "big"))
            input_hash.update(encoded)
            if not isinstance(line, str) or len(encoded) > MAX_LINE_BYTES:
                invalid.append({"line": number, "key": None, "disposition": "incompatible",
                                "reason": "line must be a string of at most 2 MiB"})
                continue
            try:
                item = loads(line)
            except (ValueError, TypeError):
                invalid.append({"line": number, "key": None, "disposition": "incompatible",
                                "reason": "invalid JSON"})
                continue
            if not isinstance(item, dict) or set(item) - {"key", "document", "expires_at"} or not {"key", "document"}.issubset(item):
                invalid.append({"line": number, "key": None, "disposition": "incompatible",
                                "reason": "line requires key and document, optional expires_at"})
                continue
            expires_at = item.get("expires_at")
            if expires_at is not None and (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                                           or not math.isfinite(expires_at)):
                invalid.append({"line": number, "key": None, "disposition": "incompatible",
                                "reason": "expires_at must be a finite timestamp"})
                continue
            try:
                item_key = checked_key(item["key"])
                if not isinstance(item["document"], dict):
                    raise InvalidDocument("document must be an object")
                document = loads(dumps(item["document"]))
            except InvalidDocument as exc:
                invalid.append({"line": number, "key": item["key"] if isinstance(item["key"], str) else None,
                                "disposition": "incompatible", "reason": str(exc)})
                continue
            parsed.append((number, item_key, document, expires_at))

        with self.store.read() as connection:
            collection_row = connection.execute("SELECT schema,generation FROM collections WHERE name=?",
                                                (collection,)).fetchone()
            if collection_row is None:
                raise NotFound("unknown collection: " + collection)
            schema = loads(collection_row["schema"])
            revision = int(self.store.get("head"))
            grouped = {}
            for number, item_key, document, expires_at in parsed:
                grouped.setdefault(item_key, []).append((number, document, expires_at))

            by_line = {entry["line"]: entry for entry in invalid}
            for item_key, occurrences in grouped.items():
                for number, document, expires_at in occurrences:
                    try:
                        validate_document(document, schema)
                    except InvalidDocument as exc:
                        by_line[number] = {"line": number, "key": item_key, "disposition": "incompatible",
                                           "reason": str(exc)}
            for item_key, occurrences in grouped.items():
                row = latest_row(connection, collection, item_key, revision)
                current = row["rev"] if row is not None else 0
                base = "existing" if row is not None else "create"
                compatible = [item for item in occurrences if item[0] not in by_line]
                for index, (number, document, expires_at) in enumerate(compatible):
                    entry = {"line": number, "key": item_key, "current_revision": current,
                             "document": document, "expires_at": expires_at}
                    if len(compatible) > 1:
                        entry.update({"disposition": "duplicate", "base": base,
                                      "occurrence": index + 1, "occurrences": len(compatible)})
                    else:
                        entry["disposition"] = base
                    by_line[number] = entry

        entries = [by_line[number] for number in sorted(by_line)]
        counts = _counts(entries)
        counts["compatible_keys"] = sum(1 for group in grouped.values()
                                        if any(number not in by_line or by_line[number].get("disposition") != "incompatible"
                                               for number, _, _ in group))
        database_id = self.store.get("database_id")
        plan = _plan_payload(collection, revision, collection_row["generation"], input_hash.hexdigest(), entries)
        token = encode_cursor(self.store, {"database": database_id, "kind": PREVIEW_KIND,
                                           "plan": digest(plan)})
        return ImportPreview(collection, revision, collection_row["generation"], database_id,
                             input_hash.hexdigest(), entries, counts, token)

    def preview_summary(self, preview):
        return _coerce_preview(preview).summary()

    def apply_preview(self, preview, on_existing="keep", on_duplicate="first",
                      on_incompatible="reject", decisions=None, request_key=None):
        """Apply a confirmed preview in one atomic write batch.

        ``on_existing`` accepts ``keep``, ``replace`` or ``drop``;
        ``on_duplicate`` accepts ``first``, ``last`` or ``drop``;
        ``on_incompatible`` accepts ``reject`` (default) or ``drop``.
        ``decisions`` maps a document key to ``keep``, ``replace`` or ``drop``
        and overrides the class-level policies for that key. ``first``/``last``
        are relative to the compatible occurrences of a duplicated key.

        Before writing, the batch re-reads the collection generation and every
        key it is about to write inside the same IMMEDIATE transaction. If any
        of them moved since the preview, ``StalePreview`` is raised and nothing
        is written, so a newer version is never silently overwritten.
        """
        preview = _coerce_preview(preview)
        if on_existing not in EXISTING_POLICIES:
            raise InvalidDocument("on_existing accepts keep, replace or drop")
        if on_duplicate not in DUPLICATE_POLICIES:
            raise InvalidDocument("on_duplicate accepts first, last or drop")
        if on_incompatible not in INCOMPATIBLE_POLICIES:
            raise InvalidDocument("on_incompatible accepts reject or drop")
        if decisions is None:
            decisions = {}
        if not isinstance(decisions, dict):
            raise InvalidDocument("decisions must map document keys to keep, replace or drop")
        for item_key, decision in decisions.items():
            checked_key(item_key)
            if decision not in DECISIONS:
                raise InvalidDocument("decisions accept keep, replace or drop")

        self._verify(preview)

        compatible = {}
        incompatible = []
        for entry in preview.entries:
            if entry["disposition"] == "incompatible":
                incompatible.append(entry)
            else:
                compatible.setdefault(entry["key"], []).append(entry)

        written = []
        skipped = []
        operations = []

        for entry in incompatible:
            explicit = decisions.get(entry.get("key"))
            if explicit != "drop" and on_incompatible == "reject":
                raise InvalidDocument(f"incompatible import input at line {entry['line']}: {entry['reason']}")
            skipped.append({"line": entry["line"], "key": entry.get("key"), "reason": "incompatible",
                            "detail": entry["reason"]})

        for item_key, group in compatible.items():
            base = group[0].get("base", group[0]["disposition"])
            current = group[0]["current_revision"]
            survivor, outcome = self._resolve(item_key, group, base, decisions.get(item_key),
                                              on_existing, on_duplicate)
            if survivor is not None:
                action = "replace" if base == "existing" else "create"
                operations.append((item_key, survivor["document"], survivor["expires_at"], current))
                written.append({"line": survivor["line"], "key": item_key, "action": action,
                                "previous_revision": current})
                for entry in group:
                    if entry["line"] != survivor["line"]:
                        skipped.append({"line": entry["line"], "key": item_key, "reason": "duplicate-superseded",
                                        "detail": "kept line " + str(survivor["line"])})
            else:
                for entry in group:
                    skipped.append({"line": entry["line"], "key": item_key, "reason": outcome})

        written.sort(key=lambda item: item["line"])
        skipped.sort(key=lambda item: item["line"])
        expected = {item_key: current for item_key, document, expires_at, current in operations}
        guard = self._guard(preview, expected)
        context = {"preview": preview.token, "policies": {"existing": on_existing, "duplicate": on_duplicate,
                    "incompatible": on_incompatible}, "decisions": dict(decisions),
                    "written": [[item["line"], item["key"], item["action"]] for item in written],
                    "skipped": [[item["line"], item["key"]] for item in skipped]}

        if not operations:
            # Nothing is written, so a concurrent change cannot be overwritten.
            return ImportResult(None, 0, False, written, skipped)

        batch = WriteBatch(self.store, request_key, guard=guard, context=context,
                           note=[{"line": item["line"], "key": item["key"], "reason": item["reason"]}
                                 for item in skipped])
        for item_key, document, expires_at, current in operations:
            batch.put(preview.collection, item_key, document, expected=current, expires_at=expires_at)
        result = batch.commit()
        return ImportResult(result.revision, result.count, result.replayed, written, skipped)

    def _resolve(self, item_key, group, base, explicit, on_existing, on_duplicate):
        if explicit == "drop":
            return None, "dropped"
        if explicit == "keep":
            return (None, "existing-kept") if base == "existing" else (group[0], "created")
        if explicit == "replace":
            return group[-1], "replaced-source"
        if len(group) > 1 and on_duplicate == "drop":
            return None, "duplicate-dropped"
        survivor = group[-1] if len(group) > 1 and on_duplicate == "last" else group[0]
        if base == "existing":
            if on_existing == "replace":
                return survivor, "replaced-source"
            if on_existing == "drop":
                return None, "dropped"
            return None, "existing-kept"
        return survivor, "created"

    def _verify(self, preview):
        try:
            payload = decode_cursor(self.store, preview.token)
        except InvalidCursor as exc:
            raise StalePreview("import preview token is invalid or belongs to another database") from exc
        if payload.get("kind") != PREVIEW_KIND:
            raise StalePreview("confirmation token is not an import preview")
        plan = _plan_payload(preview.collection, preview.revision, preview.generation,
                             preview.input_digest, preview.entries)
        if payload.get("plan") != digest(plan):
            raise StalePreview("import preview was modified after it was issued; preview again")

    def _guard(self, preview, expected):
        def guard(connection):
            row = connection.execute("SELECT generation FROM collections WHERE name=?",
                                     (preview.collection,)).fetchone()
            if row is None:
                raise StalePreview("collection disappeared after import preview: " + preview.collection)
            if row["generation"] != preview.generation:
                raise StalePreview("collection schema changed after import preview: generation "
                                   f"{preview.generation} -> {row['generation']}")
            head = int(self.store.get("head"))
            for item_key, revision in expected.items():
                latest = latest_row(connection, preview.collection, item_key, head)
                found = latest["rev"] if latest is not None else 0
                if found != revision:
                    raise StalePreview(f"import preview is stale for {preview.collection}/{item_key}: "
                                       f"previewed revision {revision}, found {found}")
        return guard

    def import_lines(self, collection, lines, replace=False, request_key=None):
        if not isinstance(replace, bool):
            raise InvalidDocument("replace must be a boolean")
        batch = WriteBatch(self.store, request_key)
        count = 0
        for line in lines:
            count += 1
            if count > MAX_LINES or not isinstance(line, str) or len(line.encode()) > MAX_LINE_BYTES:
                raise InvalidDocument("import exceeds bounded batch/line size")
            try:
                item = loads(line)
            except (ValueError, TypeError) as exc:
                raise InvalidDocument("invalid JSON at input line " + str(count)) from exc
            if not isinstance(item, dict) or set(item) - {"key", "document", "expires_at"} or not {"key", "document"}.issubset(item):
                raise InvalidDocument("line requires key and document, optional expires_at")
            batch.put(collection, item["key"], item["document"], expected=None if replace else 0,
                      expires_at=item.get("expires_at"))
        return batch.commit()
