"""Commit adjudication for write batches.

decide() rules on a whole batch from one consistent snapshot of facts
(CommitFacts): replay of an already recorded request, a CommitPlan ready for
persistence, or a rejection. Duplicate request keys, version preconditions,
collection schemas and unique-value occupation are all ruled from the same
fact chain, before any row is written; a rejected ruling writes nothing, which
is what makes the batch atomic. This module is pure: it imports no storage
machinery and performs no I/O, so the write rules can be reviewed on their own.
"""
from dataclasses import dataclass

from .codec import dumps
from .errors import Conflict, NotFound
from .indexes import projection_entries
from .schema import validate_document


@dataclass(frozen=True)
class Receipt:
    """A previously recorded commit receipt for a request key."""
    revision: int
    count: int
    request_hash: str


@dataclass(frozen=True)
class IndexDef:
    """An index definition as an adjudication fact."""
    name: str
    field: str
    unique: bool


@dataclass(frozen=True)
class CommitFacts:
    """Every fact a commit ruling may cite, read inside the commit boundary."""
    head: int
    receipt: Receipt | None
    schemas: dict     # collection -> normalized schema, or None when unknown
    revisions: dict   # (collection, key) -> current revision at head
    indexes: dict     # collection -> tuple[IndexDef, ...] in definition order
    occupancy: dict   # (collection, index, token) -> frozenset of holder keys


@dataclass(frozen=True)
class Replay:
    """Ruling: this request key already committed these exact operations."""
    revision: int
    count: int


@dataclass(frozen=True)
class VersionRow:
    collection: str
    key: str
    body: str | None
    expires_at: float | None


@dataclass(frozen=True)
class IndexProjection:
    collection: str
    key: str
    entries: tuple    # (index name, token) pairs replacing the key's projection


@dataclass(frozen=True)
class CommitPlan:
    """Ruling: the batch is acceptable; persistence applies this verbatim."""
    revision: int
    payload: str
    request_key: str | None
    request_hash: str
    count: int
    versions: tuple
    projections: tuple


class UniqueOccupation:
    """The unique-value occupation rule over a sequence of projection changes.

    A token stays occupied while at least one key holds it; replacing or
    deleting a key's projection releases its previous tokens. Claiming a token
    held by another key is a conflict. The same rule explains unique
    enforcement at commit time and index rebuilds on replica catch-up.
    """

    def __init__(self, occupancy=None):
        self.holders = {claim: set(keys) for claim, keys in (occupancy or {}).items()}
        self.claims_by_document = {}
        for claim, keys in self.holders.items():
            for key in keys:
                self.claims_by_document.setdefault((claim[0], key), set()).add(claim)

    def release(self, collection, key):
        for claim in self.claims_by_document.pop((collection, key), ()):
            holders = self.holders[claim]
            holders.remove(key)
            if not holders:
                del self.holders[claim]

    def claim(self, collection, key, index, token):
        claim = (collection, index, token)
        holders = self.holders.setdefault(claim, set())
        if holders and (len(holders) != 1 or key not in holders):
            raise Conflict("unique index conflict: " + index)
        holders.add(key)
        self.claims_by_document.setdefault((collection, key), set()).add(claim)


def decide(facts, operations, request_key, request_hash):
    """Rule on operations as one atomic unit against the given facts."""
    if request_key is not None and facts.receipt is not None:
        if facts.receipt.request_hash != request_hash:
            raise Conflict("request key was used with different operations")
        return Replay(facts.receipt.revision, facts.receipt.count)
    for operation in operations:
        schema = facts.schemas[operation["collection"]]
        if schema is None:
            raise NotFound("unknown collection: " + operation["collection"])
        current = facts.revisions[(operation["collection"], operation["key"])]
        expected = operation["expected"]
        if expected is not None and expected != current:
            raise Conflict(f"revision conflict for {operation['collection']}/{operation['key']}: "
                           f"expected {expected}, found {current}")
        if operation["document"] is not None:
            validate_document(operation["document"], schema)
    unique_names = {collection: frozenset(d.name for d in definitions if d.unique)
                    for collection, definitions in facts.indexes.items()}
    occupation = UniqueOccupation(facts.occupancy)
    versions = []
    projections = []
    for operation in operations:
        collection, key = operation["collection"], operation["key"]
        document, expires_at = operation["document"], operation["expires_at"]
        # The old write path bound the version row before checking index conflicts.
        if isinstance(expires_at, int) and not -(1 << 63) <= int.__index__(expires_at) < (1 << 63):
            raise OverflowError("Python int too large to convert to SQLite INTEGER")
        versions.append(VersionRow(collection, key,
                                   dumps(document) if document is not None else None,
                                   expires_at))
        definitions = facts.indexes[collection]
        entries = projection_entries(document, [(d.name, d.field) for d in definitions])
        occupation.release(collection, key)
        for name, token in entries:
            if name in unique_names[collection]:
                occupation.claim(collection, key, name, token)
        projections.append(IndexProjection(collection, key, entries))
    revision = facts.head + 1
    payload = dumps({"revision": revision, "operations": operations})
    return CommitPlan(revision, payload, request_key, request_hash, len(operations),
                      tuple(versions), tuple(projections))
