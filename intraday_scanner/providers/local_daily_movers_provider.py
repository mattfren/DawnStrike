"""Local CSV provider for daily market movers."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.errors import MarketCalendarCoverageError
from intraday_scanner.market_calendar import MARKET_TIMEZONE, market_session
from intraday_scanner.providers.daily_movers_base import (
    DESCRIPTIVE_EOD_ROLE,
    REALIZED_EOD_KIND,
    VERIFIED_CORPORATE_ACTION_STATUSES,
    normalize_daily_mover_rows,
    read_daily_mover_csv,
    retain_file_artifact,
    sha256_file_ref,
)


class LocalDailyMoversProvider:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def collect(self, *, market_date: str, out_dir: str | Path) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "status": "missing",
                "source": "local_daily_movers",
                "path": str(self.path),
                "rows": [],
                "rejected_rows": [],
                "failure_reason": "local daily movers CSV not found",
            }
        try:
            artifact_ref, retained_path = retain_file_artifact(
                self.path,
                artifact_dir=Path(out_dir) / "source_artifacts",
            )
            raw_rows = read_daily_mover_csv(retained_path)
        except OSError as exc:
            return {
                "status": "failed",
                "source": "local_daily_movers",
                "path": str(self.path.resolve()),
                "rows": [],
                "rejected_rows": [],
                "failure_reason": f"could not retain local source artifact: {exc}",
            }
        artifact_path = str(retained_path)
        corporate_action_reasons = _retain_corporate_action_artifacts(
            raw_rows,
            input_directory=self.path.resolve().parent,
            artifact_directory=Path(out_dir) / "corporate_action_artifacts",
            mover_artifact_ref=artifact_ref,
        )
        system_received_at = _utc_now().isoformat()
        unverified_rows, rejected, rejection_counts = normalize_daily_mover_rows(
            raw_rows,
            market_date=market_date,
            source="local_daily_movers",
            source_url=artifact_path,
            source_confidence=90.0,
            data_quality="Operator CSV pending source-truth validation",
            ingestion_channel="local_operator_csv",
            source_artifact_ref=artifact_ref,
            source_artifact_path=artifact_path,
            system_received_at=system_received_at,
            trusted_row_truth_fields=True,
        )
        truth_gate = _local_eod_truth_gate(
            raw_rows,
            market_date=market_date,
            artifact_ref=artifact_ref,
            system_received_at=system_received_at,
            preexisting_reasons=corporate_action_reasons,
        )
        if rejected:
            truth_gate["eligible"] = False
            truth_gate["reasons"] = list(
                dict.fromkeys(
                    [*truth_gate["reasons"], "one_or_more_rows_failed_normalization"]
                )
            )
        if not truth_gate["eligible"]:
            return {
                "status": "ineligible_source_truth",
                "source": "local_daily_movers",
                "path": artifact_path,
                "source_artifact_ref": artifact_ref,
                "source_artifact_path": artifact_path,
                "rows": unverified_rows,
                "rejected_rows": rejected,
                "rejection_reason_counts": rejection_counts,
                "rows_extracted": len(raw_rows),
                "rows_normalized": len(unverified_rows),
                "rows_rejected": len(rejected),
                "failure_reason": "; ".join(truth_gate["reasons"]),
                "truth_gate": truth_gate,
            }

        rows, rejected, rejection_counts = normalize_daily_mover_rows(
            raw_rows,
            market_date=market_date,
            source="local_daily_movers",
            source_url=artifact_path,
            source_confidence=100.0,
            data_quality="Verified complete operator EOD mover label artifact",
            dataset_role=DESCRIPTIVE_EOD_ROLE,
            prospective_signal_eligible=False,
            source_snapshot_kind=REALIZED_EOD_KIND,
            ingestion_channel="local_operator_csv",
            source_artifact_ref=artifact_ref,
            source_artifact_path=artifact_path,
            system_received_at=system_received_at,
            source_coverage_complete=True,
            list_coverage_complete=True,
            expected_row_count=len(raw_rows),
            eod_label_eligible=True,
            trusted_row_truth_fields=True,
        )
        return {
            "status": "success" if rows else "no_valid_rows",
            "source": "local_daily_movers",
            "path": artifact_path,
            "source_artifact_ref": artifact_ref,
            "source_artifact_path": artifact_path,
            "rows": rows,
            "rejected_rows": rejected,
            "rejection_reason_counts": rejection_counts,
            "rows_extracted": len(raw_rows),
            "rows_normalized": len(rows),
            "rows_rejected": len(rejected),
            "truth_gate": truth_gate,
        }


def _local_eod_truth_gate(
    rows: list[dict[str, Any]],
    *,
    market_date: str,
    artifact_ref: str,
    system_received_at: str | None = None,
    preexisting_reasons: list[str] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = list(preexisting_reasons or ())
    row_receipts = {
        _text(row, "system_received_at")
        for row in rows
        if _text(row, "system_received_at")
    }
    if system_received_at is None:
        system_received_at = next(iter(row_receipts)) if len(row_receipts) == 1 else ""
    elif row_receipts and row_receipts != {system_received_at}:
        reasons.append("row_system_receipts_must_match_authoritative_receipt")
    try:
        requested_date = date.fromisoformat(market_date)
    except ValueError:
        return {
            "eligible": False,
            "market_date": market_date,
            "source_artifact_ref": artifact_ref,
            "reasons": ["invalid_requested_market_date"],
        }
    try:
        session = market_session(requested_date)
    except MarketCalendarCoverageError:
        session = None
        reasons.append("market_date_lacks_published_calendar_coverage")
    if session is not None and (
        not session.is_trading_day or session.close_time_et is None
    ):
        reasons.append("market_date_is_not_a_published_trading_session")
    if not rows:
        reasons.append("empty_local_mover_file")

    explicit_dates = [_text(row, "market_date", "date") for row in rows]
    if any(not value for value in explicit_dates):
        reasons.append("every_row_requires_explicit_market_date")
    if any(value and value != market_date for value in explicit_dates):
        reasons.append("row_market_date_mismatch")

    coverage_values = [
        _strict_bool(row.get("source_coverage_complete")) for row in rows
    ]
    if any(value is not True for value in coverage_values):
        reasons.append("source_coverage_complete_must_be_explicit_true")
    list_coverage_values = [
        _strict_bool(row.get("list_coverage_complete")) for row in rows
    ]
    if any(value is not True for value in list_coverage_values):
        reasons.append("list_coverage_complete_must_be_explicit_true")

    expected_counts = {_positive_int(row.get("expected_row_count")) for row in rows}
    if None in expected_counts or len(expected_counts) != 1:
        reasons.append("expected_row_count_must_be_consistent_and_positive")
    elif next(iter(expected_counts)) != len(rows):
        reasons.append("expected_row_count_does_not_match_file")

    ranks = [_positive_int(row.get("rank")) for row in rows]
    if any(rank is None for rank in ranks) or set(ranks) != set(
        range(1, len(rows) + 1)
    ):
        reasons.append("ranks_must_be_unique_contiguous_and_complete")
    symbols = [_text(row, "ticker", "symbol").upper() for row in rows]
    if any(not symbol for symbol in symbols) or len(set(symbols)) != len(rows):
        reasons.append("symbols_must_be_present_and_unique")

    statuses = [
        _text(row, "corporate_action_status").lower() for row in rows
    ]
    if any(status not in VERIFIED_CORPORATE_ACTION_STATUSES for status in statuses):
        reasons.append("corporate_action_status_must_be_verified_or_adjusted")
    if any(
        not _corporate_action_artifact_valid(
            row,
            artifact_ref,
            system_received_at,
        )
        for row in rows
    ):
        reasons.append("corporate_action_source_artifact_missing_or_hash_invalid")

    received_at = _aware_datetime(system_received_at)
    if received_at is None:
        reasons.append("system_received_at_must_be_timezone_aware")

    if session is not None and session.close_time_et is not None:
        close_at = datetime.combine(
            requested_date,
            time.fromisoformat(session.close_time_et),
            tzinfo=MARKET_TIMEZONE,
        )
        extracted_values = [_text(row, "extracted_at", "captured_at") for row in rows]
        parsed_extracted = [_aware_datetime(value) for value in extracted_values]
        if any(value is None for value in parsed_extracted):
            reasons.append("every_row_requires_timezone_aware_extracted_at")
        else:
            aware_values = [value for value in parsed_extracted if value is not None]
            if any(value.astimezone(MARKET_TIMEZONE) < close_at for value in aware_values):
                reasons.append("extracted_at_must_be_at_or_after_official_close")
            if any(
                value.astimezone(MARKET_TIMEZONE).date() != requested_date
                for value in aware_values
            ):
                reasons.append("extracted_at_must_be_on_requested_market_date")
            if received_at is not None and any(
                value > received_at for value in aware_values
            ):
                reasons.append("extracted_at_cannot_be_after_system_receipt")

    if not artifact_ref.startswith("sha256:") or len(artifact_ref) != 71:
        reasons.append("retained_source_artifact_requires_sha256_identity")
    return {
        "eligible": not reasons,
        "market_date": market_date,
        "source_artifact_ref": artifact_ref,
        "source_coverage_complete": not reasons,
        "system_received_at": system_received_at,
        "corporate_action_statuses": sorted(set(statuses)),
        "reasons": list(dict.fromkeys(reasons)),
    }


def _retain_corporate_action_artifacts(
    rows: list[dict[str, Any]],
    *,
    input_directory: Path,
    artifact_directory: Path,
    mover_artifact_ref: str,
) -> list[str]:
    reasons: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        declared_ref = _text(row, "corporate_action_source_ref")
        declared_path = _text(row, "corporate_action_source_path")
        if not declared_ref or not declared_path:
            reasons.append(
                f"row_{row_index}_requires_independent_corporate_action_source"
            )
            continue
        source_path = Path(declared_path)
        if not source_path.is_absolute():
            source_path = input_directory / source_path
        source_path = source_path.resolve()
        parts = declared_ref.split(":", 2)
        declared_digest_ref = ":".join(parts[:2]) if len(parts) >= 2 else ""
        try:
            source_valid = bool(
                len(parts) in {2, 3}
                and parts[0] == "sha256"
                and len(parts[1]) == 64
                and source_path.is_file()
                and sha256_file_ref(source_path) == declared_digest_ref
                and (
                    len(parts) != 3
                    or Path(parts[2]).resolve() == source_path
                )
            )
        except OSError:
            source_valid = False
        if not source_valid:
            reasons.append(f"row_{row_index}_corporate_action_source_hash_invalid")
            continue
        try:
            retained_ref, retained_path = retain_file_artifact(
                source_path,
                artifact_dir=artifact_directory,
            )
        except OSError:
            reasons.append(
                f"row_{row_index}_corporate_action_source_unreadable"
            )
            continue
        if retained_ref == mover_artifact_ref:
            reasons.append(
                f"row_{row_index}_corporate_action_source_must_be_independent"
            )
            continue
        row["corporate_action_source_ref"] = retained_ref
        row["corporate_action_source_path"] = str(retained_path)
    return reasons


def _corporate_action_artifact_valid(
    row: dict[str, Any],
    mover_artifact_ref: str,
    system_received_at: str,
) -> bool:
    reference = _text(row, "corporate_action_source_ref")
    path_text = _text(row, "corporate_action_source_path")
    if not reference or reference == mover_artifact_ref or not path_text:
        return False
    try:
        path = Path(path_text)
        if not path.is_file() or sha256_file_ref(path) != reference:
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    observed_at = (
        _aware_datetime(str(payload.get("observed_at") or ""))
        if isinstance(payload, dict)
        else None
    )
    received_at = _aware_datetime(system_received_at)
    return bool(
        isinstance(payload, dict)
        and payload.get("schema_version")
        == "v2.corporate_action_evidence.v1"
        and payload.get("market_date") == _text(row, "market_date", "date")
        and str(payload.get("symbol") or "").upper()
        == _text(row, "ticker", "symbol").upper()
        and str(payload.get("corporate_action_status") or "").lower()
        == _text(row, "corporate_action_status").lower()
        and bool(str(payload.get("source") or "").strip())
        and observed_at is not None
        and received_at is not None
        and _after_published_market_close(
            _text(row, "market_date", "date"),
            observed_at,
        )
        and observed_at <= received_at
        and payload.get("research_only") is True
        and payload.get("broker_execution_enabled") is False
    )


def _after_published_market_close(
    market_date: str,
    observed_at: datetime,
) -> bool:
    try:
        requested_date = date.fromisoformat(market_date)
        session = market_session(requested_date)
    except (ValueError, MarketCalendarCoverageError):
        return False
    if not session.is_trading_day or session.close_time_et is None:
        return False
    close_at = datetime.combine(
        requested_date,
        time.fromisoformat(session.close_time_et),
        tzinfo=MARKET_TIMEZONE,
    )
    observed_et = observed_at.astimezone(MARKET_TIMEZONE)
    return observed_et.date() == requested_date and observed_et >= close_at


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in {None, ""}:
            return str(value).strip()
    return ""


def _strict_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _aware_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed
