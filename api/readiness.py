from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from vercel_dawnstrike.runtime import options, readiness_payload, send_json


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        options(self)

    def do_GET(self) -> None:
        send_json(self, readiness_payload())
