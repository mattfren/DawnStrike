"""Daily market mover provider primitives."""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path
from typing import Any, Protocol

from intraday_scanner.models import utc_now_iso

DAILY_MOVER_COLUMNS = [
    "date",
    "ticker",
    "company",
    "rank",
    "price",
    "change_pct",
    "volume",
    "dollar_volume",
    "open",
    "high",
    "low",
    "close",
    "source",
    "source_url",
    "extracted_at",
    "system_received_at",
    "dataset_role",
    "prospective_signal_eligible",
    "source_snapshot_kind",
    "ingestion_channel",
    "source_artifact_ref",
    "source_artifact_path",
    "source_coverage_complete",
    "source_complete",
    "list_coverage_complete",
    "expected_row_count",
    "corporate_action_status",
    "corporate_action_source_ref",
    "corporate_action_source_path",
    "eod_label_eligible",
    "source_ref",
]

REJECTED_MOVER_COLUMNS = [
    "source",
    "row_index",
    "ticker",
    "reject_reason",
    "detail",
    "raw_json",
]

ALIASES = {
    "ticker": ["ticker", "symbol"],
    "company": ["company", "company name", "name", "security", "description"],
    "rank": ["rank", "#", "no"],
    "price": ["price", "last", "last price", "close", "last sale"],
    "change_pct": ["change_pct", "change %", "% change", "chg %", "change", "perf %"],
    "volume": ["volume", "vol"],
    "dollar_volume": ["dollar_volume", "dollar volume", "$ volume", "value"],
    "open": ["open"],
    "high": ["high"],
    "low": ["low"],
    "close": ["close"],
    "date": ["market_date", "market date", "date"],
    "extracted_at": ["extracted_at", "extracted at", "captured_at", "captured at"],
    "corporate_action_status": [
        "corporate_action_status",
        "corporate action status",
    ],
    "corporate_action_source_ref": [
        "corporate_action_source_ref",
        "corporate action source ref",
    ],
    "corporate_action_source_path": [
        "corporate_action_source_path",
        "corporate action source path",
    ],
}

DESCRIPTIVE_EOD_ROLE = "descriptive_eod_movers"
REALIZED_EOD_KIND = "realized_eod_gainers"
CURRENT_WEB_ROLE = "descriptive_current_session_gainers"
CURRENT_WEB_KIND = "current_session_public_gainers"
UNVERIFIED_ROLE = "descriptive_unverified_mover_rows"
UNVERIFIED_KIND = "operator_supplied_unverified_rows"
VERIFIED_CORPORATE_ACTION_STATUSES = frozenset(
    {"verified_clear", "verified_adjusted", "adjusted"}
)


class DailyMoversProvider(Protocol):
    def collect(self, *, market_date: str, out_dir: str | Path) -> dict[str, Any]:
        """Collect and normalize daily mover rows."""


