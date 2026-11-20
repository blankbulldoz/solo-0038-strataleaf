"""Differential state-semantics verification for the strataleaf database.

This package is a verification harness, not part of the production library.
It drives the real SQLite-backed ``strataleaf.Database`` and an independent
expected-state model (:mod:`verification.model`) through identical generated
operation sequences and compares every observable outcome. Failing sequences
are shrunk to a minimal explainable segment (:mod:`verification.shrink`).

Run from the repository root:

    python3 -m verification run --scenarios 60 --ops 220
    python3 -m verification replay repro.json
    python3 -m verification shrink repro.json
"""
