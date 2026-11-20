from .codec import digest, dumps, key as checked_key, loads, name
from .errors import Conflict, InvalidDocument, NotFound, TransferError
from .mvcc import checked_revision, record, visible_range, visible_stream
from .transaction import WriteBatch

LINE_LIMIT = 2 * 1024 * 1024
MAX_SEGMENTS = 10000


def _checked_export_id(value):
    return checked_key(value) if isinstance(value, str) else None


def _line(item):
    return dumps({"key": item.key, "document": item.document, "expires_at": item.expires_at})


def verify_manifest(manifest, received):
    """Receiver-side check that fetched segments form exactly the frozen export.

    `received` is the sequence of segment envelopes a receiver actually holds.
    Missing, duplicated, reordered or substituted segments raise `TransferError`
    so a half-copied collection is never passed off as a complete one.
    """
    if not isinstance(manifest, dict) or not isinstance(manifest.get("segments"), list):
        raise InvalidDocument("manifest must carry a segment list")
    expected = manifest["segments"]
    if not isinstance(received, list) or len(received) != len(expected):
        detail = "too few" if isinstance(received, list) and len(received) < len(expected) else "too many"
        raise TransferError(f"received {detail} segments: {len(received) if isinstance(received, list) else 0} "
                            f"of {len(expected)}")
    lines = []
    for position, (want, got) in enumerate(zip(expected, received)):
        if not isinstance(got, dict) or got.get("index") != want["index"]:
            raise TransferError(f"segment {position} is out of order or missing")
        if got.get("digest") != want["digest"] or got.get("lines") is None:
            raise TransferError(f"segment {position} does not match its manifest digest")
        if digest(got["lines"]) != want["digest"]:
            raise TransferError(f"segment {position} content was altered in transit")
        if want["documents"] and (got.get("first_key") != want["first_key"] or got.get("last_key") != want["last_key"]):
            raise TransferError(f"segment {position} covers the wrong key range")
        if not isinstance(got["lines"], list) or len(got["lines"]) != want["documents"]:
            raise TransferError(f"segment {position} has the wrong document count")
        lines.extend(got["lines"])
    if digest(lines) != manifest["digest"] or len(lines) != manifest["total_documents"]:
        raise TransferError("combined segments do not match the export digest")
    return lines


