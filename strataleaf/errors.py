class DatabaseError(Exception):
    """Base error for caller-visible database failures."""

class InvalidDocument(DatabaseError):
    pass

class AggregateError(DatabaseError):
    """Raised when requested aggregation results cannot be produced completely."""
    def __init__(self, message, errors=None, summary=None, revision=None):
        super().__init__(message)
        self.errors = list(errors or [])
        self.summary = summary
        self.revision = revision

class Conflict(DatabaseError):
    pass

class NotFound(DatabaseError):
    pass

class SnapshotTooOld(DatabaseError):
    pass

class InvalidCursor(DatabaseError):
    pass

class ReadOnly(DatabaseError):
    pass

class ReplicationError(DatabaseError):
    pass
