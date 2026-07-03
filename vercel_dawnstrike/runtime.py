"""Small Vercel API runtime around the real Dawnstrike v2 Python modules."""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback only
    ZoneInfo = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_BASE = (
    Path(os.getenv("DAWNSTRIKE_RUNTIME_BASE", tempfile.gettempdir())) / "dawnstrike_vercel"
)
SECRET_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET_KEY",
    "ALPHA_VANTAGE_API_KEY",
    "TWELVE_DATA_API_KEY",
    "DAWNSTRIKE_ADMIN_TOKEN",
    "CRON_SECRET",
)
PUBLIC_ENV_KEYS = (
    *SECRET_ENV_KEYS,
    "TELEGRAM_ENABLED",
    "TELEGRAM_DRY_RUN",
    "TELEGRAM_PARSE_MODE",
    "TELEGRAM_MAX_MESSAGE_CHARS",
)
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization:\s*bearer|password|secret|token)\s*[:=]\s*['\"]?[^'\"\s,;]+"
)
SEED_DIRECTORIES = (
    "data/v2_omega_sentinel",
    "data/v2_telegram_intel",
    "data/v2_autodata",
    "data/v2_paper_ops",
    "data/v2_fill_truth",
    "data/v2_evidence_commit",
    "data/v2_learning_foundry",
    "data/v2_market_masters",
    "data/v2_scheduler",
    "data/v2_autonomous_runner",
    "data/v2_command_center",
    "data/v2_omega",
    "data/v2_data_truth",
)
SEED_FILES = (
    "data/v2_forward_evidence/frozen_picks/2026-06-29_picks.json",
    "data/v2_forward_evidence/frozen_picks/2026-06-29_picks_superseding_dab2bc51d950.json",
    "data/v2_forward_evidence/reports/riskhub_daily.json",
    "data/v2_forward_evidence/reports/daily/2026-06-29.json",
    "data/v2_forward_evidence/reconciliation/evidence_integrity.json",
    "data/v2_forward_evidence/strategy_evidence/strategy_evidence_omega.json",
    "data/v2_forward_evidence/calendar/strategy_daily_returns.csv",
    "docs/audit/omega_build_state.json",
)


def send_json(handler: BaseHTTPRequestHandler, payload: Any, *, status: int = 200) -> None:
    body = json.dumps(redact(payload), indent=2, sort_keys=True, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Authorization,Content-Type,X-Dawnstrike-Admin-Token",
    )
    handler.end_headers()
    handler.wfile.write(body)


def options(handler: BaseHTTPRequestHandler) -> None:
    send_json(handler, {"status": "ok"}, status=204)


