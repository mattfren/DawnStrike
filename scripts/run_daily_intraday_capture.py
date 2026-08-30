"""Generate today's governed session contract and run delayed SIP capture."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.market_calendar import MARKET_TIMEZONE, market_session


def main() -> int:
    args = _parser().parse_args()
    market_date = (
        date.fromisoformat(args.market_date)
        if args.market_date
        else datetime.now(UTC).astimezone(MARKET_TIMEZONE).date()
    )
    session = build_expected_session(market_date)
    if session is None:
        print(
            json.dumps(
                {
                    "schema_version": "dawnstrike.daily_intraday_capture.v1",
                    "status": "SKIPPED_MARKET_CLOSED",
                    "market_date": market_date.isoformat(),
                    "provider_network_performed": False,
                    "research_only": True,
                    "broker_execution": "disabled",
                },
                sort_keys=True,
            )
        )
        return 0

    args.session_root.mkdir(parents=True, exist_ok=True)
    session_path = args.session_root / f"expected-session-{market_date.isoformat()}.json"
    _write_once_json(session_path, session)
    command = [
        sys.executable,
        str(Path(__file__).with_name("capture_intraday_operations.py")),
        "--mode",
        "forward_observed",
        "--provider",
        "alpaca",
        "--feed",
        "sip",
        "--candidate-sha",
        args.candidate_sha,
        "--repo-root",
        str(args.repo_root),
        "--db-path",
        str(args.db_path),
        "--evidence-root",
        str(args.evidence_root),
        "--run-root",
        str(args.run_root),
        "--output-root",
        str(args.output_root),
        "--symbols-manifest",
        str(args.symbols_manifest),
        "--symbols-manifest-sha256",
        args.symbols_manifest_sha256,
        "--expected-session",
        str(session_path),
        "--entitlement-receipt",
        str(args.entitlement_receipt),
        "--entitlement-receipt-sha256",
        args.entitlement_receipt_sha256,
        "--source-config",
        str(args.source_config),
        "--source-config-sha256",
        args.source_config_sha256,
        "--env-file",
        str(args.env_file),
        "--max-pages",
        str(args.max_pages),
        "--retries",
        str(args.retries),
        "--execute",
    ]
    result = subprocess.run(
        command,
        cwd=args.repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode:
        print(
            json.dumps(
                {
                    "schema_version": "dawnstrike.daily_intraday_capture.v1",
                    "status": "CAPTURE_FAILED",
                    "market_date": market_date.isoformat(),
                    "exit_code": result.returncode,
                    "research_only": True,
                    "broker_execution": "disabled",
                },
                sort_keys=True,
            )
        )
    return result.returncode


def build_expected_session(market_date: date) -> dict[str, Any] | None:
    """Build one exact regular-session window from the checked-in calendar."""

    decision = market_session(market_date)
    if not decision.is_trading_day:
        return None
    if decision.open_time_et is None or decision.close_time_et is None:
        raise ValueError("open session is missing governed open or close time")
    start = datetime.combine(
        market_date, time.fromisoformat(decision.open_time_et), tzinfo=MARKET_TIMEZONE
    ).astimezone(UTC)
    end = datetime.combine(
        market_date, time.fromisoformat(decision.close_time_et), tzinfo=MARKET_TIMEZONE
    ).astimezone(UTC)
    return {
        "schema_version": "dawnstrike.expected_capture_session.v1",
        "exchange": "XNYS",
        "market_date": market_date.isoformat(),
        "exchange_session_id": f"XNYS:{market_date.isoformat()}:regular",
        "calendar_id": decision.calendar_id,
        "calendar_status": decision.status.value,
        "calendar_reason": decision.reason,
        "calendar_published_as_of": decision.calendar_published_as_of,
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        # Retain the full calendar session above for denominator identity,
        # while bounding delayed SIP microstructure capture to the first
        # regular-open window (or the whole session on an early close).
        "capture_start_utc": start.isoformat(),
        "capture_end_utc": min(start + timedelta(minutes=30), end).isoformat(),
        "research_only": True,
        "broker_execution": "disabled",
    }


def _write_once_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
        return
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
            raise RuntimeError(
                "expected-session identity conflicts with retained evidence"
            ) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--session-root", type=Path, required=True)
    parser.add_argument("--symbols-manifest", type=Path, required=True)
    parser.add_argument("--symbols-manifest-sha256", required=True)
    parser.add_argument("--entitlement-receipt", type=Path, required=True)
    parser.add_argument("--entitlement-receipt-sha256", required=True)
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--source-config-sha256", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--market-date", default=None)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--retries", type=int, default=3)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
