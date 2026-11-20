#!/bin/sh
# Probe hook for orchestrators (docker HEALTHCHECK / Kubernetes *liveness*).
#
# The probe is a separate short-lived process holding no long-lived state.
# Read strataleaf_ops.py `probe` for the full contract. Process liveness, file
# existence, an open handle, and quick_check are NOT sufficient on their own.
#
# This hook runs `--kind liveness`: restarting on a WAL-blocked checkpoint
# would discard the reader that holds it back. For readiness use the HTTP
# endpoint /readyz (deploy/service/server.py) or run:
#   strataleaf_ops.py probe --kind readiness
#
# Exit codes: 0 healthy, 1 unhealthy, 2 not-configured (treated as unhealthy).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
exec "${STRATALEAF_PYTHON:-python3}" "$here/strataleaf_ops.py" --config "${STRATALEAF_CONF:-$here/service.conf}" probe --kind liveness
