from .adjudication import UniqueOccupation
from .codec import digest, dumps, loads
from .errors import ReplicationError
from .indexes import projection_entries, replace_projection
from .mvcc import checked_revision, visible_rows


class Replication:
    def __init__(self, store):
        self.store = store

    def export(self, since, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ReplicationError("replication limit must be between 1 and 1000 commits")
        with self.store.read() as connection:
            since = checked_revision(self.store, since)
            commits = [loads(row[0]) for row in connection.execute("SELECT payload FROM commits WHERE rev>? ORDER BY rev LIMIT ?", (since, limit))]
            body = {"format": 1, "source": self.store.get("database_id"), "since": since,
                "through": commits[-1]["revision"] if commits else since,
                "catalog_epoch": int(self.store.get("catalog_epoch")), "head": int(self.store.get("head")), "floor": int(self.store.get("floor")),
                "collections": [dict(row) for row in connection.execute("SELECT * FROM collections ORDER BY name")],
                "indexes": [dict(row) for row in connection.execute("SELECT * FROM index_defs ORDER BY collection,name")],
                "commits": commits}
            return {"body": body, "sha256": digest(body)}

    def apply(self, envelope):
        try:
            body = envelope["body"]
            if envelope["sha256"] != digest(body) or body["format"] != 1:
                raise ReplicationError("replication envelope checksum/format mismatch")
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
                    connection.execute("INSERT INTO collections VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET schema=excluded.schema,generation=excluded.generation",
                                       (definition["name"], definition["schema"], definition["generation"]))
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
                            dumps(document) if document is not None else None, operation["expires_at"]))
                    connection.execute("INSERT INTO commits(rev,payload) VALUES (?,?)", (revision, dumps(commit)))
                    applied += 1
                self.store.set("head", body["through"])
                self.store.set("catalog_epoch", body["catalog_epoch"] if body["through"] == body["head"] else -1)
                occupation = UniqueOccupation()
                for collection in connection.execute("SELECT name FROM collections").fetchall():
                    definitions = connection.execute(
                        "SELECT name,field,unique_flag FROM index_defs WHERE collection=?", (collection[0],)).fetchall()
                    fields = [(row["name"], row["field"]) for row in definitions]
                    unique = {row["name"] for row in definitions if row["unique_flag"]}
                    for row in visible_rows(connection, collection[0], body["through"]):
                        entries = projection_entries(loads(row["body"]), fields)
                        for index_name, token in entries:
                            if index_name in unique:
                                occupation.claim(collection[0], row["key"], index_name, token)
                        replace_projection(connection, collection[0], row["key"], entries)
                return {"revision": body["through"], "applied": applied}
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplicationError("malformed replication envelope") from exc
