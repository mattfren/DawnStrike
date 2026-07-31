"""Record one scheduled process result in Dawnstrike's shared daily DAG."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.services.daily_run_service import record_daily_stage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--release-sha", default=None)
    parser.add_argument("--exit-code", type=int, default=None)
    parser.add_argument("--started-at", default=None)
    parser.add_argument("--completed-at", default=None)
    parser.add_argument("--input-file", default=None)
    parser.add_argument("--output-file", default=None)
    parser.add_argument("--source-data-watermark", default=None)
    parser.add_argument("--error-code", default=None)
    parser.add_argument("--error-detail", default=None)
    parser.add_argument("--result-file", default=None)
    parser.add_argument("--not-required", action="store_true")
    args = parser.parse_args()
    payload = _load_payload(args.result_file)
    result = record_daily_stage(
        db_path=args.db_path,
        market_date=args.market_date,
        stage_name=args.stage,
        status=args.status,
        runtime_root=args.runtime_root,
        state_root=args.state_root,
        release_sha=args.release_sha,
        required=not args.not_required,
        started_at=args.started_at,
        completed_at=args.completed_at,
        exit_code=args.exit_code,
        input_hash_sha256=_file_hash(args.input_file),
        output_hash_sha256=_file_hash(args.output_file),
        source_data_watermark=(
            args.source_data_watermark
            or str(payload.get("source_data_watermark") or "")
            or None
        ),
        error_code=args.error_code,
        error_detail=args.error_detail,
        payload=payload,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def _load_payload(path: str | None) -> dict[str, object]:
    if not path:
        return {}
    source = Path(path)
    if not source.is_file():
        return {"result_file": str(source), "result_file_status": "missing"}
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "result_file": str(source),
            "result_file_status": "unreadable",
            "result_file_error": str(exc),
        }
    return payload if isinstance(payload, dict) else {"result": payload}


def _file_hash(path: str | None) -> str | None:
    if not path:
        return None
    source = Path(path)
    if not source.is_file():
        return None
    return hashlib.sha256(source.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
