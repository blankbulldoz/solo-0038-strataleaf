#!/bin/sh
# Container entrypoint.
#
# Responsibilities, in order:
#   1. Fail closed if the process is not running as the configured uid/gid.
#   2. Fail closed if the data/state volumes are missing, world-writable,
#      on the wrong filesystem, or the wrong owner (strataleaf_ops preflight).
#   3. Fail closed if free space is below the configured floor.
#   4. Exec the service under the same process so signals reach it directly
#      (PID 1 in the container, or the reaper's child under docker-compose).
#
# It never runs `chown` implicitly: volume ownership is an explicit operator
# step because silently re-owning a volume can mask a mis-mounted volume.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
CONF=${STRATALEAF_CONF:-$here/service.conf}
PY=${STRATALEAF_PYTHON:-python3}
COMMAND=${STRATALEAF_COMMAND:-supervise}

fail() { echo "entrypoint: $*" >&2; exit 78; }

conf_uid=$(sed -n 's/^STRATALEAF_UID=//p' "$CONF")
conf_gid=$(sed -n 's/^STRATALEAF_GID=//p' "$CONF")
uid=$(id -u)
gid=$(id -g)
[ -n "$conf_uid" ] && [ -n "$conf_gid" ] || fail "cannot read uid/gid from $CONF"
[ "$uid" = "$conf_uid" ] || fail "running as uid $uid, expected $conf_uid"
[ "$gid" = "$conf_gid" ] || fail "running as gid $gid, expected $conf_gid"
[ "$uid" != 0 ] || fail "refusing to run as root"

# Preflight once here so a bad volume never reaches the application; the
# service performs its own preflight again on startup.
"$PY" "$here/strataleaf_ops.py" --config "$CONF" preflight >/dev/null \
    || fail "preflight failed"

case "$COMMAND" in
    serve)
        exec "$PY" "$here/../service/server.py" ;;
    supervise)
        exec "$PY" "$here/strataleaf_ops.py" --config "$CONF" supervise ;;
    *)
        exec "$PY" "$here/strataleaf_ops.py" --config "$CONF" "$COMMAND" ;;
esac