def query_params(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    parsed = urlparse(handler.path)
    return {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}


def json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw_len = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_len or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def require_admin(handler: BaseHTTPRequestHandler) -> tuple[bool, dict[str, object]]:
    configured = os.getenv("DAWNSTRIKE_ADMIN_TOKEN", "")
    supplied = handler.headers.get("X-Dawnstrike-Admin-Token", "")
    if not configured:
        return False, {"status": "blocked_admin_token_missing"}
    if not supplied or not hmac.compare_digest(supplied, configured):
        return False, {"status": "blocked_admin_auth_failed"}
    return True, {"status": "authorized"}


def require_cron(handler: BaseHTTPRequestHandler) -> tuple[bool, dict[str, object]]:
    configured = os.getenv("CRON_SECRET", "")
    auth = handler.headers.get("Authorization", "")
    supplied = auth.removeprefix("Bearer").strip()
    if not configured:
        return False, {"status": "blocked_cron_secret_missing"}
    if not supplied or not hmac.compare_digest(supplied, configured):
        return False, {"status": "blocked_cron_auth_failed"}
    return True, {"status": "authorized"}


def health_payload() -> dict[str, object]:
    vercel_static_runtime = os.getenv("VERCEL", "") == "1"
    static_files = {
        "index": (REPO_ROOT / "data/v2_command_center_x3/index.html").exists(),
        "home": (REPO_ROOT / "data/v2_command_center_x3/pages/home.html").exists(),
        "system": (REPO_ROOT / "data/v2_command_center_x3/pages/system.html").exists(),
        "assets": (REPO_ROOT / "data/v2_command_center_x3/assets/x3.js").exists()
        and (REPO_ROOT / "data/v2_command_center_x3/assets/x3.css").exists(),
    }
    return {
        "app": "dawnstrike-command-center-x3",
        "backend": "vercel-python-functions",
        "checked_at": _now(),
        "env": env_snapshot(),
        "live_trading_enabled": False,
        "research_only": True,
        "routes": [
            "/api/health",
            "/api/readiness",
            "/api/scanner",
            "/api/telegram",
            "/api/cron_morning",
            "/api/cron_after_close",
        ],
        "static_ui": static_files,
        "static_ui_served_by_rewrite": vercel_static_runtime,
        "status": "ok"
        if all(static_files.values()) or vercel_static_runtime
        else "warning_static_ui_incomplete",
    }


def readiness_payload() -> dict[str, object]:
    with runtime_workspace() as runtime:
        from intraday_scanner.v2.autodata import readiness as autodata_readiness
        from intraday_scanner.v2.omega_sentinel import doctor as sentinel_doctor
        from intraday_scanner.v2.omega_sentinel import morning_check
        from intraday_scanner.v2.telegram_intel import readiness as telegram_readiness

        telegram = telegram_readiness(output_root=Path("data/v2_telegram_intel"))
        autodata = autodata_readiness(output_root=Path("data/v2_autodata"))
        scanner_smoke = morning_check(
            run_date=market_date(),
            output_root=Path("data/v2_omega_sentinel"),
            use_real_intraday=False,
            autodata=False,
            learn=False,
            telegram=False,
            market_masters=False,
        )
        doctor = sentinel_doctor(output_root=Path("data/v2_omega_sentinel"))
        scanner_status = str(scanner_smoke.get("status", "missing"))
        return {
            "checked_at": _now(),
            "env": env_snapshot(),
            "runtime": runtime_summary(runtime),
            "telegram": compact(telegram),
            "autodata": compact(autodata),
            "scanner": compact(scanner_smoke),
            "sentinel": {
                "mode": "ephemeral_smoke_morning_check_no_external_send",
                "status": scanner_status,
                "telegram_send_status": scanner_smoke.get("telegram_send_status", "not_requested"),
            },
            "doctor": compact(doctor),
            "live_trading_enabled": False,
            "research_only": True,
            "status": _combined_status(
                str(telegram.get("status", "")),
                str(autodata.get("status", "")),
                scanner_status,
            ),
        }


def run_telegram(action: str, *, kind: str, run_date: date) -> dict[str, object]:
    with runtime_workspace() as runtime:
        from intraday_scanner.v2.telegram_intel import draft, readiness, report, send, test_send

        if action == "readiness":
            payload = readiness(output_root=Path("data/v2_telegram_intel"))
        elif action == "draft":
            payload = draft(
                kind=kind,
                run_date=run_date,
                output_root=Path("data/v2_telegram_intel"),
            )
        elif action == "send":
            payload = send(kind=kind, run_date=run_date, output_root=Path("data/v2_telegram_intel"))
        elif action == "test-send":
            payload = test_send(output_root=Path("data/v2_telegram_intel"))
        elif action == "report":
            payload = report(output_root=Path("data/v2_telegram_intel"))
        else:
            return {"status": "unsupported_action", "action": action}
        return {
            "action": action,
            "kind": kind,
            "live_trading_enabled": False,
            "payload": compact(payload),
            "research_only": True,
            "runtime": runtime_summary(runtime),
            "status": payload.get("send_status", payload.get("status", "passed")),
        }


def run_scanner(
    action: str,
    *,
    run_date: date,
    options_payload: dict[str, Any],
) -> dict[str, object]:
    with runtime_workspace() as runtime:
        from intraday_scanner.v2.omega_sentinel import (
            after_close,
            morning_check,
            run,
            status,
            verify,
        )
        from intraday_scanner.v2.omega_sentinel import (
            doctor as sentinel_doctor,
        )

        if action == "status":
            payload: dict[str, object] = status(output_root=Path("data/v2_omega_sentinel"))
        elif action == "doctor":
            payload = sentinel_doctor(output_root=Path("data/v2_omega_sentinel"))
        elif action == "verify":
            payload = verify(output_root=Path("data/v2_omega_sentinel"))
        elif action == "run":
            result = run(
                run_date=run_date,
                output_root=Path("data/v2_omega_sentinel"),
                allow_fetch=_bool(options_payload.get("fetch"), False),
            )
            payload = result.to_dict()
        elif action == "after-close":
            payload = after_close(
                run_date=run_date,
                output_root=Path("data/v2_omega_sentinel"),
                use_real_intraday=_bool(options_payload.get("use_real_intraday"), False),
                autodata=_bool(options_payload.get("autodata"), True),
                learn=_bool(options_payload.get("learn"), True),
                telegram=_bool(options_payload.get("telegram"), True),
                market_masters=_bool(options_payload.get("market_masters"), True),
            )
        elif action == "morning-check":
            payload = morning_check(
                run_date=run_date,
                output_root=Path("data/v2_omega_sentinel"),
                use_real_intraday=_bool(options_payload.get("use_real_intraday"), False),
                autodata=_bool(options_payload.get("autodata"), True),
                learn=_bool(options_payload.get("learn"), True),
                telegram=_bool(options_payload.get("telegram"), True),
                market_masters=_bool(options_payload.get("market_masters"), True),
            )
        else:
            return {"status": "unsupported_action", "action": action}
        return {
            "action": action,
            "live_trading_enabled": False,
            "payload": compact(payload),
            "research_only": True,
            "runtime": runtime_summary(runtime),
            "status": payload.get("status", "passed"),
        }


def market_date(value: str | None = None) -> date:
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("America/Chicago")).date()
    return date.today()


