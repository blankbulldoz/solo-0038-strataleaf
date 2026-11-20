from .codec import digest, loads
from .errors import ReplicationError
from .indexes import refresh_document
from .mvcc import checked_revision

#: Replication package protocol revisions.
#: 1 - legacy build: ``collections[].schema`` is the raw stored representation
#:     (JSON text for format-1 databases).
#: 2 - current build: ``collections[].schema`` is a decoded logical schema
#:     object, so a package means the same thing whichever payload format the
#:     source or target database uses.
PACKAGE_FORMAT = 2
ACCEPTED_PACKAGE_FORMATS = {1, 2}


class Replication:
    def __init__(self, store):
        self.store = store

    def export(self, since, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ReplicationError("replication limit must be between 1 and 1000 commits")
        with self.store.read() as connection:
            since = checked_revision(self.store, since)
            commits = [loads(row[0]) for row in connection.execute("SELECT payload FROM commits WHERE rev>? ORDER BY rev LIMIT ?", (since, limit))]
            body = {"format": PACKAGE_FORMAT, "source": self.store.get("database_id"), "since": since,
                "through": commits[-1]["revision"] if commits else since,
                "catalog_epoch": int(self.store.get("catalog_epoch")), "head": int(self.store.get("head")), "floor": int(self.store.get("floor")),
                "collections": [{"name": row["name"], "schema": loads(row["schema"]),
                                 "generation": row["generation"]}
                                for row in connection.execute("SELECT * FROM collections ORDER BY name")],
                "indexes": [dict(row) for row in connection.execute("SELECT * FROM index_defs ORDER BY collection,name")],
                "commits": commits}
            return {"body": body, "sha256": digest(body)}

    def apply(self, envelope):
        try:
            body = envelope["body"]
            if envelope["sha256"] != digest(body):
                raise ReplicationError("replication envelope checksum mismatch")
            package_format = body["format"]
            if package_format not in ACCEPTED_PACKAGE_FORMATS:
                raise ReplicationError("unsupported replication package format: " + str(package_format))
            commits = body["commits"]
            expected = list(range(body["since"] + 1, body["through"] + 1))
            if [item["revision"] for item in commits] != expected or body["through"] > body["head"]:
                raise ReplicationError("replication envelope has a revision gap")
            with self.store.atomic() as connection:
                if self.store.get("mode") != "replica" or self.store.get("upstream") != body["source"]:
                    raise ReplicationError("envelope does not match configured upstream")
                head = int(self.store.get("head"))
                if body["since"] > head:
                    raise ReplicationError("missing preceding replication range")
                if body["through"] < head or (body["through"] == head and body["catalog_epoch"] <= int(self.store.get("catalog_epoch"))):
                    return {"revision": head, "applied": 0}
                for definition in body["collections"]:
                    schema = definition["schema"]
                    if package_format == 1 and not isinstance(schema, dict):
                        schema = loads(schema)   # legacy packages carried the stored representation
                    connection.execute("INSERT INTO collections VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET schema=excluded.schema,generation=excluded.generation",
                                       (definition["name"], self.store.encode(schema), definition["generation"]))
                connection.execute("DELETE FROM index_values")
                connection.execute("DELETE FROM index_defs")
                for definition in body["indexes"] if body["through"] == body["head"] else []:
                    connection.execute("INSERT INTO index_defs VALUES (?,?,?,?)", (definition["collection"], definition["name"], definition["field"], definition["unique_flag"]))
                applied = 0
                for commit in commits:
                    revision = commit["revision"]
                    if revision <= head:
                        continue
                    for operation in commit["operations"]:
                        document = operation["document"]
                        connection.execute("INSERT INTO versions VALUES (?,?,?,?,?)", (operation["collection"], operation["key"], revision,
                            self.store.encode(document) if document is not None else None, operation["expires_at"]))
                    connection.execute("INSERT INTO commits(rev,payload) VALUES (?,?)", (revision, self.store.encode(commit)))
                    applied += 1
                self.store.set("head", body["through"])
                self.store.set("catalog_epoch", body["catalog_epoch"] if body["through"] == body["head"] else -1)
                from .mvcc import visible_rows
                for collection in connection.execute("SELECT name FROM collections").fetchall():
                    for row in visible_rows(connection, collection[0], body["through"]):
                        refresh_document(connection, collection[0], row["key"], loads(row["body"]))
                return {"revision": body["through"], "applied": applied}
        except ReplicationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplicationError("malformed replication envelope") from exc
