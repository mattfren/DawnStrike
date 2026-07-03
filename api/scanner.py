from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from vercel_dawnstrike.runtime import (
    json_body,
    market_date,
    options,
    query_params,
    require_admin,
    run_scanner,
    send_json,
)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        options(self)

    def do_GET(self) -> None:
        params = query_params(self)
        action = params.get("action", "status")
        if action not in {"status", "doctor"}:
            send_json(self, {"status": "blocked_admin_required"}, status=401)
            return
        send_json(
            self,
            run_scanner(
                action,
                run_date=market_date(params.get("date")),
                options_payload={},
            ),
        )

    def do_POST(self) -> None:
        ok, auth = require_admin(self)
        if not ok:
            send_json(self, auth, status=401)
            return
        body = json_body(self)
        params = query_params(self)
        action = str(body.get("action") or params.get("action") or "morning-check")
        run_date = market_date(str(body.get("date") or params.get("date") or ""))
        options_payload = {**params, **body}
        send_json(self, run_scanner(action, run_date=run_date, options_payload=options_payload))