@contextmanager
def runtime_workspace() -> Iterator[Path]:
    RUNTIME_BASE.mkdir(parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="run_", dir=str(RUNTIME_BASE)))
    _seed_runtime(runtime)
    original_cwd = Path.cwd()
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        os.chdir(runtime)
        yield runtime
    finally:
        os.chdir(original_cwd)


def runtime_summary(runtime: Path) -> dict[str, object]:
    return {
        "artifact_root": "ephemeral_tmp",
        "path_present": runtime.exists(),
        "seeded": (runtime / "data").exists(),
    }


def env_snapshot() -> dict[str, object]:
    telegram_enabled = _bool_env("TELEGRAM_ENABLED", False)
    telegram_dry_run = _bool_env("TELEGRAM_DRY_RUN", True)
    return {
        "present": {key: bool(os.getenv(key, "")) for key in PUBLIC_ENV_KEYS},
        "telegram_enabled": telegram_enabled,
        "telegram_dry_run": telegram_dry_run,
        "telegram_ready_for_external_send": bool(
            os.getenv("TELEGRAM_BOT_TOKEN", "")
            and os.getenv("TELEGRAM_CHAT_ID", "")
            and telegram_enabled
            and not telegram_dry_run
        ),
    }


def compact(value: Any, *, depth: int = 0) -> Any:
    if depth >= 7:
        return "<truncated-depth>"
    if isinstance(value, dict):
        return {str(key): compact(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        items = [compact(item, depth=depth + 1) for item in value[:40]]
        if len(value) > 40:
            items.append(f"<truncated {len(value) - 40} item(s)>")
        return items
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + f"... <truncated {len(value) - 4000} chars>"
    return value


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = value
        for key in SECRET_ENV_KEYS:
            secret = os.getenv(key, "")
            if secret:
                text = text.replace(secret, "<redacted>")
        return SECRET_PATTERN.sub(r"\1=<redacted>", text)
    return value


def _seed_runtime(runtime: Path) -> None:
    for rel in SEED_DIRECTORIES:
        source = REPO_ROOT / rel
        target = runtime / rel
        if source.exists() and source.is_dir():
            shutil.copytree(
                source,
                target,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
            )
    for rel in SEED_FILES:
        source = REPO_ROOT / rel
        target = runtime / rel
        if source.exists() and source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _combined_status(*statuses: str) -> str:
    lowered = {status.lower() for status in statuses if status}
    if any(status.startswith("failed") or status.startswith("blocked") for status in lowered):
        return "warning"
    warning_statuses = {"dry_run_or_disabled", "passed_with_warnings", "warning"}
    if any(status in warning_statuses for status in lowered):
        return "warning"
    return "passed"


def _bool_env(key: str, default: bool) -> bool:
    return _bool(os.getenv(key), default)


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
