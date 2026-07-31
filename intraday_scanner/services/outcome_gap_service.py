"""Truthful outcome-gap reporting for AlphaOps evaluated candidates."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.dashboard.operator_data_service import (
    calculate_missing_outcome_status,
    canonical_missing_outcome_rows,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def outcome_gap_report(
    *,
    db_path: str | Path,
    market_date: str | None = None,
    out_path: str | Path | None = None,
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    store.initialize()
    summary = calculate_missing_outcome_status(db_path, market_date)
    selected_date = str(summary.get("market_date") or market_date or "")[:10]
    missing_rows = canonical_missing_outcome_rows(
        db_path,
        selected_date or None,
    )
    attempts = store.load_outcome_capture_attempts(
        market_date=selected_date or None,
        limit=100_000,
    )
    latest_attempts = _latest_attempts(attempts)
    gaps: list[dict[str, Any]] = []
    for row in missing_rows:
        signal_id = str(row.get("signal_id") or "")
        attempt = latest_attempts.get(signal_id)
        gaps.append(
            {
                **row,
                "capture_status": (
                    attempt.get("status") if attempt else "not_attempted"
                ),
                "terminal": (
                    bool(attempt.get("terminal")) if attempt else False
                ),
                "learning_eligible": False,
                "provider_chain": (
                    attempt.get("provider_chain")
                    or attempt.get("provider_chain_json")
                    if attempt
                    else []
                ),
                "attempted_at": (
                    attempt.get("attempted_at") if attempt else None
                ),
                "error_code": (
                    attempt.get("error_code") if attempt else None
                ),
                "error_detail": (
                    attempt.get("error_detail") if attempt else None
                ),
            }
        )
    terminal_missing = sum(
        1 for row in gaps if row.get("terminal") is True
    )
    status = (
        "NO_ELIGIBLE"
        if int(summary.get("signals_requiring_outcomes") or 0) == 0
        else "COMPLETE"
        if not gaps
        else "DEGRADED"
    )
    payload: dict[str, Any] = {
        "schema_version": "dawnstrike.outcome_gap.v1",
        "generated_at": _utc_now(),
        "market_date": selected_date or None,
        "status": status,
        "eligible_candidate_count": summary.get(
            "signals_requiring_outcomes",
            0,
        ),
        "complete_outcome_count": summary.get("audited_count", 0),
        "missing_outcome_count": len(gaps),
        "terminal_missing_count": terminal_missing,
        "retryable_missing_count": len(gaps) - terminal_missing,
        "learning_eligible_missing_count": 0,
        "missing_truth_is_zero": False,
        "gaps": gaps,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["payload_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    if out_path is not None:
        _atomic_write(Path(out_path), payload)
    return payload


def _latest_attempts(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        signal_id = str(row.get("signal_id") or "")
        prior = latest.get(signal_id)
        if prior is None or str(row.get("attempted_at") or "") >= str(
            prior.get("attempted_at") or ""
        ):
            latest[signal_id] = row
    return latest


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = ["outcome_gap_report"]