def normalize_daily_mover_rows(
    rows: list[dict[str, Any]],
    *,
    market_date: str,
    source: str,
    source_url: str = "",
    source_confidence: float = 50.0,
    data_quality: str = "Unverified shadow data",
    extracted_at: str | None = None,
    system_received_at: str | None = None,
    dataset_role: str = UNVERIFIED_ROLE,
    prospective_signal_eligible: bool = False,
    source_snapshot_kind: str = UNVERIFIED_KIND,
    ingestion_channel: str = "unspecified",
    source_artifact_ref: str = "",
    source_artifact_path: str = "",
    source_coverage_complete: bool = False,
    list_coverage_complete: bool = False,
    expected_row_count: int | None = None,
    corporate_action_status: str = "unverified",
    corporate_action_source_ref: str = "",
    corporate_action_source_path: str = "",
    eod_label_eligible: bool = False,
    trusted_row_truth_fields: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    extracted = extracted_at or utc_now_iso()
    received = system_received_at or utc_now_iso()
    normalized: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    for index, raw in enumerate(rows, start=1):
        try:
            row = _normalize_one(
                raw,
                row_index=index,
                market_date=market_date,
                source=source,
                source_url=source_url,
                source_confidence=source_confidence,
                data_quality=data_quality,
                extracted_at=extracted,
                system_received_at=received,
                dataset_role=dataset_role,
                prospective_signal_eligible=prospective_signal_eligible,
                source_snapshot_kind=source_snapshot_kind,
                ingestion_channel=ingestion_channel,
                source_artifact_ref=source_artifact_ref,
                source_artifact_path=source_artifact_path,
                source_coverage_complete=source_coverage_complete,
                list_coverage_complete=list_coverage_complete,
                expected_row_count=expected_row_count,
                corporate_action_status=corporate_action_status,
                corporate_action_source_ref=corporate_action_source_ref,
                corporate_action_source_path=corporate_action_source_path,
                eod_label_eligible=eod_label_eligible,
                trusted_row_truth_fields=trusted_row_truth_fields,
            )
            normalized.append(row)
        except ValueError as exc:
            reason = str(exc).split(":", 1)[0]
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            rejected.append(
                {
                    "source": source,
                    "row_index": index,
                    "ticker": str(_alias(raw, "ticker") or "").upper(),
                    "reject_reason": reason,
                    "detail": str(exc),
                    "raw_json": repr(raw),
                }
            )
    return normalized, rejected, reason_counts


def read_daily_mover_csv(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_daily_mover_csv(path: str | Path, rows: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DAILY_MOVER_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_rejected_mover_csv(path: str | Path, rows: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REJECTED_MOVER_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sha256_file_ref(path: str | Path) -> str:
    """Return a content identity for a retained source artifact."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def retain_file_artifact(
    source_path: str | Path,
    *,
    artifact_dir: str | Path,
) -> tuple[str, Path]:
    """Copy a source file into a content-addressed, immutable evidence path."""

    source = Path(source_path)
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    artifact_ref = f"sha256:{digest}"
    suffix = source.suffix.lower() or ".bin"
    directory = Path(artifact_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}{suffix}"
    try:
        with target.open("xb") as handle:
            handle.write(data)
            handle.flush()
    except FileExistsError:
        pass
    if sha256_file_ref(target) != artifact_ref:
        raise OSError(f"content-addressed artifact hash mismatch: {target}")
    return artifact_ref, target.resolve()


def _normalize_one(
    raw: dict[str, Any],
    *,
    row_index: int,
    market_date: str,
    source: str,
    source_url: str,
    source_confidence: float,
    data_quality: str,
    extracted_at: str,
    system_received_at: str,
    dataset_role: str,
    prospective_signal_eligible: bool,
    source_snapshot_kind: str,
    ingestion_channel: str,
    source_artifact_ref: str,
    source_artifact_path: str,
    source_coverage_complete: bool,
    list_coverage_complete: bool,
    expected_row_count: int | None,
    corporate_action_status: str,
    corporate_action_source_ref: str,
    corporate_action_source_path: str,
    eod_label_eligible: bool,
    trusted_row_truth_fields: bool,
) -> dict[str, Any]:
    ticker = _clean_ticker(_alias(raw, "ticker"))
    if not ticker:
        raise ValueError("missing_ticker: ticker/symbol is required")
    price = _optional_number(_alias(raw, "price"))
    change_pct = _optional_number(_alias(raw, "change_pct"))
    volume = _optional_number(_alias(raw, "volume"))
    dollar_volume = _optional_number(_alias(raw, "dollar_volume"))
    if dollar_volume is None and price is not None and volume is not None:
        dollar_volume = round(price * volume, 2)
    if price is None and change_pct is None and volume is None:
        raise ValueError("missing_mover_values: price, change percent, or volume is required")
    rank = _optional_int(_alias(raw, "rank")) or row_index
    effective_extracted_at = extracted_at
    effective_corporate_action_status = corporate_action_status
    effective_corporate_action_source_ref = corporate_action_source_ref
    effective_corporate_action_source_path = corporate_action_source_path
    if trusted_row_truth_fields:
        effective_extracted_at = str(_alias(raw, "extracted_at") or extracted_at)
        effective_corporate_action_status = str(
            _alias(raw, "corporate_action_status") or corporate_action_status
        ).strip().lower()
        effective_corporate_action_source_ref = str(
            _alias(raw, "corporate_action_source_ref")
            or corporate_action_source_ref
        ).strip()
        effective_corporate_action_source_path = str(
            _alias(raw, "corporate_action_source_path")
            or corporate_action_source_path
        ).strip()
    truth = {
        "system_received_at": system_received_at,
        "dataset_role": dataset_role,
        "prospective_signal_eligible": bool(prospective_signal_eligible),
        "source_snapshot_kind": source_snapshot_kind,
        "ingestion_channel": ingestion_channel,
        "source_artifact_ref": source_artifact_ref,
        "source_artifact_path": source_artifact_path,
        "source_coverage_complete": bool(source_coverage_complete),
        # Compatibility names consumed by the strict all-candidate EOD join.
        "source_complete": bool(source_coverage_complete),
        "list_coverage_complete": bool(list_coverage_complete),
        "expected_row_count": expected_row_count,
        "corporate_action_status": effective_corporate_action_status,
        "corporate_action_source_ref": effective_corporate_action_source_ref,
        "corporate_action_source_path": effective_corporate_action_source_path,
        "eod_label_eligible": bool(eod_label_eligible),
        "source_ref": source_artifact_ref,
    }
    row = {
        "mover_id": f"mover:{market_date}:{source}:{rank}:{ticker}",
        "market_date": market_date,
        "date": market_date,
        "ticker": ticker,
        "company": str(_alias(raw, "company") or ""),
        "rank": rank,
        "price": price,
        "change_pct": change_pct,
        "volume": volume,
        "dollar_volume": dollar_volume,
        "open": _optional_number(_alias(raw, "open")),
        "high": _optional_number(_alias(raw, "high")),
        "low": _optional_number(_alias(raw, "low")),
        "close": _optional_number(_alias(raw, "close")),
        "source": source,
        "source_url": source_url,
        "source_confidence": source_confidence,
        "extracted_at": effective_extracted_at,
        "system_received_at": system_received_at,
        "data_quality": data_quality,
        "shadow_mode": True,
        "paid_data": False,
        **truth,
        "payload_json": {
            **dict(raw),
            **truth,
        },
    }
    return row


def _alias(row: dict[str, Any], field: str) -> Any:
    normalized = {_clean_header(key): value for key, value in row.items()}
    for alias in ALIASES[field]:
        key = _clean_header(alias)
        if key in normalized and normalized[key] not in {None, ""}:
            return normalized[key]
    return None


def _clean_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("_", " ")).strip().lower()


def _clean_ticker(value: Any) -> str:
    text = str(value or "").upper().strip()
    text = re.sub(r"[^A-Z0-9.\-]", "", text)
    return text if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,4}", text) else ""


def _optional_int(value: Any) -> int | None:
    number = _optional_number(value)
    return int(number) if number is not None else None


def _optional_number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "none", "-"}:
        return None
    multiplier = 1.0
    text = text.replace("$", "").replace(",", "").replace("%", "").strip()
    suffix = text[-1:].lower()
    if suffix in {"k", "m", "b"}:
        multiplier = {"k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0}[suffix]
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None
