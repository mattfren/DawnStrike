"""Tiny liveness endpoint for the static Dawnstrike publication."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from api.public_state import PUBLIC_STATE

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _public_root() -> Path:
    candidates = (
        REPOSITORY_ROOT / "api" / "public",
        REPOSITORY_ROOT / "public",
        REPOSITORY_ROOT / "build" / "public",
    )
    return next((candidate for candidate in candidates if candidate.is_dir()), candidates[0])


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        metadata = _build_metadata()
        body = json.dumps(
            {
                "status": "alive",
                "service": "dawnstrike-public",
                "surface": "static-research-publication",
                "source_sha": metadata.get("source_sha"),
                "build_id": metadata.get("build_id"),
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


def _build_metadata() -> dict[str, object]:
    manifest_path = _public_root() / "build-manifest.json"
    if not manifest_path.is_file():
        return (
            PUBLIC_STATE.get("build_manifest", {})
            if isinstance(PUBLIC_STATE.get("build_manifest"), dict)
            else {}
        )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(payload, dict):
        return payload
    return (
        PUBLIC_STATE.get("build_manifest", {})
        if isinstance(PUBLIC_STATE.get("build_manifest"), dict)
        else {}
    )
