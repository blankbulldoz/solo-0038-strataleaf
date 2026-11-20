from .codec import MISSING, field, loads, name, scalar_token
from .errors import Conflict, InvalidDocument, NotFound
from .mvcc import visible_rows


def projection_entries(document, definitions):
    """Pure projection math: the (index name, token) pairs a document
    contributes for the given (name, field) definitions, in definition order."""
    if document is None:
        return ()
    entries = []
    for index_name, path in definitions:
        token = scalar_token(field(document, path))
        if token is not None:
            entries.append((index_name, token))
    return tuple(entries)


def replace_projection(connection, collection, key, entries):
    """Persistence only: replace a key's projection rows with the given
    entries. Acceptability is ruled elsewhere; this writer decides nothing."""
    connection.execute("DELETE FROM index_values WHERE collection=? AND key=?", (collection, key))
    for index_name, token in entries:
        connection.execute("INSERT INTO index_values VALUES (?,?,?,?)", (collection, index_name, token, key))


class Indexes:
    def __init__(self, store):
        self.store = store

    def create(self, collection, index_name, path, unique=False):
        name(index_name)
        field({}, path)
        if not isinstance(unique, bool):
            raise InvalidDocument("unique must be boolean")
        with self.store.atomic(writable=True) as connection:
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection")
            if connection.execute("SELECT 1 FROM index_defs WHERE collection=? AND name=?", (collection, index_name)).fetchone():
                raise Conflict("index already exists")
            connection.execute("INSERT INTO index_defs VALUES (?,?,?,?)", (collection, index_name, path, int(unique)))
            seen = set()
            for row in visible_rows(connection, collection, int(self.store.get("head"))):
                token = scalar_token(field(loads(row["body"]), path))
                if token is None:
                    continue
                if unique and token in seen:
                    raise Conflict("existing documents violate unique index")
                seen.add(token)
                connection.execute("INSERT INTO index_values VALUES (?,?,?,?)", (collection, index_name, token, row["key"]))
            self.store.set("catalog_epoch", int(self.store.get("catalog_epoch")) + 1)
        return {"name": index_name, "field": path, "unique": unique}

    def drop(self, collection, index_name):
        with self.store.atomic(writable=True) as connection:
            changed = connection.execute("DELETE FROM index_defs WHERE collection=? AND name=?", (collection, index_name)).rowcount
            if not changed:
                raise NotFound("unknown index")
            self.store.set("catalog_epoch", int(self.store.get("catalog_epoch")) + 1)

    def list(self, collection):
        with self.store.read() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM index_defs WHERE collection=? ORDER BY name", (collection,))]


def equality_candidates(connection, collection, path, value):
    token = scalar_token(value)
    if token is None:
        return None
    definition = connection.execute("SELECT name FROM index_defs WHERE collection=? AND field=? ORDER BY name LIMIT 1",
                                    (collection, path)).fetchone()
    if definition is None:
        return None
    keys = {row[0] for row in connection.execute("SELECT key FROM index_values WHERE collection=? AND name=? AND token=?",
                                               (collection, definition[0], token))}
    return definition[0], keys