class Transfer:
    """Logical document export/import with resumable, manifest-backed segments."""

    def __init__(self, store):
        self.store = store

    # ---- one-shot convenience API (small collections) -------------------

    def export_lines(self, collection, revision=None):
        """Existing interface: materialise the whole collection in one call."""
        with self.store.read() as connection:
            revision = checked_revision(self.store, revision)
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection")
            output = [_line(record(row)) for row in visible_stream(connection, collection, revision)]
            return {"revision": revision, "lines": output}

    def import_lines(self, collection, lines, replace=False, request_key=None):
        if not isinstance(replace, bool):
            raise InvalidDocument("replace must be a boolean")
        batch = WriteBatch(self.store, request_key)
        count = 0
        for line in lines:
            count += 1
            if count > 10000 or not isinstance(line, str) or len(line.encode()) > LINE_LIMIT:
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

    # ---- resumable export sessions --------------------------------------

    def begin(self, collection, revision=None, segment_size=100):
        """Freeze one read revision as a segmented export and pin it for reading.

        The id is stable for (collection, database, revision). Calling `begin`
        again for that revision resumes the recorded export (same manifest and
        progress) instead of silently freezing a different one; an abandoned
        export may be restarted from scratch.
        """
        name(collection)
        if isinstance(segment_size, bool) or not isinstance(segment_size, int) or not 1 <= segment_size <= 10000:
            raise InvalidDocument("segment size must be between 1 and 10000 documents")
        with self.store.atomic(writable=True) as connection:
            revision = checked_revision(self.store, revision)
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection")
            export_id = collection + "-" + self.store.get("database_id")[:8] + "-" + str(revision)
            prior = connection.execute("SELECT * FROM export_sessions WHERE export_id=?", (export_id,)).fetchone()
            if prior and prior["state"] == "abandoned":
                # Abandoned exports may be restarted at the same revision; the
                # fresh pin is reinserted with the same stable identity.
                connection.execute("DELETE FROM export_segments WHERE export_id=?", (export_id,))
                connection.execute("DELETE FROM export_sessions WHERE export_id=?", (export_id,))
                prior = None
            if prior and prior["segment_size"] != segment_size:
                raise Conflict("export already exists with a different segment size")
            if prior:
                # Restarting at the same frozen revision resumes the existing
                # export instead of silently re-freezing a different manifest.
                return self._describe(export_id, connection)
            pin_name = "export:" + export_id
            connection.execute("INSERT INTO pins VALUES (?,?)", (pin_name, revision))
            segments = []
            current = []
            current_bytes = 0
            total = 0
            for row in visible_stream(connection, collection, revision):
                item = record(row)
                line = _line(item)
                size = len(line.encode())
                if size > LINE_LIMIT:
                    raise TransferError("document exceeds export line limit")
                if current and (len(current) >= segment_size or current_bytes + size > LINE_LIMIT):
                    segments.append(current)
                    current, current_bytes = [], 0
                current.append((item.key, line))
                current_bytes += size
                total += 1
            if current:
                segments.append(current)
            if len(segments) > MAX_SEGMENTS:
                raise TransferError("export exceeds the segment limit")
            if not segments:
                # An empty collection still gets one empty segment so a
                # receiver's manifest is never empty and progress is explicit.
                segments = [[]]
            connection.execute("INSERT INTO export_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (export_id, self.store.get("database_id"), collection, revision, pin_name,
                                segment_size, len(segments), total,
                                digest([line for segment in segments for _, line in segment]), "open", 0))
            for index, segment in enumerate(segments):
                lines = [line for _, line in segment]
                connection.execute("INSERT INTO export_segments VALUES (?,?,?,?,?,?)",
                                   (export_id, index, segment[0][0] if segment else "",
                                    segment[-1][0] if segment else "", len(lines), digest(lines)))
            return self._describe(export_id, connection)

    def segments(self, export_id):
        """Return the receiver manifest: ordered segments, key ranges and digests."""
        with self.store.read() as connection:
            return self._describe(export_id, connection, include_segments=True)

    def fetch(self, export_id, index):
        """Fetch one segment.

        Segments are independent and may be retried or re-requested until the
        export is finished; `advance` is what records durable receiver progress.
        An abandoned export whose revision has since been compacted fails with
        an explicit retention error rather than returning a truncated segment.
        """
        export_id = _checked_export_id(export_id)
        if isinstance(index, bool) or not isinstance(index, int):
            raise InvalidDocument("segment index must be an integer")
        with self.store.read() as connection:
            session = self._session(connection, export_id)
            self._check_revision(connection, session)
            row = connection.execute("SELECT * FROM export_segments WHERE export_id=? AND idx=?",
                                     (export_id, index)).fetchone()
            if row is None:
                raise NotFound("unknown export segment")
            lines = [line for _, line in self._segment_lines(connection, session, row)]
            return {"export": export_id, "collection": session["collection"], "revision": session["revision"],
                    "index": index, "first_key": row["first_key"], "last_key": row["last_key"],
                    "documents": row["documents"], "digest": row["digest"], "lines": lines}

    def advance(self, export_id, index):
        """Record that the receiver durably holds segment `index`.

        Progress must arrive in order and without gaps, so a receiver that stops
        halfway can resume at `progress` and a partial transfer is never mistaken
        for a complete collection.
        """
        export_id = _checked_export_id(export_id)
        if isinstance(index, bool) or not isinstance(index, int):
            raise InvalidDocument("segment index must be an integer")
        with self.store.atomic(writable=True) as connection:
            session = self._session(connection, export_id)
            self._check_revision(connection, session)
            if session["state"] != "open":
                raise Conflict("export is " + session["state"])
            if not 0 <= index < session["total_segments"]:
                raise NotFound("unknown export segment")
            progress = session["progress"]
            if index < progress:
                raise Conflict("segment was already received")
            if index > progress:
                raise Conflict("segments must be received in order without gaps")
            progress += 1
            connection.execute("UPDATE export_sessions SET progress=? WHERE export_id=?", (progress, export_id))
            if progress == session["total_segments"]:
                self._verify_complete(connection, session)
                connection.execute("UPDATE export_sessions SET state='complete' WHERE export_id=?", (export_id,))
                connection.execute("DELETE FROM pins WHERE name=?", (session["pin"],))
            return self._describe(export_id, connection)

    def finish(self, export_id, complete=True):
        """End an export and release its read protection.

        `complete=True` succeeds only when every segment was received and the
        combined content still matches the frozen digest. `complete=False`
        abandons a long-stalled export, releasing the pin so compaction can move
        past the revision; further fetch attempts then fail with an explicit
        retention error rather than returning a truncated collection.
        """
        if not isinstance(complete, bool):
            raise InvalidDocument("complete must be a boolean")
        export_id = _checked_export_id(export_id)
        with self.store.atomic(writable=True) as connection:
            session = self._session(connection, export_id)
            if session["state"] == ("complete" if complete else "abandoned"):
                return self._describe(export_id, connection)
            self._check_revision(connection, session)
            if session["state"] != "open":
                raise Conflict("export is " + session["state"])
            if complete:
                if session["progress"] != session["total_segments"]:
                    raise Conflict(f"export has {session['total_segments'] - session['progress']} unreceived segments")
                self._verify_complete(connection, session)
                connection.execute("UPDATE export_sessions SET state='complete' WHERE export_id=?", (export_id,))
            else:
                connection.execute("UPDATE export_sessions SET state='abandoned' WHERE export_id=?", (export_id,))
            connection.execute("DELETE FROM pins WHERE name=?", (session["pin"],))
            return self._describe(export_id, connection)

    def status(self, export_id):
        with self.store.read() as connection:
            return self._describe(_checked_export_id(export_id), connection)

    def list(self):
        with self.store.read() as connection:
            return [self._describe(row[0], connection)
                    for row in connection.execute("SELECT export_id FROM export_sessions ORDER BY export_id")]

    def import_session(self, export_id, collection, replace=False, request_key=None):
        """Import a complete export into `collection` through the normal write path."""
        export_id = _checked_export_id(export_id)
        if not isinstance(replace, bool):
            raise InvalidDocument("replace must be a boolean")
        with self.store.read() as connection:
            session = self._session(connection, export_id)
            if session["state"] != "complete":
                raise Conflict("export is " + session["state"])
            self._check_revision(connection, session)
            if session["total_documents"] > 10000:
                # Import stays one atomic batch; splitting it would allow half a
                # collection to become visible if a later batch failed.
                raise InvalidDocument("export exceeds a single atomic import batch of 10000 documents")
            lines = [line
                     for row in connection.execute("SELECT * FROM export_segments WHERE export_id=? ORDER BY idx", (export_id,))
                     for _, line in self._segment_lines(connection, session, row)]
        return self.import_lines(collection, lines, replace=replace, request_key=request_key)

    # ---- internals -------------------------------------------------------

    def _session(self, connection, export_id):
        row = connection.execute("SELECT * FROM export_sessions WHERE export_id=?", (export_id,)).fetchone()
        if row is None:
            raise NotFound("unknown export")
        if row["database_id"] != self.store.get("database_id"):
            raise TransferError("export belongs to a different database")
        return row

    def _check_revision(self, connection, session):
        floor = int(self.store.get("floor"))
        if session["revision"] < floor:
            raise TransferError(f"export revision {session['revision']} is older than retained floor {floor}")

    def _segment_lines(self, connection, session, row):
        rows = visible_range(connection, session["collection"], session["revision"], row["first_key"], row["last_key"])
        lines = [(item.key, _line(item)) for item in (record(entry) for entry in rows)]
        if len(lines) != row["documents"] or digest([line for _, line in lines]) != row["digest"]:
            raise TransferError("export segment no longer matches its manifest")
        return lines

    def _verify_complete(self, connection, session):
        combined = [line
                    for row in connection.execute("SELECT * FROM export_segments WHERE export_id=? ORDER BY idx",
                                                  (session["export_id"],))
                    for _, line in self._segment_lines(connection, session, row)]
        if len(combined) != session["total_documents"] or digest(combined) != session["digest"]:
            raise TransferError("export content no longer matches its manifest")

    def _describe(self, export_id, connection, include_segments=False):
        session = self._session(connection, export_id)
        progress, total = session["progress"], session["total_segments"]
        result = {"export": session["export_id"], "collection": session["collection"],
                  "revision": session["revision"], "pin": session["pin"],
                  "segment_size": session["segment_size"], "total_segments": total,
                  "total_documents": session["total_documents"], "digest": session["digest"],
                  "state": session["state"], "progress": progress,
                  "next_segment": progress if progress < total else None,
                  "complete": session["state"] == "complete"}
        if include_segments:
            result["segments"] = [{"index": row["idx"], "first_key": row["first_key"], "last_key": row["last_key"],
                                   "documents": row["documents"], "digest": row["digest"]}
                                  for row in connection.execute(
                                      "SELECT * FROM export_segments WHERE export_id=? ORDER BY idx", (export_id,))]
        return result

