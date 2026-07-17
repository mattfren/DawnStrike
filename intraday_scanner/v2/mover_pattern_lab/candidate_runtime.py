"""Operator boundary for the all-candidate mover outcome study.

The pure study engine lives in :mod:`candidate_study`.  This module adds the
fail-closed file, lineage, and immutable-artifact checks needed by the CLI.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from intraday_scanner.errors import MarketCalendarCoverageError
from intraday_scanner.market_calendar import MARKET_TIMEZONE, market_session
from intraday_scanner.v2.data import load_ohlcv_csv

from .candidate_study import (
    CandidateSplitAssignment,
    CandidateStudyAssumptions,
    CandidateUniverseDenominator,
    study_all_candidates,
)
from .contracts import ProspectiveMoverSnapshot
from .core import (
    DEFAULT_OUTPUT_ROOT,
    MoverLabPaths,
    _forward_receipt_valid,
    _forward_universe_artifact_valid,
    _json_fingerprint,
    _read_jsonl,
    _rows_match_by_id_artifacts,
    _sha256_file,
    _snapshot_identity_valid,
    _source_artifact_refs_valid,
    _validate_csv_timestamp_awareness,
    _validate_market_bars,
    _write_immutable_json,
    _write_immutable_jsonl,
    init,
)

SCHEMA_VERSION = "v2.mover_candidate_study_runtime.v1"


def run_candidate_study(
    *,
    snapshots_path: Path,
    bars_csv: Path,
    universe_denominators_path: Path,
    split_assignments_path: Path,
    descriptive_eod_movers_path: Path | None = None,
    bar_interval_minutes: int = 5,
    slippage_bps: float = 10.0,
    fee_bps: float = 1.0,
    bar_timestamp_semantics: str,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    """Label every retained candidate under one immutable paper policy."""

    if bar_timestamp_semantics != "bar_close":
        raise ValueError("candidate study requires bar_timestamp_semantics='bar_close'")
    _validate_csv_timestamp_awareness(bars_csv)
    init(output_root=output_root)
    paths = MoverLabPaths.create(output_root)

    snapshot_rows = _read_jsonl(snapshots_path)
    snapshots = [ProspectiveMoverSnapshot.from_mapping(row) for row in snapshot_rows]
    if not snapshots:
        raise ValueError("candidate study requires at least one retained snapshot")
    snapshot_ids = [row.snapshot_id for row in snapshots]
    if len(snapshot_ids) != len(set(snapshot_ids)):
        raise ValueError("candidate study snapshots contain duplicate IDs")
    if not _rows_match_by_id_artifacts(
        snapshot_rows,
        paths.snapshots / "by_id",
        "snapshot_id",
    ):
        raise ValueError("candidate study requires exact retained snapshot artifacts")
    if not all(_snapshot_identity_valid(row) for row in snapshot_rows):
        raise ValueError("candidate study snapshot identity does not match its content")
    if not _source_artifact_refs_valid(snapshot_rows):
        raise ValueError("candidate study snapshot source artifacts are hash-invalid")
    if not all(_forward_receipt_valid(row) for row in snapshot_rows):
        raise ValueError("candidate study forward receipt lineage is invalid")
    if not all(_forward_universe_artifact_valid(row) for row in snapshot_rows):
        raise ValueError("candidate study forward universe lineage is invalid")
    snapshots_ref = _retain_raw_artifact(
        snapshots_path,
        paths.source_artifacts / "candidate_study" / "snapshots",
    )
    retained_snapshots_path = _artifact_path(snapshots_ref)

    dataset = load_ohlcv_csv(
        bars_csv,
        dataset_id=f"mover_candidate_study:{_sha256_file(bars_csv)[:16]}",
        source_kind="operator_intraday_csv",
        timeframe="intraday",
    )
    if dataset.warnings:
        raise ValueError("bars CSV contains rejected rows: " + "; ".join(dataset.warnings))
    _validate_market_bars(dataset.bars_by_symbol)
    bars = tuple(
        bar
        for symbol in sorted(dataset.bars_by_symbol)
        for bar in dataset.bars_by_symbol[symbol]
    )
    bars_ref = _retain_raw_artifact(
        bars_csv,
        paths.source_artifacts / "candidate_study" / "bars",
    )
    retained_bars_path = _artifact_path(bars_ref)

    denominator_rows = _read_rows(universe_denominators_path)
    denominators = [
        CandidateUniverseDenominator.from_mapping(row) for row in denominator_rows
    ]
    if not denominators:
        raise ValueError("candidate study requires universe denominators")
    snapshot_group_keys = {
        (snapshot.market_date, snapshot.feature_cutoff_at)
        for snapshot in snapshots
    }
    denominator_group_keys = {
        (denominator.market_date, denominator.feature_cutoff_at)
        for denominator in denominators
    }
    if denominator_group_keys != snapshot_group_keys:
        raise ValueError(
            "universe denominators must exactly match retained snapshot cohorts"
        )
    for denominator in denominators:
        if not _artifact_ref_valid(denominator.source_ref):
            raise ValueError(
                "universe denominator source_ref must identify a retained, hash-valid artifact"
            )
        if not _universe_artifact_matches(denominator):
            raise ValueError(
                "universe denominator declaration does not match its retained artifact"
            )
        group = [
            snapshot
            for snapshot in snapshots
            if snapshot.market_date == denominator.market_date
            and snapshot.feature_cutoff_at == denominator.feature_cutoff_at
        ]
        if any(
            snapshot.universe_source_ref != denominator.source_ref
            or snapshot.universe_selection_method
            != denominator.universe_selection_method
            or snapshot.evidence_mode != denominator.evidence_mode
            for snapshot in group
        ):
            raise ValueError(
                "universe denominator is not bound to snapshot universe lineage"
            )
    denominators_ref = _retain_raw_artifact(
        universe_denominators_path,
        paths.source_artifacts / "candidate_study" / "denominators",
    )
    retained_denominators_path = _artifact_path(denominators_ref)

    split_payload = json.loads(split_assignments_path.read_text(encoding="utf-8"))
    if not isinstance(split_payload, Mapping):
        raise ValueError("split assignments file must contain a JSON object")
    assignments_raw = split_payload.get("assignments", split_payload)
    if not isinstance(assignments_raw, Mapping):
        raise ValueError("split assignments must be a snapshot_id-to-split object")
    split_ref = _retain_raw_artifact(
        split_assignments_path,
        paths.source_artifacts / "candidate_study" / "splits",
    )
    retained_splits_path = _artifact_path(split_ref)
    split_assignment = CandidateSplitAssignment.create(
        {str(key): str(value) for key, value in assignments_raw.items()},
        source_ref=split_ref,
    )
    split_registry_path = (
        paths.manifests
        / "candidate_split_registry"
        / f"{split_assignment.assignment_id}.json"
    )
    _write_immutable_json(split_registry_path, split_assignment.to_dict())

    eod_rows: list[dict[str, Any]] = []
    retained_eod_path: Path | None = None
    if descriptive_eod_movers_path is not None:
        for raw in _read_rows(descriptive_eod_movers_path):
            row = _normalize_eod_row(raw)
            source_ref = str(row.get("source_ref") or "")
            if not _artifact_ref_valid(source_ref):
                raise ValueError(
                    "descriptive EOD mover rows require a retained, hash-valid source_ref"
                )
            corporate_action_ref = str(
                row.get("corporate_action_source_ref") or ""
            )
            if (
                not _artifact_ref_valid(corporate_action_ref)
                or corporate_action_ref == source_ref
                or _same_artifact_path(corporate_action_ref, source_ref)
                or not _corporate_action_artifact_matches(row)
            ):
                raise ValueError(
                    "descriptive EOD mover rows require independent, hash-valid "
                    "corporate-action evidence"
                )
            eod_rows.append(row)
        retained_eod_ref = _retain_raw_artifact(
            descriptive_eod_movers_path,
            paths.source_artifacts / "candidate_study" / "eod",
        )
        retained_eod_path = _artifact_path(retained_eod_ref)

    assumptions = CandidateStudyAssumptions(
        bar_interval_minutes=bar_interval_minutes,
        slippage_bps=float(slippage_bps),
        fee_bps=float(fee_bps),
    )
    result = study_all_candidates(
        snapshots=snapshots,
        bars=bars,
        universe_denominators=denominators,
        split_assignment=split_assignment,
        assumptions=assumptions,
        bars_source_ref=bars_ref,
        descriptive_eod_movers=eod_rows,
    )
    result_payload = result.to_dict()
    study_path = paths.reports / "candidate_studies" / f"study_{result.study_id}.json"
    outcomes = [row.to_dict() for row in result.outcomes]
    outcomes_fingerprint = _json_fingerprint(outcomes)
    outcomes_path = (
        paths.trades
        / "candidate_outcomes"
        / f"outcomes_{outcomes_fingerprint[:16]}.jsonl"
    )
    coverage_rows = [row.to_dict() for row in result.coverage]
    coverage_path = (
        paths.reports
        / "candidate_studies"
        / f"coverage_{_json_fingerprint(coverage_rows)[:16]}.csv"
    )
    _write_immutable_json(study_path, result_payload)
    _write_immutable_jsonl(outcomes_path, outcomes)
    _write_coverage_csv(coverage_path, coverage_rows)

    pending_count = sum(
        1 for row in outcomes if str(row.get("status") or "").startswith("pending_")
    )
    complete_count = sum(1 for row in outcomes if row.get("status") == "complete")
    coverage_complete = bool(coverage_rows) and all(
        row.get("snapshot_coverage_complete") is True
        and row.get("outcome_coverage_complete") is True
        for row in coverage_rows
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "passed_with_pending" if pending_count else "passed",
        "study_id": result.study_id,
        "evidence_mode": result.evidence_mode,
        "study_path": str(study_path.resolve()),
        "study_sha256": _sha256_file(study_path),
        "outcomes_path": str(outcomes_path.resolve()),
        "outcomes_sha256": _sha256_file(outcomes_path),
        "coverage_path": str(coverage_path.resolve()),
        "coverage_sha256": _sha256_file(coverage_path),
        "snapshot_count": len(snapshots),
        "complete_outcome_count": complete_count,
        "pending_outcome_count": pending_count,
        "coverage_group_count": len(coverage_rows),
        "all_candidate_coverage_complete": coverage_complete,
        "discovery_correlation_count": len(result.discovery_correlations),
        "mover_control_comparison_count": len(result.mover_control_comparisons),
        "snapshots_path": str(retained_snapshots_path),
        "snapshots_sha256": _sha256_file(retained_snapshots_path),
        "original_snapshots_path": str(snapshots_path.resolve()),
        "bars_csv": str(retained_bars_path),
        "bars_csv_sha256": _sha256_file(retained_bars_path),
        "original_bars_csv": str(bars_csv.resolve()),
        "bars_source_ref": bars_ref,
        "universe_denominators_path": str(retained_denominators_path),
        "universe_denominators_sha256": _sha256_file(retained_denominators_path),
        "original_universe_denominators_path": str(
            universe_denominators_path.resolve()
        ),
        "split_assignments_path": str(retained_splits_path),
        "split_assignments_sha256": _sha256_file(retained_splits_path),
        "original_split_assignments_path": str(split_assignments_path.resolve()),
        "split_registry_path": str(split_registry_path.resolve()),
        "split_registry_sha256": _sha256_file(split_registry_path),
        "descriptive_eod_movers_path": (
            str(retained_eod_path)
            if retained_eod_path is not None
            else None
        ),
        "descriptive_eod_movers_sha256": (
            _sha256_file(retained_eod_path)
            if retained_eod_path is not None
            else None
        ),
        "original_descriptive_eod_movers_path": (
            str(descriptive_eod_movers_path.resolve())
            if descriptive_eod_movers_path is not None
            else None
        ),
        "assumptions": assumptions.to_dict(),
        "bar_timestamp_semantics": bar_timestamp_semantics,
        "general_mover_research_data_complete": coverage_complete,
        "forward_learning_eligible": (
            result.evidence_mode == "forward_observation" and coverage_complete
        ),
        "automatic_strategy_creation": False,
        "automatic_promotion_enabled": False,
        "performance_claim_eligible": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    run_fingerprint = _json_fingerprint(manifest)
    run_payload = {**manifest, "run_fingerprint": run_fingerprint}
    manifest_path = (
        paths.manifests / f"candidate_study_{run_fingerprint[:16]}.json"
    )
    _write_immutable_json(manifest_path, run_payload)
    latest = {**run_payload, "run_manifest_path": str(manifest_path.resolve())}
    latest_path = paths.manifests / "candidate_study_latest.json"
    latest_path.write_text(
        json.dumps(latest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return latest


def _read_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError(f"{path} is an empty CSV")
            return [dict(row) for row in reader]
    if text.startswith("["):
        payload = json.loads(text)
        if not isinstance(payload, list) or not all(
            isinstance(row, dict) for row in payload
        ):
            raise ValueError(f"{path} must contain a JSON array of objects")
        return [dict(row) for row in payload]
    return _read_jsonl(path)


def _normalize_eod_row(row: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    artifact_ref = str(
        normalized.get("source_ref")
        or normalized.get("source_artifact_ref")
        or ""
    )
    artifact_path = str(normalized.get("source_artifact_path") or "").strip()
    if artifact_ref.count(":") == 1 and artifact_path:
        artifact_ref = f"{artifact_ref}:{artifact_path}"
    normalized["source_ref"] = artifact_ref
    normalized["source_artifact_ref"] = artifact_ref
    corporate_action_ref = str(
        normalized.get("corporate_action_source_ref") or ""
    ).strip()
    corporate_action_path = str(
        normalized.get("corporate_action_source_path") or ""
    ).strip()
    if corporate_action_ref.count(":") == 1 and corporate_action_path:
        corporate_action_ref = f"{corporate_action_ref}:{corporate_action_path}"
    normalized["corporate_action_source_ref"] = corporate_action_ref
    if "source_complete" not in normalized:
        normalized["source_complete"] = normalized.get("source_coverage_complete")
    if "list_coverage_complete" not in normalized:
        normalized["list_coverage_complete"] = normalized.get(
            "source_coverage_complete"
        )
    for field in (
        "source_complete",
        "list_coverage_complete",
        "source_coverage_complete",
        "eod_label_eligible",
        "prospective_signal_eligible",
    ):
        if field in normalized:
            normalized[field] = _strict_bool(normalized[field], field)
    for field in ("rank", "mover_rank", "expected_row_count"):
        value = normalized.get(field)
        if value not in {None, ""}:
            try:
                normalized[field] = int(str(value))
            except ValueError as exc:
                raise ValueError(f"{field} must be an integer") from exc
    return normalized


def _strict_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"{field} must be an explicit boolean")


def _retain_raw_artifact(source: Path, directory: Path) -> str:
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    suffix = source.suffix.lower() or ".bin"
    target = directory / f"{digest}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != payload:
            raise ValueError(f"immutable raw artifact conflict: {target}")
    else:
        target.write_bytes(payload)
    return f"sha256:{digest}:{target.resolve()}"


def _artifact_path(reference: str) -> Path:
    parts = reference.split(":", 2)
    if len(parts) != 3 or parts[0] != "sha256":
        raise ValueError("content artifact reference is malformed")
    return Path(parts[2]).resolve()


def _artifact_ref_valid(ref: str) -> bool:
    parts = ref.split(":", 2)
    if len(parts) != 3 or parts[0] != "sha256" or len(parts[1]) != 64:
        return False
    path = Path(parts[2])
    if not path.is_file():
        return False
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() == parts[1]:
        return True
    try:
        decoded = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return _json_fingerprint(decoded) == parts[1]


def _corporate_action_artifact_matches(row: Mapping[str, Any]) -> bool:
    reference = str(row.get("corporate_action_source_ref") or "")
    parts = reference.split(":", 2)
    if len(parts) != 3:
        return False
    try:
        payload = json.loads(Path(parts[2]).read_text(encoding="utf-8"))
        observed_at = _aware_timestamp(payload.get("observed_at"))
        received_at = _aware_timestamp(row.get("system_received_at"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("schema_version")
        == "v2.corporate_action_evidence.v1"
        and payload.get("market_date")
        == str(row.get("market_date") or row.get("date") or "")
        and str(payload.get("symbol") or "").upper()
        == str(row.get("symbol") or row.get("ticker") or "").upper()
        and str(payload.get("corporate_action_status") or "").lower()
        == str(row.get("corporate_action_status") or "").lower()
        and bool(str(payload.get("source") or "").strip())
        and _after_published_market_close(
            str(row.get("market_date") or row.get("date") or ""),
            observed_at,
        )
        and observed_at <= received_at
        and payload.get("research_only") is True
        and payload.get("broker_execution_enabled") is False
    )


def _same_artifact_path(first_ref: str, second_ref: str) -> bool:
    first_parts = first_ref.split(":", 2)
    second_parts = second_ref.split(":", 2)
    if len(first_parts) != 3 or len(second_parts) != 3:
        return True
    try:
        return Path(first_parts[2]).resolve() == Path(second_parts[2]).resolve()
    except OSError:
        return True


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


def _aware_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _universe_artifact_matches(
    denominator: CandidateUniverseDenominator,
) -> bool:
    parts = denominator.source_ref.split(":", 2)
    if len(parts) != 3:
        return False
    try:
        payload = json.loads(Path(parts[2]).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    return bool(
        payload.get("schema_version") == "v2.mover_candidate_universe.v1"
        and payload.get("market_date") == denominator.market_date
        and payload.get("feature_cutoff_at")
        == denominator.feature_cutoff_at.isoformat()
        and payload.get("evidence_mode") == denominator.evidence_mode
        and payload.get("system_received_at")
        == (
            denominator.system_received_at.isoformat()
            if denominator.system_received_at is not None
            else None
        )
        and payload.get("universe_selection_method")
        == denominator.universe_selection_method
        and payload.get("expected_symbols")
        == list(denominator.expected_symbols)
        and payload.get("expected_symbols_complete")
        is denominator.expected_symbols_complete
        and payload.get("research_only") is True
        and payload.get("broker_execution_enabled") is False
    )


def _write_coverage_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = tuple(rows[0]) if rows else ()
    rendered_rows = [
        {
            key: (
                json.dumps(value, separators=(",", ":"))
                if isinstance(value, (list, dict))
                else value
            )
            for key, value in row.items()
        }
        for row in rows
    ]
    from io import StringIO

    stream = StringIO(newline="")
    if fieldnames:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rendered_rows)
    text = stream.getvalue().replace("\r\n", "\n")
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError(f"immutable coverage artifact conflict: {path}")
    else:
        path.write_text(text, encoding="utf-8", newline="\n")


__all__ = ["run_candidate_study"]
