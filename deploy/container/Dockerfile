# syntax=docker/dockerfile:1.6
# Minimal image for a StrataLeaf-backed service on a constrained Linux host.
#
# Build from the repository root:
#   docker build -f deploy/container/Dockerfile -t strataleaf-service:0.1.0 .
#
# The image is deliberately unprivileged and immutable: application code is
# read-only at runtime, all mutable state lives on two mounted volumes, and
# there is no shell-based healthcheck that could be satisfied by process
# liveness alone.

FROM python:3.12-slim-bookworm AS runtime

# Fail the build if the base is ever retagged to a different digest.
ARG PYTHON_VERSION=3.12

# The uid/gid here MUST match STRATALEAF_UID/STRATALEAF_GID in service.conf.
# 9042 is arbitrary but stable; do not reuse a distro account id.
ARG APP_UID=9042
ARG APP_GID=9042

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    STRATALEAF_CONF=/opt/strataleaf/container/service.conf \
    STRATALEAF_PYTHON=/usr/local/bin/python3 \
    TMPDIR=/tmp

RUN groupadd --gid "$APP_GID" strataleaf \
 && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home \
            --home-dir /nonexistent --shell /usr/sbin/nologin strataleaf

WORKDIR /opt/strataleaf

# Library, operator tooling, and the pinned HTTP-free reference service.
COPY pyproject.toml /opt/strataleaf/
COPY strataleaf /opt/strataleaf/strataleaf
COPY deploy/container /opt/strataleaf/container
COPY deploy/service /opt/strataleaf/service
RUN python3 -m pip install --no-cache-dir --no-compile . \
 && python3 -m compileall -q /opt/strataleaf/strataleaf /opt/strataleaf/service

# Build a read-only application layer: the service account can execute but not
# modify code, so a compromised process cannot rewrite its own entrypoint.
RUN chown -R root:root /opt/strataleaf \
 && chmod -R a-w /opt/strataleaf \
 && chmod a+rx /opt/strataleaf/container/entrypoint.sh \
              /opt/strataleaf/container/healthcheck.sh

# The volume mount points exist in the image with the final ownership so a
# named volume inherits them. A bind mount still needs a one-time chown:
#   install -d -o 9042 -g 9042 -m 0750 /srv/strataleaf/data /srv/strataleaf/state
RUN install -d -o "$APP_UID" -g "$APP_GID" -m 0750 \
        /var/lib/strataleaf /var/run/strataleaf

USER 9042:9042

# Container runs the reference HTTP service; it serves /healthz, /readyz and
# POST /events. The operator tool remains shipped for preflight/verify/stop.
ENV STRATALEAF_PORT=8080 \
    STRATALEAF_COMMAND=serve

# Data volume (db + wal + shm) and state volume (pid/revision/clean markers).
VOLUME ["/var/lib/strataleaf", "/var/run/strataleaf"]

EXPOSE 8080

# Liveness only. Readiness is a separate endpoint the orchestrator should call
# (see deploy/container/healthcheck.sh and docs/container-runbook.md); using
# this as a readiness probe would restart pods that are merely WAL-blocked.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["/opt/strataleaf/container/healthcheck.sh"]

# No shell: signals from the runtime reach the Python supervisor directly.
ENTRYPOINT ["/opt/strataleaf/container/entrypoint.sh"]
