"""SQLite persistence for commit rulings.

load_facts() reads, inside the caller's IMMEDIATE boundary, every fact that
adjudication.decide() may cite. apply_plan() persists an accepted CommitPlan
verbatim. This layer makes no acceptability decisions: it raises no business
errors and never judges whether a batch of operations should be allowed.
"""
from .adjudication import CommitFacts, IndexDef, Receipt
from .codec import loads
from .errors import DatabaseError
from .indexes import projection_entries, replace_projection
from .mvcc import latest_row


def _head(connection):
    row = connection.execute("SELECT value FROM meta WHERE key='head'").fetchone()
    if row is None:
        raise DatabaseError("missing storage metadata: head")
    return int(row[0])


def load_facts(connection, operations, request_key):
    """Read the adjudication fact snapshot for a batch; writes nothing."""
    head = _head(connection)
    receipt = None
    if request_key is not None:
        row = connection.execute("SELECT rev,count,request_hash FROM receipts WHERE request_key=?",
                                 (request_key,)).fetchone()
        if row is not None:
            receipt = Receipt(row["rev"], row["count"], row["request_hash"])
    schemas, revisions, indexes = {}, {}, {}
    for operation in operations:
        collection, key = operation["collection"], operation["key"]
        if collection not in schemas:
            row = connection.execute("SELECT schema FROM collections WHERE name=?", (collection,)).fetchone()
            schemas[collection] = loads(row["schema"]) if row is not None else None
        if (collection, key) not in revisions:
            row = latest_row(connection, collection, key, head)
            revisions[(collection, key)] = row["rev"] if row is not None else 0
        if collection not in indexes:
            indexes[collection] = tuple(
                IndexDef(row["name"], row["field"], bool(row["unique_flag"]))
                for row in connection.execute(
                    "SELECT name,field,unique_flag FROM index_defs WHERE collection=?", (collection,)))
    occupancy = {}
    for operation in operations:
        if operation["document"] is None:
            continue
        collection = operation["collection"]
        definitions = indexes[collection]
        unique = {d.name for d in definitions if d.unique}
        if not unique:
            continue
        entries = projection_entries(operation["document"], [(d.name, d.field) for d in definitions])
        for name, token in entries:
            if name not in unique:
                continue
            claim = (collection, name, token)
            if claim not in occupancy:
                rows = connection.execute(
                    "SELECT key FROM index_values WHERE collection=? AND name=? AND token=?", claim).fetchall()
                occupancy[claim] = frozenset(row["key"] for row in rows)
    return CommitFacts(head, receipt, schemas, revisions, indexes, occupancy)


def apply_plan(connection, plan):
    """Persist an accepted commit plan verbatim; decides nothing."""
    for row in plan.versions:
        connection.execute("INSERT INTO versions VALUES (?,?,?,?,?)",
                           (row.collection, row.key, plan.revision, row.body, row.expires_at))
    for projection in plan.projections:
        replace_projection(connection, projection.collection, projection.key, projection.entries)
    connection.execute("INSERT INTO commits VALUES (?,?,?,?)",
                       (plan.revision, plan.payload, plan.request_key, plan.request_hash))
    if plan.request_key is not None:
        connection.execute("INSERT INTO receipts VALUES (?,?,?,?)",
                           (plan.request_key, plan.revision, plan.request_hash, plan.count))
    connection.execute("UPDATE meta SET value=? WHERE key='head'", (str(plan.revision),))
