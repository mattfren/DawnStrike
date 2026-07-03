from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from vercel_dawnstrike.runtime import market_date, require_cron, run_scanner, send_json


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        ok, auth = require_cron(self)
        if not ok:
            send_json(self, auth, status=401)
            return
        payload = run_scanner(
            "morning-check",
            run_date=market_date(),
            options_payload={
                "autodata": True,
                "learn": True,
                "market_masters": True,
                "telegram": True,
                "use_real_intraday": False,
            },
        )
        send_json(self, payload)
