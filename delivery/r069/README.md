# StrataLeaf Storage Handoff

This directory preserves the publishable part of the original model handoff.
The files alongside this README are unmodified copies, laid out as in the
original out-of-repository bundle. This is an archival commit, not a model
revision or a change to StrataLeaf production code.

SQLite databases, their WAL and SHM files, and raw observations are kept in
the private archive because they can contain cursor signing material. The
observation script that emits that material is also retained privately.
Paths, receipts, and checksums in the original manifests describe the full
bundle, including files intentionally absent here. This public subset is
not a complete recovery package.
