from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from vercel_dawnstrike.runtime import (
    json_body,
    market_date,
    options,
    query_params,
    require_admin,
    run_telegram,
    send_json,
)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        options(self)

    def do_GET(self) -> None:
        params = query_params(self)
        action = params.get("action", "readiness")
        if action != "readiness":
            send_json(self, {"status": "blocked_admin_required"}, status=401)
            return
        send_json(
            self,
            run_telegram(
                "readiness",
                kind=params.get("kind", "morning"),
                run_date=market_date(params.get("date")),
            ),
        )

    def do_POST(self) -> None:
        ok, auth = require_admin(self)
        if not ok:
            send_json(self, auth, status=401)
            return
        body = json_body(self)
        params = query_params(self)
        action = str(body.get("action") or params.get("action") or "draft")
        kind = str(body.get("kind") or params.get("kind") or "morning")
        run_date = market_date(str(body.get("date") or params.get("date") or ""))
        send_json(self, run_telegram(action, kind=kind, run_date=run_date))
