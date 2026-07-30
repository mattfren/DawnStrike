"""Readiness endpoint backed by the generated static stage manifest."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path

READINESS_PATH = Path(__file__).resolve().parents[1] / "build" / "public" / "readiness.json"


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not READINESS_PATH.is_file():
            _send(
                self, {"status": "not_ready", "http_status": 503, "reason": "snapshot_missing"}, 503
            )
            return
        try:
            payload = json.loads(READINESS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _send(
                self,
                {"status": "not_ready", "http_status": 503, "reason": "snapshot_unreadable"},
                503,
            )
            return
        status = (
            200 if payload.get("status") == "ready" and payload.get("http_status") == 200 else 503
        )
        _send(self, payload, status)


def _send(handler: BaseHTTPRequestHandler, payload: dict[str, object], status: int) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
