#!/bin/sh
# Reproduce the container drills from a clean checkout.
#
#   deploy/smoke/run-drills.sh [mode] [workspace]
#
# Default workspace is a fresh temporary directory; evidence is printed at the
# end. Set STRATALEAF_ALLOW_FUSE=1 on Docker Desktop / OrbStack when the
# volumes live on a local virtiofs bind mount. Set
# STRATALEAF_DRILL_ALLOW_PSEUDO=1 only when there is genuinely no persistent
# local volume available (CI sandboxes); the report records that fact.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
mode=${1:-all}
workspace=${2:-$(mktemp -d /tmp/strataleaf-drill.XXXXXX)}

cd "$root"
python3 -m compileall -q strataleaf examples deploy
exec "${STRATALEAF_PYTHON:-python3}" deploy/container/strataleaf_ops.py \
    --config deploy/container/service.conf \
    drill --workspace "$workspace" --mode "$mode" --json
