"""Single-host disaster-recovery drills for StrataLeaf deployments.

The package never writes to the embedded database library itself.  It only
orchestrates the public StrataLeaf API (archives, replication, diagnostics,
retention and feeds) and records independently verifiable evidence.
"""

TOOL_NAME = "strataleaf-dr"
TOOL_VERSION = "1.0"
