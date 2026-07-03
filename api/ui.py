from __future__ import annotations

import mimetypes
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

UI_ROOT = Path(__file__).resolve().parents[1] / "data" / "v2_command_center_x3"


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        asset = _requested_asset(self.path)
        file_path = _resolve_asset(asset)
        if file_path is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        body = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if file_path.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif file_path.suffix in {".css", ".html", ".json", ".svg"}:
            charset = "utf-8" if file_path.suffix != ".svg" else "utf-8"
            content_type = f"{content_type}; charset={charset}"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        cache_control = (
            "no-store" if file_path.suffix == ".html" else "public, max-age=31536000, immutable"
        )
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)


def _requested_asset(path: str) -> str:
    query = parse_qs(urlparse(path).query)
    asset = query.get("asset", ["index.html"])[0]
    asset = unquote(asset).replace("\\", "/").lstrip("/")
    return asset or "index.html"


def _resolve_asset(asset: str) -> Path | None:
    candidate = (UI_ROOT / asset).resolve()
    try:
        candidate.relative_to(UI_ROOT.resolve())
    except ValueError:
        return None
    if candidate.is_dir():
        candidate = candidate / "index.html"
    if not candidate.is_file():
        return None
    return candidate
