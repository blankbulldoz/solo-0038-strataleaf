"""Reference HTTP service for a container.

This is not the library. It is the smallest possible host that demonstrates
the operational contract the drills verify:

  GET /healthz  liveness  -> 200 while the process can serve at all
  GET /readyz   readiness -> 200 only when not WAL-blocked; 503 otherwise
  POST /events  one conditional batch, returns the commit receipt

The split matters. A long read or a slow consumer can leave WAL unable to
checkpoint: writes still succeed, so `/healthz` stays 200. Restarting the
container in that state throws away the very reader that was making progress,
so `/readyz` is where the orchestrator should stop routing new work. Neither
endpoint inspects "file exists" or "pid alive"; both call the operator probe.

No web framework is available at runtime and none is needed: the server is a
single-threaded `http.server`, which also matches the library's one-writer
model (all writes go through one handle and one SQLite connection).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTAINER = HERE.parent / "container"
sys.path.insert(0, str(CONTAINER))
sys.path.insert(0, str(HERE.parents[1]))

import strataleaf_ops as ops  # noqa: E402


class Service:
    def __init__(self, config: dict):
        self.config = config
        self.paths = ops.config_paths(config)
        self.stopping = threading.Event()
        from strataleaf import Database
        self.database = Database(str(self.paths["db"]), timeout=config["STRATALEAF_BUSY_TIMEOUT"])
        self._bootstrap()

    def _bootstrap(self) -> None:
        if not self.database.catalog.list():
            try:
                self.database.catalog.create("events", {
                    "fields": {"worker": "string", "seq": "integer"},
                    "required": ["worker", "seq"]})
            except Exception as exc:
                if "already exists" not in str(exc):
                    raise

    def write(self, payload: dict) -> dict:
        if self.stopping.is_set():
            raise RuntimeError("service is draining")
        result = self.database.batch(payload.get("request_key")).put(
            "events", str(payload["key"]),
            {"worker": str(payload["worker"]), "seq": int(payload["seq"])}).commit()
        return {"revision": result.revision, "count": result.count,
                "replayed": result.replayed}

    def health(self, kind: str) -> tuple[int, dict]:
        try:
            report = ops.probe(self.config, kind=kind)
        except ops.GateError as exc:
            return 503, {"status": "unhealthy", "reason": str(exc)}
        # Liveness tolerates "degraded": the process can still commit, and
        # restarting it would discard the reader that is holding the WAL back.
        # Readiness does not: an orchestrator should stop routing new work to a
        # node whose checkpoint is not advancing, even though writes succeed.
        healthy = report["status"] == "healthy" if kind == "readiness" \
            else report["status"] in ("healthy", "degraded")
        return (200 if healthy else 503), {
            "status": report["status"], "kind": kind,
            "reason": report.get("reason"),
            "checkpoint": report.get("checkpoint"),
            "wal": report.get("wal")}

    def close(self) -> None:
        self.database.close()


def build_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (http.server API)
            if self.path == "/healthz":
                self._send(*service.health("liveness"))
            elif self.path == "/readyz":
                self._send(*service.health("readiness"))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/events":
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                self._send(201, service.write(payload))
            except Exception as exc:  # surface the real class name to callers
                self._send(409, {"error": type(exc).__name__, "message": str(exc)})

        def log_message(self, fmt, *args):  # keep stdout for structured logs
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    return Handler


def main() -> int:
    config = ops.load_config(os.environ.get("STRATALEAF_CONF", CONTAINER / "service.conf"))
    ops.preflight(config)
    service = Service(config)
    signal.signal(signal.SIGTERM, lambda *_: service.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: service.stopping.set())
    port = int(os.environ.get("STRATALEAF_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), build_handler(service))
    server.timeout = 0.2
    try:
        while not service.stopping.is_set():
            server.handle_request()
    finally:
        server.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
