from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from vercel_dawnstrike.runtime import health_payload, options, send_json


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        options(self)

    def do_GET(self) -> None:
        send_json(self, health_payload())
