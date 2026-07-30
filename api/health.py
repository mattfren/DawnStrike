"""Tiny liveness endpoint for the static Dawnstrike publication."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps(
            {
                "status": "ok",
                "service": "dawnstrike-public",
                "surface": "static-research-publication",
                "research_only": True,
                "live_trading_enabled": False,
            },
            sort_keys=True,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
