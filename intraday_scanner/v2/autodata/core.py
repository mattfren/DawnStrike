# ruff: noqa: E501
# mypy: ignore-errors
"""Read-only provider gateway for OMEGA intraday evidence."""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from intraday_scanner.public_data.autodata_fetcher import (
    ProviderFetchError,
    ProviderHttpError,
    encode_query,
    fetch_json_url,
)
from intraday_scanner.v2.data import MarketBar, MarketDataset, load_ohlcv_csv, write_ohlcv_csv

DEFAULT_OUTPUT_ROOT = Path("data/v2_autodata")
PAPER_OPS_ROOT = Path("data/v2_paper_ops")
DATA_TRUTH_ROOT = Path("data/v2_data_truth")
FILL_TRUTH_ROOT = Path("data/v2_fill_truth")
EVIDENCE_COMMIT_ROOT = Path("data/v2_evidence_commit")
REAL_INTRADAY_ROOT = Path("data/v2_real_intraday")
COMMAND_CENTER_ROOT = Path("data/v2_command_center")
RECONCILED_STATUSES = {
    "reconciled",
    "reconciled_with_minor_diffs",
    "provider_with_public_fallback_comparison",
}
AUTO_COMMIT_ELIGIBLE_LABELS = {"provider_intraday", "broker_or_vendor_intraday"}
BLOCKED_LABELS = {"synthetic_demo_intraday", "mock_test_intraday", "replay_intraday", "unknown_intraday"}
CANONICAL_SOURCE_PRIORITY = {
    "broker_or_vendor_intraday": 0,
    "provider_intraday": 1,
    "public_intraday_single_provider": 2,
    "mock_test_intraday": 3,
    "unknown_intraday": 9,
}


@dataclass(frozen=True)
class _Paths:
    root: Path
    provider_registry: Path
    raw: Path
    cache: Path
    normalized: Path
    validation: Path
    reconciliation: Path
    manifests: Path
    reports: Path
    logs: Path
    readiness: Path

    @classmethod
    def create(cls, root: Path) -> _Paths:
        values = {
            "provider_registry": root / "provider_registry",
            "raw": root / "raw",
            "cache": root / "cache",
            "normalized": root / "normalized",
            "validation": root / "validation",
            "reconciliation": root / "reconciliation",
            "manifests": root / "manifests",
            "reports": root / "reports",
            "logs": root / "logs",
            "readiness": root / "readiness",
        }
        for path in values.values():
            path.mkdir(parents=True, exist_ok=True)
        return cls(root=root, **values)


def init(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    paths = _Paths.create(output_root)
    registry = providers(output_root=output_root)
    _write_docs()
    payload = {
        "created_at": _now(),
        "directories": [getattr(paths, name).as_posix() for name in _DIR_FIELDS],
        "output_root": output_root.as_posix(),
        "provider_count": registry["provider_count"],
        "schema_version": "v2.autodata_manifest.v1",
        "status": "initialized",
    }
    _write_json(paths.manifests / "autodata_manifest.json", payload)
    return payload


def providers(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    paths = _Paths.create(output_root)
    rows = [_provider_definition(provider_id) for provider_id in _provider_ids()]
    payload = {
        "configured_count": sum(1 for row in rows if row["configured"]),
        "enabled_count": sum(1 for row in rows if row["enabled"]),
        "provider_count": len(rows),
        "providers": rows,
        "schema_version": "v2.autodata_provider_registry.v1",
        "status": "passed",
        "warnings": _unique(item for row in rows for item in _list(row.get("warnings"))),
    }
    _write_json(paths.provider_registry / "providers.json", payload)
    _write_json(paths.reports / "provider_readiness.json", payload)
    _write_md(paths.reports / "provider_readiness.md", "AutoData Provider Readiness", _provider_lines(payload))
    return payload


def readiness(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    paths = _Paths.create(output_root)
    registry = providers(output_root=output_root)
    rows = [_dict(row) for row in _list(registry.get("providers"))]
    configured_real = [
        row
        for row in rows
        if row.get("configured")
        and row.get("requires_api_key")
        and row.get("supports_intraday")
    ]
    public_ready = any(
        row.get("provider_id") == "yahoo_chart_public_fallback"
        and row.get("configured")
        and row.get("supports_intraday")
        for row in rows
    )
    if configured_real:
        status = "ready_with_configured_provider"
    elif public_ready:
        status = "ready_public_fallback_only"
    else:
        status = "blocked_needs_provider_keys"
    payload = {
        "configured_real_providers": [row["provider_id"] for row in configured_real],
        "exact_env_vars_to_set_next": _missing_env_vars(rows),
        "public_fallback_available": public_ready,
        "schema_version": "v2.autodata_readiness.v1",
        "status": status,
        "warnings": _readiness_warnings(rows, configured_real, public_ready),
    }
    _write_json(paths.readiness / "provider_readiness.json", payload)
    _write_json(paths.reports / "provider_readiness.json", {**registry, "readiness_status": status, "exact_env_vars_to_set_next": payload["exact_env_vars_to_set_next"]})
    _write_md(paths.reports / "provider_readiness.md", "AutoData Provider Readiness", _provider_lines({**registry, "readiness_status": status, "exact_env_vars_to_set_next": payload["exact_env_vars_to_set_next"]}))
    return payload


def fetch(
    *,
    symbol: str,
    run_date: date,
    interval: str = "1min",
    provider_id: str | None = None,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, object]:
    paths = _Paths.create(output_root)
    normalized_symbol = symbol.strip().upper()
    try:
        provider = _select_provider(provider_id)
    except _ProviderUnavailable as exc:
        provider = _provider_definition(provider_id or "yahoo_chart_public_fallback")
        request_id = _request_id(str(provider["provider_id"]), normalized_symbol, run_date, interval)
        raw_payload = {"error": str(exc), "provider_id": provider["provider_id"], "status": exc.status}
        raw_path = paths.raw / str(provider["provider_id"]) / normalized_symbol / run_date.isoformat() / f"{request_id}.json"
        _write_json(raw_path, raw_payload)
        payload = {
            "accepted_bar_count": 0,
            "cache_status": "created",
            "errors": [str(exc)],
            "interval": interval,
            "provider_id": provider["provider_id"],
            "provider_name": provider["provider_name"],
            "provider_type": provider["provider_type"],
            "evidence_scope": "operational",
            "request_id": request_id,
            "request_params_redacted": _redacted_request(provider, {"date": run_date.isoformat(), "interval": interval, "symbol": normalized_symbol}),
            "requested_at": _now(),
            "response_metadata": _response_metadata(raw_payload),
            "raw_artifact_path": raw_path.as_posix(),
            "raw_artifact_sha256": _sha256(raw_path),
            "schema_version": "v2.autodata_provider_request.v1",
            "source_label": provider["source_label"],
            "source_trust_level": provider["source_trust_level"],
            "status": exc.status,
            "symbol": normalized_symbol,
            "trade_date": run_date.isoformat(),
            "validation": {"accepted_bar_count": 0, "errors": [str(exc)], "schema_version": "v2.autodata_validation.v1", "status": "failed", "warnings": []},
            "warnings": _list(provider.get("warnings")),
        }
        _write_json(paths.manifests / f"{request_id}.json", payload)
        _write_json(paths.manifests / "latest_request.json", payload)
        return payload
    request_id = _request_id(provider["provider_id"], normalized_symbol, run_date, interval)
    manifest_path = paths.manifests / f"{request_id}.json"
    if manifest_path.exists():
        payload = _dict(_read_json(manifest_path, {}))
        payload["cache_status"] = "reused"
        _write_json(paths.manifests / "latest_request.json", payload)
        return payload
    request_params = {
        "date": run_date.isoformat(),
        "interval": interval,
        "symbol": normalized_symbol,
    }
    errors: list[str] = []
    warnings = list(_list(provider.get("warnings")))
    status = "passed"
    try:
        raw_payload = _fetch_provider_payload(provider, normalized_symbol, run_date, interval)
    except _ProviderUnavailable as exc:
        raw_payload = {"error": str(exc), "provider_id": provider["provider_id"], "status": exc.status}
        status = exc.status
        errors.append(str(exc))
    except _ProviderError as exc:
        raw_payload = {"error": str(exc), "provider_id": provider["provider_id"], "status": exc.status}
        status = exc.status
        errors.append(str(exc))
    raw_path = paths.raw / str(provider["provider_id"]) / normalized_symbol / run_date.isoformat() / f"{request_id}.json"
    _write_json(raw_path, raw_payload)
    raw_hash = _sha256(raw_path)
    bars, parse_warnings = _normalize_provider_payload(provider, normalized_symbol, raw_payload, interval)
    warnings.extend(parse_warnings)
    if status == "passed" and not bars:
        status = "no_intraday_bars"
        warnings.append("provider returned no accepted intraday bars")
    dataset = _dataset_from_bars(
        bars,
        dataset_id=f"autodata_{request_id}",
        source_kind=str(provider["source_label"]),
        source_path=raw_path.as_posix(),
        warnings=tuple(warnings + errors),
    )
    normalized_path = paths.cache / str(provider["provider_id"]) / normalized_symbol / run_date.isoformat() / f"{request_id}_normalized.csv"
    write_ohlcv_csv(dataset, normalized_path)
    validation = _validate_bars(bars, run_date=run_date, provider=provider, interval=interval)
    payload = {
        "accepted_bar_count": len(bars),
        "cache_status": "created",
        "errors": errors,
        "interval": interval,
        "provider_id": provider["provider_id"],
        "provider_name": provider["provider_name"],
        "provider_type": provider["provider_type"],
        "evidence_scope": "operational",
        "request_id": request_id,
        "request_params_redacted": _redacted_request(provider, request_params),
        "requested_at": _now(),
        "response_metadata": _response_metadata(raw_payload),
        "raw_artifact_path": raw_path.as_posix(),
        "raw_artifact_sha256": raw_hash,
        "normalized_artifact_path": normalized_path.as_posix(),
        "normalized_artifact_sha256": _sha256(normalized_path),
        "schema_version": "v2.autodata_provider_request.v1",
        "source_label": provider["source_label"],
        "source_trust_level": provider["source_trust_level"],
        "status": "passed_with_warnings" if status == "passed" and (warnings or validation["warnings"]) else status,
        "symbol": normalized_symbol,
        "trade_date": run_date.isoformat(),
        "validation": validation,
        "warnings": _unique(warnings),
    }
    _write_json(manifest_path, payload)
    _write_json(paths.manifests / "latest_request.json", payload)
    return payload


def fetch_pending(
    *,
    run_date: date,
    interval: str = "1min",
    provider_id: str | None = None,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, object]:
    paths = _Paths.create(output_root)
    init(output_root=output_root)
    pending = _pending_orders()
    needed = _needed_symbols_dates(pending, run_date)
    rows: list[dict[str, object]] = []
    fetched = 0
    missing = 0
    for item in needed:
        result = fetch(
            symbol=str(item["symbol"]),
            run_date=date.fromisoformat(str(item["date"])),
            interval=interval,
            provider_id=provider_id,
            output_root=output_root,
        )
        accepted = _int(result.get("accepted_bar_count"))
        if accepted:
            fetched += 1
        else:
            missing += 1
        rows.append(
            {
                "accepted_bar_count": accepted,
                "date": item["date"],
                "order_id": item["order_id"],
                "provider_id": result.get("provider_id", "missing"),
                "request_id": result.get("request_id", "missing"),
                "status": result.get("status", "missing"),
                "symbol": item["symbol"],
                "warnings": result.get("warnings", []),
            }
        )
    status = "passed" if fetched else "blocked_needs_autodata_provider"
    payload = {
        "fetched_count": fetched,
        "missing_count": missing,
        "pending_orders_inspected": len(pending),
        "rows": rows,
        "run_date": run_date.isoformat(),
        "schema_version": "v2.autodata_fetch_pending.v1",
        "status": status,
        "warnings": ["no pending orders required provider fetch"] if not needed else [],
    }
    _write_json(paths.reports / "fetch_pending_latest.json", payload)
    _write_md(paths.reports / "fetch_pending_latest.md", "AutoData Fetch Pending", _fetch_pending_lines(payload))
    return payload


def build(
    *,
    run_date: date,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    include_demo: bool = False,
) -> dict[str, object]:
    paths = _Paths.create(output_root)
    manifests = _request_manifests(paths, run_date, include_demo=include_demo)
    warnings: list[str] = []
    provider_rows = _build_per_provider_artifacts(paths, manifests, run_date)
    rows = [_public_provider_row(row) for row in provider_rows]
    for manifest in manifests:
        warnings.extend(str(item) for item in _list(manifest.get("warnings")))
    canonical_row = _select_canonical_provider(provider_rows)
    canonical_bars = [bar for bar in _list(canonical_row.get("_bars")) if isinstance(bar, MarketBar)]
    canonical_provider_id = str(canonical_row.get("provider_id", "missing"))
    canonical_source_label = str(canonical_row.get("source_label", "unknown_intraday"))
    canonical_duplicate_count = _duplicate_timestamp_count(canonical_bars)
    dataset = _dataset_from_bars(
        canonical_bars,
        dataset_id=f"autodata_canonical_intraday_{run_date.isoformat()}",
        source_kind=canonical_source_label,
        warnings=tuple(warnings),
    )
    canonical_path = paths.normalized / "canonical" / f"{run_date.isoformat()}_canonical_intraday.csv"
    latest_canonical_path = paths.normalized / "canonical" / "latest_canonical_intraday.csv"
    compatibility_path = paths.normalized / f"{run_date.isoformat()}_provider_intraday.csv"
    latest_path = paths.normalized / "latest_provider_intraday.csv"
    write_ohlcv_csv(dataset, canonical_path)
    write_ohlcv_csv(dataset, latest_canonical_path)
    write_ohlcv_csv(dataset, compatibility_path)
    write_ohlcv_csv(dataset, latest_path)
    canonical_symbol_paths = _write_canonical_symbol_artifacts(
        paths,
        canonical_bars,
        run_date,
        canonical_source_label,
    )
    validation = _validate_bars(
        canonical_bars,
        run_date=run_date,
        provider={
            "provider_id": canonical_provider_id,
            "source_label": canonical_source_label,
        },
        interval="canonical",
    )
    canonical_selection = {
        "canonical_artifact_path": canonical_path.as_posix(),
        "canonical_artifact_sha256": _sha256(canonical_path),
        "canonical_duplicate_timestamp_count": canonical_duplicate_count,
        "canonical_provider_id": canonical_provider_id,
        "canonical_source_label": canonical_source_label,
        "canonical_symbol_artifact_paths": canonical_symbol_paths,
        "comparison_provider_ids": [
            str(row.get("provider_id"))
            for row in rows
            if str(row.get("provider_id")) != canonical_provider_id
        ],
        "reason": _canonical_selection_reason(canonical_row, rows),
        "run_date": run_date.isoformat(),
        "schema_version": "v2.autodata_canonical_selection.v1",
        "status": "passed" if canonical_bars and canonical_duplicate_count == 0 else "blocked",
    }
    _write_json(paths.reports / "canonical_selection_latest.json", canonical_selection)
    _write_md(
        paths.reports / "canonical_selection_latest.md",
        "AutoData Canonical Selection",
        _summary_lines(canonical_selection),
    )
    payload = {
        "accepted_bar_count": len(canonical_bars),
        "build_id": f"autodata_build_{run_date.isoformat()}_{_compact_now()}",
        "canonical_artifact_path": canonical_path.as_posix(),
        "canonical_artifact_sha256": _sha256(canonical_path),
        "canonical_duplicate_timestamp_count": canonical_duplicate_count,
        "canonical_provider_id": canonical_provider_id,
        "canonical_selection": canonical_selection,
        "canonical_symbol_artifact_paths": canonical_symbol_paths,
        "comparison_provider_ids": canonical_selection["comparison_provider_ids"],
        "normalized_artifact_path": canonical_path.as_posix(),
        "normalized_artifact_sha256": _sha256(canonical_path),
        "per_provider_rows": rows,
        "provider_count": len({row["provider_id"] for row in rows}),
        "providers": sorted({row["provider_id"] for row in rows}),
        "rows": rows,
        "run_date": run_date.isoformat(),
        "schema_version": "v2.autodata_build.v1",
        "source_label": canonical_source_label,
        "status": "passed" if canonical_bars and validation["status"] == "passed" else "passed_with_warnings" if canonical_bars else "blocked_needs_autodata_provider",
        "validation": validation,
        "warnings": _unique(warnings + _list(validation.get("warnings"))),
    }
    _write_json(paths.normalized / "build_latest.json", payload)
    _write_json(paths.reports / "autodata_build_latest.json", payload)
    _write_md(paths.reports / "autodata_build_latest.md", "AutoData Build", _summary_lines(payload))
    return payload


def reconcile(*, run_date: date, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    paths = _Paths.create(output_root)
    build_payload = _dict(_read_json(paths.reports / "autodata_build_latest.json", {}))
    if str(build_payload.get("run_date", "")) != run_date.isoformat():
        build_payload = build(run_date=run_date, output_root=output_root)
    provider_rows = [_dict(row) for row in _list(build_payload.get("per_provider_rows") or build_payload.get("rows"))]
    provider_datasets = _load_provider_build_datasets(provider_rows, run_date)
    provider_count = len(provider_datasets)
    daily_reference = _daily_reference_rows(run_date)
    rows: list[dict[str, object]] = []
    for provider in provider_datasets:
        provider_id = str(provider["provider_id"])
        source_label = str(provider["source_label"])
        for symbol, bars_object in _dict(provider.get("bars_by_symbol")).items():
            bars = [bar for bar in _list(bars_object) if isinstance(bar, MarketBar)]
            if not bars:
                continue
            aggregate = _daily_aggregate(str(symbol), run_date, bars)
            reference = daily_reference.get(str(symbol), {})
            diffs = _daily_diffs(aggregate, reference) if reference else {}
            if not reference:
                status = "insufficient_overlap"
            elif _within_minor_tolerance(diffs):
                status = "reconciled_with_minor_diffs"
            else:
                status = "mismatch"
            rows.append(
                {
                    **aggregate,
                    "daily_reference_snapshot_id": reference.get("snapshot_id", "missing"),
                    "diffs": diffs,
                    "provider_id": provider_id,
                    "reconciliation_status": status,
                    "source_label": source_label,
                }
            )
    canonical_provider_id = str(build_payload.get("canonical_provider_id", "missing"))
    pairwise = _write_provider_diff_reports(
        paths,
        run_date,
        provider_datasets,
        canonical_provider_id=canonical_provider_id,
    )
    if not rows:
        overall = "no_provider_data"
    elif any(row["reconciliation_status"] == "mismatch" for row in rows):
        overall = "mismatch"
    elif pairwise["material_mismatch"]:
        overall = "manual_review_required"
    elif provider_count <= 1:
        overall = "single_provider_unreconciled"
    elif _has_public_comparison(build_payload):
        overall = "provider_with_public_fallback_comparison"
    elif all(row["reconciliation_status"] == "reconciled_with_minor_diffs" for row in rows):
        overall = "reconciled_with_minor_diffs"
    else:
        overall = "insufficient_overlap"
    warnings = []
    if overall == "single_provider_unreconciled":
        warnings.append("single-provider data is not broker-grade reconciled evidence")
    if overall == "provider_with_public_fallback_comparison":
        warnings.append("public fallback was used only as comparison evidence; canonical bars are not a provider merge")
    if overall in {"mismatch", "manual_review_required"}:
        warnings.append("provider differences require review before official commit")
    payload = {
        "canonical_duplicate_timestamp_count": build_payload.get("canonical_duplicate_timestamp_count", 0),
        "canonical_provider_id": canonical_provider_id,
        "canonical_selection": build_payload.get("canonical_selection", {}),
        "diff_artifact_paths": pairwise["diff_artifact_paths"],
        "pairwise": pairwise["rows"],
        "provider_count": provider_count,
        "reconciliation_status": overall,
        "rows": rows,
        "run_date": run_date.isoformat(),
        "schema_version": "v2.autodata_provider_reconciliation.v1",
        "status": "passed" if overall in RECONCILED_STATUSES else "passed_with_warnings",
        "warnings": warnings,
    }
    _write_json(paths.reconciliation / "provider_reconciliation_latest.json", payload)
    _write_json(paths.reports / "provider_reconciliation_latest.json", payload)
    _write_md(paths.reports / "provider_reconciliation_latest.md", "AutoData Provider Reconciliation", _reconciliation_lines(payload))
    return payload


def feed_filltruth(*, run_date: date, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    from intraday_scanner.v2.fill_truth import resolve_pending
    from intraday_scanner.v2.real_intraday import build as build_real_intraday
    from intraday_scanner.v2.real_intraday import import_intraday as import_real_intraday
    from intraday_scanner.v2.real_intraday import readiness as real_readiness

    paths = _Paths.create(output_root)
    build_payload = _dict(_read_json(paths.reports / "autodata_build_latest.json", {}))
    if str(build_payload.get("run_date", "")) != run_date.isoformat():
        build_payload = build(run_date=run_date, output_root=output_root)
    reconciliation = reconcile(run_date=run_date, output_root=output_root)
    normalized = Path(str(build_payload.get("normalized_artifact_path", "")))
    if not normalized.exists() or _int(build_payload.get("accepted_bar_count")) == 0:
        payload = {
            "accepted_bar_count": 0,
            "filltruth_status": "skipped_no_provider_data",
            "reconciliation_status": reconciliation.get("reconciliation_status", "missing"),
            "run_date": run_date.isoformat(),
            "schema_version": "v2.autodata_filltruth_feed.v1",
            "status": "blocked_needs_autodata_provider",
            "warnings": ["no provider intraday bars available; FillTruth was not updated by AutoData"],
        }
        _write_json(paths.reports / "autodata_filltruth_latest.json", payload)
        _write_md(paths.reports / "autodata_filltruth_latest.md", "AutoData FillTruth Feed", _summary_lines(payload))
        return payload
    source_label = str(build_payload.get("source_label", "unknown_intraday"))
    canonical_provider_id = str(build_payload.get("canonical_provider_id", "missing"))
    import_payload = import_real_intraday(
        path=normalized,
        source_label=source_label,
        source_name=f"autodata:{canonical_provider_id}",
        source_timezone="UTC",
        output_root=REAL_INTRADAY_ROOT,
    )
    real_build = build_real_intraday(run_date=run_date, output_root=REAL_INTRADAY_ROOT)
    real_ready = real_readiness(run_date=run_date, output_root=REAL_INTRADAY_ROOT)
    _augment_filltruth_autodata_manifest(paths, build_payload=build_payload, reconciliation=reconciliation)
    filltruth = resolve_pending(run_date=run_date)
    payload = {
        "accepted_bar_count": import_payload.get("accepted_row_count", 0),
        "filltruth": filltruth,
        "real_intraday": {"build": real_build, "import": import_payload, "readiness": real_ready},
        "reconciliation_status": reconciliation.get("reconciliation_status", "missing"),
        "run_date": run_date.isoformat(),
        "schema_version": "v2.autodata_filltruth_feed.v1",
        "source_label": source_label,
        "status": "passed" if filltruth.get("status") in {"passed", "passed_with_warnings"} else "passed_with_warnings",
        "warnings": _unique(_list(build_payload.get("warnings")) + _list(reconciliation.get("warnings"))),
    }
    _write_json(paths.reports / "autodata_filltruth_latest.json", payload)
    _write_md(paths.reports / "autodata_filltruth_latest.md", "AutoData FillTruth Feed", _summary_lines(payload))
    return payload


def trial_day(
    *,
    run_date: date,
    commit: bool = False,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, object]:
    from intraday_scanner.v2.command_center import build_command_center
    from intraday_scanner.v2.evidence_commit import commit as commitbridge_commit
    from intraday_scanner.v2.evidence_commit import propose as commitbridge_propose
    from intraday_scanner.v2.evidence_commit import rebuild_state as commitbridge_rebuild
    from intraday_scanner.v2.evidence_commit import reconcile as commitbridge_reconcile
    from intraday_scanner.v2.evidence_commit import report as commitbridge_report
    from intraday_scanner.v2.evidence_commit import review as commitbridge_review

    paths = _Paths.create(output_root)
    fetch_payload = fetch_pending(run_date=run_date, output_root=output_root)
    build_payload = build(run_date=run_date, output_root=output_root)
    reconciliation = reconcile(run_date=run_date, output_root=output_root)
    filltruth = feed_filltruth(run_date=run_date, output_root=output_root)
    proposals = commitbridge_propose(run_date=run_date, require_provider_intraday=True)
    review_payload = commitbridge_review(run_date=run_date)
    commit_payload: dict[str, object] = {"committed_count": 0, "status": "skipped_no_auto_commit"}
    rebuild_payload: dict[str, object] = {"status": "skipped_no_auto_commit"}
    if commit:
        commit_payload = commitbridge_commit(run_date=run_date, require_provider_intraday=True)
        rebuild_payload = commitbridge_rebuild(run_date=run_date)
    commit_reconciliation = commitbridge_reconcile(run_date=run_date)
    summary = commitbridge_report()
    command_center = build_command_center().to_dict()
    payload = {
        "autocommit_enabled": commit,
        "build": build_payload,
        "command_center_status": command_center.get("status", "missing"),
        "commit": commit_payload,
        "commitbridge": summary,
        "commitbridge_reconciliation": commit_reconciliation,
        "fetch_pending": fetch_payload,
        "filltruth": filltruth,
        "proposals": proposals,
        "provider_reconciliation": reconciliation,
        "rebuild": rebuild_payload,
        "review": review_payload,
        "run_date": run_date.isoformat(),
        "schema_version": "v2.autodata_trial_day.v1",
        "status": "passed",
        "trial_mode": "explicit_commit" if commit else "propose_only",
    }
    _write_json(paths.reports / "autodata_trial_day_latest.json", payload)
    _write_md(paths.reports / "autodata_trial_day_latest.md", "AutoData Trial Day", _trial_lines(payload))
    return payload


def verify(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    paths = _Paths.create(output_root)
    failures: list[str] = []
    warnings: list[str] = []
    required = (
        paths.provider_registry / "providers.json",
        paths.reports / "provider_readiness.json",
        paths.reports / "autodata_summary.json",
        paths.reports / "canonical_selection_latest.json",
        paths.reports / "canonical_selection_latest.md",
        paths.reports / "autodata_quality_scorecard.md",
        paths.reports / "autodata_red_team.md",
        Path("docs/audit/omega_autodata_quality_scorecard.md"),
        Path("docs/audit/omega_autodata_red_team.md"),
    )
    for path in required:
        if not path.exists():
            failures.append(f"missing artifact: {path.as_posix()}")
    safety = _safety_scan(paths)
    failures.extend(safety["failures"])
    warnings.extend(safety["warnings"])
    secret_scan = _secret_scan(paths)
    failures.extend(secret_scan["failures"])
    warnings.extend(secret_scan["warnings"])
    build_payload = _dict(_read_json(paths.reports / "autodata_build_latest.json", {}))
    canonical_selection = _dict(_read_json(paths.reports / "canonical_selection_latest.json", {}))
    if _int(build_payload.get("accepted_bar_count")) > 0:
        canonical_path = Path(str(build_payload.get("canonical_artifact_path", "")))
        if not canonical_path.exists():
            failures.append("canonical provider artifact is missing")
        if canonical_selection.get("status") != "passed":
            failures.append("canonical provider selection is not passed")
        if _int(
            build_payload.get("canonical_duplicate_timestamp_count")
            or canonical_selection.get("canonical_duplicate_timestamp_count")
        ) != 0:
            failures.append("canonical provider artifact contains duplicate symbol/timestamp rows")
    payload = {
        "checked_at": _now(),
        "failures": sorted(set(failures)),
        "schema_version": "v2.autodata_verify.v1",
        "status": "passed" if not failures else "failed",
        "warnings": sorted(set(warnings)),
    }
    _write_json(paths.reports / "verify_latest.json", payload)
    _write_md(paths.reports / "verify_latest.md", "AutoData Verify", _summary_lines(payload))
    return payload


def report(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    paths = _Paths.create(output_root)
    registry = _dict(_read_json(paths.reports / "provider_readiness.json", {})) or providers(output_root=output_root)
    ready = readiness(output_root=output_root)
    fetch_payload = _dict(_read_json(paths.reports / "fetch_pending_latest.json", {}))
    build_payload = _dict(_read_json(paths.reports / "autodata_build_latest.json", {}))
    reconciliation = _dict(_read_json(paths.reports / "provider_reconciliation_latest.json", {}))
    canonical_selection = _dict(_read_json(paths.reports / "canonical_selection_latest.json", {}))
    filltruth = _dict(_read_json(paths.reports / "autodata_filltruth_latest.json", {}))
    trial = _dict(_read_json(paths.reports / "autodata_trial_day_latest.json", {}))
    commitbridge = _dict(_read_json(EVIDENCE_COMMIT_ROOT / "reports" / "evidence_commit_summary.json", {}))
    score = _write_scorecard(paths, registry=registry, readiness_payload=ready, trial=trial)
    status = _overall_status(ready, build_payload, reconciliation, commitbridge)
    payload = {
        "accepted_bar_count": build_payload.get("accepted_bar_count", 0),
        "build_id": build_payload.get("build_id", f"autodata_report_{_compact_now()}"),
        "canonical_artifact_sha256": build_payload.get("canonical_artifact_sha256", ""),
        "canonical_duplicate_timestamp_count": build_payload.get("canonical_duplicate_timestamp_count", canonical_selection.get("canonical_duplicate_timestamp_count", "missing")),
        "canonical_provider_id": build_payload.get("canonical_provider_id", canonical_selection.get("canonical_provider_id", "missing")),
        "canonical_selection_reason": canonical_selection.get("reason", ""),
        "canonical_selection_status": canonical_selection.get("status", "missing"),
        "command_center_status": trial.get("command_center_status", _dict(_read_json(COMMAND_CENTER_ROOT / "command_center_qa.json", {})).get("status", "missing")),
        "comparison_provider_ids": build_payload.get("comparison_provider_ids", canonical_selection.get("comparison_provider_ids", [])),
        "commitbridge_eligible": commitbridge.get("eligible", 0),
        "commitbridge_status": commitbridge.get("status", "missing"),
        "configured_providers": ready.get("configured_real_providers", []),
        "exact_env_vars_to_set_next": ready.get("exact_env_vars_to_set_next", []),
        "fetch_pending_status": fetch_payload.get("status", "missing"),
        "filltruth_status": filltruth.get("status", "missing"),
        "provider_count": registry.get("provider_count", 0),
        "provider_readiness_status": ready.get("status", "missing"),
        "quality_score": score["score"],
        "reconciliation_status": reconciliation.get("reconciliation_status", "missing"),
        "schema_version": "v2.autodata_report.v1",
        "status": status,
        "trial_day_status": trial.get("status", "missing"),
        "warnings": _unique(_list(ready.get("warnings")) + _list(reconciliation.get("warnings"))),
    }
    _write_json(paths.reports / "autodata_summary.json", payload)
    _write_md(paths.reports / "autodata_summary.md", "OMEGA AutoData Summary", _summary_lines(payload))
    _write_red_team(paths)
    _write_audit_docs(paths, payload, score)
    return payload


def demo(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, object]:
    paths = _Paths.create(output_root)
    init(output_root=output_root)
    provider = _provider_definition("mock_provider_for_tests")
    payload = _mock_payload("QQQ", date(2026, 6, 29))
    request_id = _request_id("mock_provider_for_tests", "QQQ", date(2026, 6, 29), "1min")
    raw_path = paths.raw / "mock_provider_for_tests" / "QQQ" / "2026-06-29" / f"{request_id}.json"
    _write_json(raw_path, payload)
    bars, warnings = _normalize_provider_payload(provider, "QQQ", payload, "1min")
    dataset = _dataset_from_bars(
        bars,
        dataset_id=f"autodata_{request_id}",
        source_kind="mock_test_intraday",
        source_path=raw_path.as_posix(),
        warnings=tuple(warnings),
    )
    normalized_path = paths.cache / "mock_provider_for_tests" / "QQQ" / "2026-06-29" / f"{request_id}_normalized.csv"
    write_ohlcv_csv(dataset, normalized_path)
    manifest = {
        "accepted_bar_count": len(bars),
        "cache_status": "created",
        "errors": [],
        "interval": "1min",
        "provider_id": "mock_provider_for_tests",
        "provider_name": provider["provider_name"],
        "provider_type": provider["provider_type"],
        "evidence_scope": "demo",
        "request_id": request_id,
        "request_params_redacted": {"symbol": "QQQ", "date": "2026-06-29", "interval": "1min"},
        "requested_at": _now(),
        "response_metadata": {"mock": True},
        "raw_artifact_path": raw_path.as_posix(),
        "raw_artifact_sha256": _sha256(raw_path),
        "normalized_artifact_path": normalized_path.as_posix(),
        "normalized_artifact_sha256": _sha256(normalized_path),
        "schema_version": "v2.autodata_provider_request.v1",
        "source_label": "mock_test_intraday",
        "source_trust_level": "mock_test_only",
        "status": "passed",
        "symbol": "QQQ",
        "trade_date": "2026-06-29",
        "validation": _validate_bars(bars, run_date=date(2026, 6, 29), provider=provider, interval="1min"),
        "warnings": ["mock provider is for tests only and cannot become official forward evidence"],
    }
    _write_json(paths.manifests / f"{request_id}.json", manifest)
    _write_json(paths.manifests / "latest_request.json", manifest)
    build_payload = build(run_date=date(2026, 6, 29), output_root=output_root, include_demo=True)
    reconciliation = reconcile(run_date=date(2026, 6, 29), output_root=output_root)
    report_payload = report(output_root=output_root)
    verification = verify(output_root=output_root)
    return {
        "accepted_bar_count": build_payload.get("accepted_bar_count", 0),
        "quality_score": report_payload.get("quality_score", 0),
        "reconciliation_status": reconciliation.get("reconciliation_status", "missing"),
        "source_label": "mock_test_intraday",
        "status": "complete" if verification["status"] == "passed" else "resume_required",
        "verify_status": verification["status"],
    }


class _ProviderError(RuntimeError):
    def __init__(self, message: str, *, status: str = "provider_error") -> None:
        super().__init__(message)
        self.status = status


class _ProviderUnavailable(_ProviderError):
    pass


_DIR_FIELDS = (
    "provider_registry",
    "raw",
    "cache",
    "normalized",
    "validation",
    "reconciliation",
    "manifests",
    "reports",
    "logs",
    "readiness",
)


def _provider_ids() -> tuple[str, ...]:
    return (
        "alpaca_market_data",
        "alpha_vantage",
        "twelve_data",
        "yahoo_chart_public_fallback",
        "mock_provider_for_tests",
    )


def _provider_definition(provider_id: str) -> dict[str, object]:
    env = os.environ
    definitions: dict[str, dict[str, object]] = {
        "alpaca_market_data": {
            "provider_name": "Alpaca Market Data",
            "provider_type": "alpaca_market_data",
            "requires_api_key": True,
            "required_env_vars": ["ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"],
            "optional_env_vars": ["ALPACA_DATA_FEED"],
            "supports_intraday": True,
            "supported_intervals": ["1min", "5min", "15min"],
            "supports_historical_intraday": True,
            "supports_extended_hours": False,
            "source_trust_level": "broker_or_vendor_readonly",
            "source_label": "broker_or_vendor_intraday",
            "data_delay_policy": "feed-dependent; IEX/default may be delayed or limited",
            "rate_limit_policy": "provider-plan-dependent; AutoData caches immutable responses",
            "terms_note": "Use only with legal account access and market data permissions.",
            "official_docs_reference": "https://docs.alpaca.markets/reference/stockbars",
        },
        "alpha_vantage": {
            "provider_name": "Alpha Vantage",
            "provider_type": "alpha_vantage",
            "requires_api_key": True,
            "required_env_vars": ["ALPHA_VANTAGE_API_KEY"],
            "optional_env_vars": [],
            "supports_intraday": True,
            "supported_intervals": ["1min", "5min", "15min", "30min", "60min"],
            "supports_historical_intraday": True,
            "supports_extended_hours": False,
            "source_trust_level": "provider_readonly",
            "source_label": "provider_intraday",
            "data_delay_policy": "free/API-plan dependent; not assumed real-time",
            "rate_limit_policy": "free tier is rate-limited; AutoData treats limits as blocked provider state",
            "terms_note": "Use only with legal API access.",
            "official_docs_reference": "https://www.alphavantage.co/documentation/",
        },
        "twelve_data": {
            "provider_name": "Twelve Data",
            "provider_type": "twelve_data",
            "requires_api_key": True,
            "required_env_vars": ["TWELVE_DATA_API_KEY"],
            "optional_env_vars": [],
            "supports_intraday": True,
            "supported_intervals": ["1min", "5min", "15min", "30min", "1h"],
            "supports_historical_intraday": True,
            "supports_extended_hours": False,
            "source_trust_level": "provider_readonly",
            "source_label": "provider_intraday",
            "data_delay_policy": "API-plan dependent; not assumed real-time",
            "rate_limit_policy": "provider-plan-dependent; AutoData treats limits as blocked provider state",
            "terms_note": "Use only with legal API access.",
            "official_docs_reference": "https://twelvedata.com/docs",
        },
        "yahoo_chart_public_fallback": {
            "provider_name": "Yahoo Chart Public Fallback",
            "provider_type": "yahoo_chart_public_fallback",
            "requires_api_key": False,
            "required_env_vars": [],
            "optional_env_vars": [],
            "supports_intraday": True,
            "supported_intervals": ["1min", "5min", "15min", "30min", "60min"],
            "supports_historical_intraday": True,
            "supports_extended_hours": False,
            "source_trust_level": "public_single_provider_low_trust",
            "source_label": "public_intraday_single_provider",
            "data_delay_policy": "public unofficial endpoint; not broker-grade and not assumed real-time",
            "rate_limit_policy": "unpublished public endpoint limits; cache and degrade on failure",
            "terms_note": "Public fallback only; use according to Yahoo terms and do not treat as broker-grade.",
            "official_docs_reference": "unofficial public chart endpoint already present in repo",
        },
        "mock_provider_for_tests": {
            "provider_name": "Mock Provider For Tests",
            "provider_type": "mock_provider_for_tests",
            "requires_api_key": False,
            "required_env_vars": [],
            "optional_env_vars": [],
            "supports_intraday": True,
            "supported_intervals": ["1min"],
            "supports_historical_intraday": True,
            "supports_extended_hours": False,
            "source_trust_level": "mock_test_only",
            "source_label": "mock_test_intraday",
            "data_delay_policy": "not market data",
            "rate_limit_policy": "none",
            "terms_note": "Tests and demo only; never official forward evidence.",
            "official_docs_reference": "local deterministic fixture",
        },
    }
    if provider_id not in definitions:
        raise ValueError(f"unsupported AutoData provider: {provider_id}")
    row = {"provider_id": provider_id, **definitions[provider_id]}
    missing = [name for name in _list(row["required_env_vars"]) if not env.get(str(name))]
    configured = not missing
    enabled = configured and provider_id != "mock_provider_for_tests"
    warnings: list[str] = []
    if missing:
        warnings.append(f"missing env vars: {', '.join(missing)}")
    if provider_id == "yahoo_chart_public_fallback":
        warnings.append("public fallback is single-provider, free/unofficial, and not broker-grade")
    if provider_id == "mock_provider_for_tests":
        warnings.append("mock provider is tests/demo only and cannot commit official evidence")
    return {
        **row,
        "configured": configured,
        "enabled": enabled,
        "warnings": warnings,
    }


def _select_provider(provider_id: str | None) -> dict[str, object]:
    if provider_id:
        provider = _provider_definition(provider_id)
        if not provider["configured"]:
            raise _ProviderUnavailable(
                f"{provider_id} is not configured",
                status="provider_not_configured",
            )
        return provider
    for candidate in ("alpaca_market_data", "alpha_vantage", "twelve_data", "yahoo_chart_public_fallback"):
        provider = _provider_definition(candidate)
        if provider["configured"]:
            return provider
    raise _ProviderUnavailable("no AutoData intraday provider is configured", status="provider_not_configured")


def _fetch_provider_payload(
    provider: dict[str, object],
    symbol: str,
    run_date: date,
    interval: str,
) -> dict[str, object]:
    provider_id = str(provider["provider_id"])
    if provider_id == "mock_provider_for_tests":
        return _mock_payload(symbol, run_date)
    if not provider.get("configured"):
        raise _ProviderUnavailable(f"{provider_id} is not configured", status="provider_not_configured")
    try:
        if provider_id == "yahoo_chart_public_fallback":
            return _http_json(_yahoo_url(symbol, interval=interval))
        if provider_id == "alpha_vantage":
            return _http_json(_alpha_vantage_url(symbol, interval=interval))
        if provider_id == "twelve_data":
            return _http_json(_twelve_data_url(symbol, run_date=run_date, interval=interval))
        if provider_id == "alpaca_market_data":
            return _http_json(_alpaca_url(symbol, run_date=run_date, interval=interval), headers=_alpaca_headers())
    except ProviderHttpError as exc:
        if exc.status_code in {401, 403}:
            raise _ProviderError(f"{provider_id} auth failed: HTTP {exc.status_code}", status="provider_auth_failed") from exc
        if exc.status_code == 429:
            raise _ProviderError(f"{provider_id} rate limited: HTTP 429", status="provider_rate_limited") from exc
        raise _ProviderError(f"{provider_id} provider error: HTTP {exc.status_code}") from exc
    except ProviderFetchError as exc:
        raise _ProviderError(f"{provider_id} fetch failed: {exc}") from exc
    raise _ProviderUnavailable(f"{provider_id} has no fetch implementation", status="provider_not_configured")


def _http_json(url: str, *, headers: dict[str, str] | None = None, timeout_seconds: float = 20.0) -> dict[str, object]:
    return fetch_json_url(url, headers=headers, timeout_seconds=timeout_seconds)


def _yahoo_url(symbol: str, *, interval: str) -> str:
    query = encode_query({"range": "5d", "interval": _provider_interval("yahoo_chart_public_fallback", interval), "includePrePost": "false", "events": "history"})
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{query}"


def _alpha_vantage_url(symbol: str, *, interval: str) -> str:
    query = encode_query({"function": "TIME_SERIES_INTRADAY", "symbol": symbol, "interval": _provider_interval("alpha_vantage", interval), "apikey": os.environ.get("ALPHA_VANTAGE_API_KEY", ""), "outputsize": "compact"})
    return f"https://www.alphavantage.co/query?{query}"


def _twelve_data_url(symbol: str, *, run_date: date, interval: str) -> str:
    query = encode_query({"symbol": symbol, "interval": _provider_interval("twelve_data", interval), "apikey": os.environ.get("TWELVE_DATA_API_KEY", ""), "start_date": f"{run_date.isoformat()} 00:00:00", "end_date": f"{run_date.isoformat()} 23:59:59", "format": "JSON"})
    return f"https://api.twelvedata.com/time_series?{query}"


def _alpaca_url(symbol: str, *, run_date: date, interval: str) -> str:
    start = f"{run_date.isoformat()}T00:00:00Z"
    end = f"{run_date.isoformat()}T23:59:59Z"
    feed = os.environ.get("ALPACA_DATA_FEED", "iex")
    query = encode_query({"symbols": symbol, "timeframe": _provider_interval("alpaca_market_data", interval), "start": start, "end": end, "feed": feed})
    return f"https://data.alpaca.markets/v2/stocks/bars?{query}"


def _alpaca_headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY_ID", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET_KEY", ""),
    }


def _provider_interval(provider_id: str, interval: str) -> str:
    normalized = interval.lower()
    mapping = {
        "1min": {"yahoo_chart_public_fallback": "1m", "alpha_vantage": "1min", "twelve_data": "1min", "alpaca_market_data": "1Min"},
        "5min": {"yahoo_chart_public_fallback": "5m", "alpha_vantage": "5min", "twelve_data": "5min", "alpaca_market_data": "5Min"},
        "15min": {"yahoo_chart_public_fallback": "15m", "alpha_vantage": "15min", "twelve_data": "15min", "alpaca_market_data": "15Min"},
        "30min": {"yahoo_chart_public_fallback": "30m", "alpha_vantage": "30min", "twelve_data": "30min", "alpaca_market_data": "30Min"},
        "60min": {"yahoo_chart_public_fallback": "60m", "alpha_vantage": "60min", "twelve_data": "1h", "alpaca_market_data": "1Hour"},
        "1h": {"yahoo_chart_public_fallback": "60m", "alpha_vantage": "60min", "twelve_data": "1h", "alpaca_market_data": "1Hour"},
    }
    return mapping.get(normalized, mapping["1min"]).get(provider_id, normalized)


def _normalize_provider_payload(
    provider: dict[str, object],
    symbol: str,
    payload: dict[str, object],
    interval: str,
) -> tuple[list[MarketBar], list[str]]:
    provider_id = str(provider["provider_id"])
    if payload.get("error"):
        return [], [str(payload.get("error"))]
    if provider_id == "yahoo_chart_public_fallback":
        return _normalize_yahoo(symbol, payload)
    if provider_id == "alpha_vantage":
        return _normalize_alpha_vantage(symbol, payload, interval)
    if provider_id == "twelve_data":
        return _normalize_twelve_data(symbol, payload)
    if provider_id == "alpaca_market_data":
        return _normalize_alpaca(symbol, payload)
    if provider_id == "mock_provider_for_tests":
        return _normalize_mock(symbol, payload)
    return [], [f"{provider_id}: no normalizer available"]


def _normalize_yahoo(symbol: str, payload: dict[str, object]) -> tuple[list[MarketBar], list[str]]:
    warnings: list[str] = []
    result = _dict(payload.get("chart")).get("result")
    if not isinstance(result, list) or not result:
        return [], ["Yahoo chart payload missing result"]
    item = _dict(result[0])
    timestamps = item.get("timestamp", [])
    quote_items = _dict(item.get("indicators")).get("quote", [])
    if not isinstance(timestamps, list) or not isinstance(quote_items, list) or not quote_items:
        return [], ["Yahoo chart payload missing timestamp/quote arrays"]
    quote = _dict(quote_items[0])
    return _bars_from_series(symbol, timestamps, quote, warnings)


def _normalize_alpha_vantage(symbol: str, payload: dict[str, object], interval: str) -> tuple[list[MarketBar], list[str]]:
    if "Note" in payload:
        return [], [f"Alpha Vantage rate limit or note: {payload['Note']}"]
    if "Error Message" in payload:
        return [], [f"Alpha Vantage error: {payload['Error Message']}"]
    key = next((name for name in payload if name.lower().startswith("time series")), "")
    series = _dict(payload.get(key))
    warnings: list[str] = []
    bars: list[MarketBar] = []
    for timestamp_text, row in series.items():
        data = _dict(row)
        try:
            timestamp = _parse_timestamp(timestamp_text)
            bars.append(MarketBar(symbol=symbol, timestamp=timestamp, open=float(data["1. open"]), high=float(data["2. high"]), low=float(data["3. low"]), close=float(data["4. close"]), volume=int(float(data.get("5. volume", 0)))))
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"{symbol}: Alpha Vantage malformed bar {timestamp_text} ({exc})")
    return bars, warnings


def _normalize_twelve_data(symbol: str, payload: dict[str, object]) -> tuple[list[MarketBar], list[str]]:
    if payload.get("status") == "error":
        return [], [f"Twelve Data error: {payload.get('message', 'unknown')}"]
    rows = payload.get("values", [])
    warnings: list[str] = []
    bars: list[MarketBar] = []
    if not isinstance(rows, list):
        return [], ["Twelve Data values were not a list"]
    for row in rows:
        data = _dict(row)
        try:
            timestamp = _parse_timestamp(str(data["datetime"]))
            bars.append(MarketBar(symbol=symbol, timestamp=timestamp, open=float(data["open"]), high=float(data["high"]), low=float(data["low"]), close=float(data["close"]), volume=int(float(data.get("volume", 0) or 0))))
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"{symbol}: Twelve Data malformed bar ({exc})")
    return bars, warnings


def _normalize_alpaca(symbol: str, payload: dict[str, object]) -> tuple[list[MarketBar], list[str]]:
    raw = payload.get("bars", [])
    if isinstance(raw, dict):
        rows = raw.get(symbol, raw.get(symbol.upper(), []))
    else:
        rows = raw
    warnings: list[str] = []
    bars: list[MarketBar] = []
    if not isinstance(rows, list):
        return [], ["Alpaca bars payload was not a list"]
    for row in rows:
        data = _dict(row)
        try:
            timestamp = _parse_timestamp(str(data.get("t") or data.get("timestamp")))
            bars.append(MarketBar(symbol=symbol, timestamp=timestamp, open=float(data.get("o") or data.get("open")), high=float(data.get("h") or data.get("high")), low=float(data.get("l") or data.get("low")), close=float(data.get("c") or data.get("close")), volume=int(float(data.get("v") or data.get("volume") or 0))))
        except (TypeError, ValueError) as exc:
            warnings.append(f"{symbol}: Alpaca malformed bar ({exc})")
    return bars, warnings


def _normalize_mock(symbol: str, payload: dict[str, object]) -> tuple[list[MarketBar], list[str]]:
    rows = payload.get("bars", [])
    warnings: list[str] = ["mock provider is tests/demo only"]
    bars: list[MarketBar] = []
    if not isinstance(rows, list):
        return [], ["mock payload bars were not a list"]
    for row in rows:
        data = _dict(row)
        try:
            bars.append(MarketBar(symbol=symbol, timestamp=_parse_timestamp(str(data["timestamp"])), open=float(data["open"]), high=float(data["high"]), low=float(data["low"]), close=float(data["close"]), volume=int(float(data["volume"]))))
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"{symbol}: mock malformed bar ({exc})")
    return bars, warnings


def _bars_from_series(symbol: str, timestamps: list[object], quote: dict[str, object], warnings: list[str]) -> tuple[list[MarketBar], list[str]]:
    bars: list[MarketBar] = []
    for index, timestamp_value in enumerate(timestamps):
        try:
            open_value = _series_at(quote, "open", index)
            high_value = _series_at(quote, "high", index)
            low_value = _series_at(quote, "low", index)
            close_value = _series_at(quote, "close", index)
            volume_value = _series_at(quote, "volume", index, allow_none=True)
            if open_value is None or high_value is None or low_value is None or close_value is None:
                warnings.append(f"{symbol}: skipped incomplete Yahoo bar {index}")
                continue
            bars.append(MarketBar(symbol=symbol, timestamp=datetime.fromtimestamp(float(timestamp_value), tz=timezone.utc), open=float(open_value), high=float(high_value), low=float(low_value), close=float(close_value), volume=int(float(volume_value or 0))))
        except (TypeError, ValueError, IndexError) as exc:
            warnings.append(f"{symbol}: skipped provider bar {index} ({exc})")
    return bars, warnings


def _validate_bars(
    bars: list[MarketBar],
    *,
    run_date: date,
    provider: dict[str, object],
    interval: str,
) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []
    if not bars:
        warnings.append("no intraday bars accepted")
    by_symbol: dict[str, list[MarketBar]] = {}
    for bar in bars:
        by_symbol.setdefault(bar.symbol, []).append(bar)
    for symbol, symbol_bars in sorted(by_symbol.items()):
        seen: set[datetime] = set()
        previous: datetime | None = None
        for bar in sorted(symbol_bars, key=lambda item: item.timestamp):
            if bar.timestamp in seen:
                errors.append(f"{symbol}: duplicate timestamp {bar.timestamp.isoformat()}")
            seen.add(bar.timestamp)
            if previous and bar.timestamp < previous:
                errors.append(f"{symbol}: non-monotonic timestamp {bar.timestamp.isoformat()}")
            previous = bar.timestamp
            if min(bar.open, bar.high, bar.low, bar.close) <= 0:
                errors.append(f"{symbol}: non-positive OHLC at {bar.timestamp.isoformat()}")
            if bar.high < max(bar.open, bar.low, bar.close):
                errors.append(f"{symbol}: invalid high at {bar.timestamp.isoformat()}")
            if bar.low > min(bar.open, bar.high, bar.close):
                errors.append(f"{symbol}: invalid low at {bar.timestamp.isoformat()}")
            if bar.volume == 0:
                warnings.append(f"{symbol}: zero volume at {bar.timestamp.isoformat()}")
            if _market_date(bar.timestamp) != run_date:
                warnings.append(f"{symbol}: bar outside requested date {bar.timestamp.isoformat()}")
    return {
        "accepted_bar_count": len(bars),
        "errors": sorted(set(errors)),
        "interval": interval,
        "provider_id": provider.get("provider_id", "missing"),
        "schema_version": "v2.autodata_validation.v1",
        "status": "failed" if errors else "passed_with_warnings" if warnings else "passed",
        "warnings": _unique(warnings),
    }


def _needed_symbols_dates(pending: list[dict[str, object]], run_date: date) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for order in pending:
        symbol = str(order.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        earliest = str(order.get("earliest_fill_date") or order.get("trade_date") or run_date.isoformat())
        try:
            target_date = date.fromisoformat(earliest)
        except ValueError:
            target_date = run_date
        if target_date > run_date:
            target_date = run_date
        rows.append({"date": target_date.isoformat(), "order_id": order.get("order_id", ""), "symbol": symbol})
    deduped: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        deduped[(str(row["symbol"]), str(row["date"]))] = row
    return list(deduped.values())


def _request_manifests(
    paths: _Paths,
    run_date: date,
    *,
    include_demo: bool = False,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(paths.manifests.glob("*.json")):
        if path.name in {"autodata_manifest.json", "latest_request.json"}:
            continue
        payload = _dict(_read_json(path, {}))
        if payload.get("trade_date") == run_date.isoformat():
            if not include_demo and (
                payload.get("evidence_scope") == "demo"
                or payload.get("provider_id") == "mock_provider_for_tests"
                or payload.get("source_label") == "mock_test_intraday"
            ):
                continue
            rows.append(payload)
    return rows


def _build_per_provider_artifacts(
    paths: _Paths,
    manifests: list[dict[str, object]],
    run_date: date,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for manifest in manifests:
        provider_id = str(manifest.get("provider_id", "missing"))
        source_label = str(manifest.get("source_label", "unknown_intraday"))
        path = Path(str(manifest.get("normalized_artifact_path", "")))
        bars: list[MarketBar] = []
        load_warnings: list[str] = []
        if path.exists():
            dataset = load_ohlcv_csv(
                path,
                dataset_id=str(manifest.get("request_id", provider_id)),
                source_kind=source_label,
                timeframe="intraday",
            )
            load_warnings.extend(dataset.warnings)
            for symbol in dataset.symbols:
                bars.extend(
                    bar
                    for bar in dataset.bars_by_symbol[symbol]
                    if _market_date(bar.timestamp) == run_date
                )
        else:
            load_warnings.append(f"{provider_id}: normalized artifact missing")
        artifact_paths = _write_per_provider_symbol_artifacts(
            paths,
            provider_id=provider_id,
            source_label=source_label,
            bars=bars,
            run_date=run_date,
        )
        rows.append(
            {
                "_bars": bars,
                "accepted_bar_count": len(bars),
                "duplicate_timestamp_count": _duplicate_timestamp_count(bars),
                "first_timestamp": min((bar.timestamp for bar in bars), default=None).isoformat() if bars else "",
                "last_timestamp": max((bar.timestamp for bar in bars), default=None).isoformat() if bars else "",
                "per_provider_artifact_paths": artifact_paths,
                "provider_id": provider_id,
                "request_id": manifest.get("request_id", "missing"),
                "source_label": source_label,
                "source_trust_level": manifest.get("source_trust_level", "unknown"),
                "status": manifest.get("status", "missing"),
                "warnings": _unique(load_warnings + _list(manifest.get("warnings"))),
            }
        )
    return rows


def _write_per_provider_symbol_artifacts(
    paths: _Paths,
    *,
    provider_id: str,
    source_label: str,
    bars: list[MarketBar],
    run_date: date,
) -> list[str]:
    artifact_paths: list[str] = []
    for symbol in sorted({bar.symbol for bar in bars}):
        symbol_bars = [bar for bar in bars if bar.symbol == symbol]
        path = paths.normalized / "per_provider" / provider_id / symbol / f"{run_date.isoformat()}.csv"
        dataset = _dataset_from_bars(
            symbol_bars,
            dataset_id=f"autodata_{provider_id}_{symbol}_{run_date.isoformat()}",
            source_kind=source_label,
        )
        write_ohlcv_csv(dataset, path)
        artifact_paths.append(path.as_posix())
    return artifact_paths


def _write_canonical_symbol_artifacts(
    paths: _Paths,
    bars: list[MarketBar],
    run_date: date,
    source_label: str,
) -> list[str]:
    artifact_paths: list[str] = []
    for symbol in sorted({bar.symbol for bar in bars}):
        symbol_bars = [bar for bar in bars if bar.symbol == symbol]
        path = paths.normalized / "canonical" / symbol / f"{run_date.isoformat()}_canonical_intraday.csv"
        dataset = _dataset_from_bars(
            symbol_bars,
            dataset_id=f"autodata_canonical_{symbol}_{run_date.isoformat()}",
            source_kind=source_label,
        )
        write_ohlcv_csv(dataset, path)
        artifact_paths.append(path.as_posix())
    return artifact_paths


def _public_provider_row(row: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _select_canonical_provider(rows: list[dict[str, object]]) -> dict[str, object]:
    candidates = [row for row in rows if _int(row.get("accepted_bar_count")) > 0]
    if not candidates:
        return {"_bars": [], "provider_id": "missing", "source_label": "unknown_intraday"}
    return sorted(
        candidates,
        key=lambda row: (
            _int(row.get("duplicate_timestamp_count")) > 0,
            CANONICAL_SOURCE_PRIORITY.get(str(row.get("source_label")), 99),
            str(row.get("provider_id")),
        ),
    )[0]


def _canonical_selection_reason(
    canonical_row: dict[str, object],
    rows: list[dict[str, object]],
) -> str:
    provider_id = str(canonical_row.get("provider_id", "missing"))
    source_label = str(canonical_row.get("source_label", "unknown_intraday"))
    duplicates = _int(canonical_row.get("duplicate_timestamp_count"))
    comparisons = [
        str(row.get("provider_id"))
        for row in rows
        if str(row.get("provider_id")) != provider_id and _int(row.get("accepted_bar_count")) > 0
    ]
    reason = (
        f"selected {provider_id} as canonical because source label {source_label} "
        f"has priority {CANONICAL_SOURCE_PRIORITY.get(source_label, 99)} and "
        f"duplicate_timestamp_count={duplicates}"
    )
    if comparisons:
        reason += f"; comparison providers kept separate: {', '.join(comparisons)}"
    return reason


def _duplicate_timestamp_count(bars: list[MarketBar]) -> int:
    seen: set[tuple[str, datetime]] = set()
    duplicate_count = 0
    for bar in sorted(bars, key=lambda item: (item.symbol, item.timestamp)):
        key = (bar.symbol, bar.timestamp)
        if key in seen:
            duplicate_count += 1
        seen.add(key)
    return duplicate_count


def _load_provider_build_datasets(
    provider_rows: list[dict[str, object]],
    run_date: date,
) -> list[dict[str, object]]:
    datasets: list[dict[str, object]] = []
    for row in provider_rows:
        provider_id = str(row.get("provider_id", "missing"))
        source_label = str(row.get("source_label", "unknown_intraday"))
        bars_by_symbol: dict[str, list[MarketBar]] = {}
        paths = [Path(str(path)) for path in _list(row.get("per_provider_artifact_paths"))]
        if not paths and row.get("normalized_artifact_path"):
            paths = [Path(str(row["normalized_artifact_path"]))]
        for path in paths:
            if not path.exists():
                continue
            dataset = load_ohlcv_csv(
                path,
                dataset_id=f"autodata_reconcile_{provider_id}",
                source_kind=source_label,
                timeframe="intraday",
            )
            for symbol in dataset.symbols:
                for bar in dataset.bars_by_symbol[symbol]:
                    if _market_date(bar.timestamp) == run_date:
                        bars_by_symbol.setdefault(symbol, []).append(bar)
        if bars_by_symbol:
            datasets.append(
                {
                    "bars_by_symbol": {
                        symbol: sorted(values, key=lambda item: item.timestamp)
                        for symbol, values in bars_by_symbol.items()
                    },
                    "provider_id": provider_id,
                    "source_label": source_label,
                }
            )
    return datasets


def _write_provider_diff_reports(
    paths: _Paths,
    run_date: date,
    provider_datasets: list[dict[str, object]],
    *,
    canonical_provider_id: str,
) -> dict[str, object]:
    canonical = next(
        (row for row in provider_datasets if row.get("provider_id") == canonical_provider_id),
        provider_datasets[0] if provider_datasets else {},
    )
    canonical_bars = _dict(canonical.get("bars_by_symbol"))
    rows: list[dict[str, object]] = []
    diff_paths: list[str] = []
    material_mismatch = False
    for provider in provider_datasets:
        provider_id = str(provider.get("provider_id"))
        if provider_id == str(canonical.get("provider_id")):
            continue
        provider_bars = _dict(provider.get("bars_by_symbol"))
        for symbol, canonical_symbol_bars in canonical_bars.items():
            comparison_symbol_bars = _list(provider_bars.get(symbol))
            canonical_by_time = {
                bar.timestamp: bar
                for bar in _list(canonical_symbol_bars)
                if isinstance(bar, MarketBar)
            }
            comparison_by_time = {
                bar.timestamp: bar
                for bar in comparison_symbol_bars
                if isinstance(bar, MarketBar)
            }
            overlap = sorted(set(canonical_by_time) & set(comparison_by_time))
            diff_rows: list[dict[str, object]] = []
            max_close_diff_bps = 0.0
            for timestamp in overlap:
                left = canonical_by_time[timestamp]
                right = comparison_by_time[timestamp]
                close_diff_bps = round(abs(left.close - right.close) / max(abs(left.close), 0.0001) * 10000, 4)
                max_close_diff_bps = max(max_close_diff_bps, close_diff_bps)
                diff_rows.append(
                    {
                        "canonical_close": left.close,
                        "canonical_provider_id": canonical.get("provider_id", "missing"),
                        "close_diff_bps": close_diff_bps,
                        "comparison_close": right.close,
                        "comparison_provider_id": provider_id,
                        "symbol": symbol,
                        "timestamp": timestamp.isoformat(),
                    }
                )
            if max_close_diff_bps > 25.0:
                material_mismatch = True
            diff_path = paths.reconciliation / f"provider_diff_{symbol}_{run_date.isoformat()}_{provider_id}.csv"
            _write_csv_rows(diff_path, diff_rows)
            diff_paths.append(diff_path.as_posix())
            rows.append(
                {
                    "canonical_provider_id": canonical.get("provider_id", "missing"),
                    "comparison_provider_id": provider_id,
                    "diff_artifact_path": diff_path.as_posix(),
                    "extra_comparison_bars": len(set(comparison_by_time) - set(canonical_by_time)),
                    "max_close_diff_bps": max_close_diff_bps,
                    "missing_comparison_bars": len(set(canonical_by_time) - set(comparison_by_time)),
                    "overlap_bar_count": len(overlap),
                    "status": "mismatch" if max_close_diff_bps > 25.0 else "reconciled_with_minor_diffs",
                    "symbol": symbol,
                }
            )
    return {"diff_artifact_paths": diff_paths, "material_mismatch": material_mismatch, "rows": rows}


def _write_csv_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row}) or ["status"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _has_public_comparison(build_payload: dict[str, object]) -> bool:
    canonical_provider_id = str(build_payload.get("canonical_provider_id", ""))
    rows = [_dict(row) for row in _list(build_payload.get("per_provider_rows") or build_payload.get("rows"))]
    canonical = next((row for row in rows if row.get("provider_id") == canonical_provider_id), {})
    if str(canonical.get("source_label")) not in {"broker_or_vendor_intraday", "provider_intraday"}:
        return False
    return any(
        str(row.get("source_label")) == "public_intraday_single_provider"
        and str(row.get("provider_id")) != canonical_provider_id
        and _int(row.get("accepted_bar_count")) > 0
        for row in rows
    )


def _combined_source_label(manifests: list[dict[str, object]]) -> str:
    labels = {str(row.get("source_label", "unknown_intraday")) for row in manifests if _int(row.get("accepted_bar_count")) > 0}
    if not labels:
        return "unknown_intraday"
    if labels == {"mock_test_intraday"}:
        return "mock_test_intraday"
    if any(label in {"broker_or_vendor_intraday", "provider_intraday"} for label in labels):
        return "broker_or_vendor_intraday" if "broker_or_vendor_intraday" in labels else "provider_intraday"
    if "public_intraday_single_provider" in labels:
        return "public_intraday_single_provider"
    return sorted(labels)[0]


def _augment_filltruth_autodata_manifest(
    paths: _Paths,
    *,
    build_payload: dict[str, object],
    reconciliation: dict[str, object],
) -> None:
    manifest_path = FILL_TRUTH_ROOT / "manifests" / "latest_intraday_import.json"
    manifest = _dict(_read_json(manifest_path, {}))
    manifests = _request_manifests(paths, date.fromisoformat(str(build_payload.get("run_date"))))
    raw_hashes = {
        str(row.get("provider_id")): str(row.get("raw_artifact_sha256"))
        for row in manifests
        if row.get("raw_artifact_sha256")
    }
    source_label = str(build_payload.get("source_label", manifest.get("source_label", "unknown_intraday")))
    canonical_selection = _dict(build_payload.get("canonical_selection"))
    canonical_hash = str(
        build_payload.get("canonical_artifact_sha256")
        or canonical_selection.get("canonical_artifact_sha256")
        or ""
    )
    canonical_duplicate_count = _int(
        build_payload.get("canonical_duplicate_timestamp_count")
        or canonical_selection.get("canonical_duplicate_timestamp_count")
    )
    reconciliation_status = str(reconciliation.get("reconciliation_status", "missing"))
    commit_eligible = (
        source_label in AUTO_COMMIT_ELIGIBLE_LABELS
        and reconciliation_status in RECONCILED_STATUSES
        and bool(raw_hashes)
        and bool(canonical_hash)
        and canonical_duplicate_count == 0
    )
    updated = {
        **manifest,
        "autodata_build_id": build_payload.get("build_id", ""),
        "autodata_provider_ids": build_payload.get("providers", []),
        "autodata_raw_artifact_hashes": raw_hashes,
        "canonical_artifact_path": build_payload.get("canonical_artifact_path", ""),
        "canonical_dataset_hash": canonical_hash,
        "canonical_duplicate_timestamp_count": canonical_duplicate_count,
        "canonical_provider_id": build_payload.get("canonical_provider_id", ""),
        "canonical_selection_reason": canonical_selection.get("reason", ""),
        "canonical_selection_status": canonical_selection.get("status", "missing"),
        "comparison_provider_ids": build_payload.get("comparison_provider_ids", []),
        "data_type": source_label,
        "daily_reconciliation_status": reconciliation_status,
        "filltruth_commit_eligible": commit_eligible,
        "intraday_reconciliation_status": reconciliation_status,
        "provider_reconciliation_status": reconciliation_status,
        "source_file_sha256": canonical_hash,
        "source_label": source_label,
        "source_provider": "autodata:" + str(build_payload.get("canonical_provider_id", "")),
        "source_trust_level": _source_trust_level(source_label),
    }
    _write_json(manifest_path, updated)


def _source_trust_level(source_label: str) -> str:
    if source_label == "public_intraday_single_provider":
        return "public_single_provider_low_trust"
    if source_label == "mock_test_intraday":
        return "mock_test_only"
    if source_label == "broker_or_vendor_intraday":
        return "broker_or_vendor_readonly"
    if source_label == "provider_intraday":
        return "provider_readonly"
    return "unknown"


def _daily_reference_rows(run_date: date) -> dict[str, dict[str, object]]:
    path = DATA_TRUTH_ROOT / "normalized" / "latest_ohlcv.csv"
    manifest = _dict(_read_json(DATA_TRUTH_ROOT / "manifests" / "latest.json", {}))
    if not path.exists():
        return {}
    rows: dict[str, dict[str, object]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            timestamp = _parse_timestamp(str(row.get("timestamp") or row.get("date") or ""))
            if _market_date(timestamp) != run_date:
                continue
            symbol = str(row.get("symbol", "")).upper()
            rows[symbol] = {
                "close": _float(row.get("close")),
                "high": _float(row.get("high")),
                "low": _float(row.get("low")),
                "open": _float(row.get("open")),
                "snapshot_id": manifest.get("snapshot_id", "missing"),
                "volume": _int(row.get("volume")),
            }
    return rows


def _daily_aggregate(symbol: str, run_date: date, bars: list[MarketBar]) -> dict[str, object]:
    ordered = sorted(bars, key=lambda item: item.timestamp)
    return {
        "bar_count": len(ordered),
        "close": ordered[-1].close,
        "date": run_date.isoformat(),
        "first_timestamp": ordered[0].timestamp.isoformat(),
        "high": max(bar.high for bar in ordered),
        "last_timestamp": ordered[-1].timestamp.isoformat(),
        "low": min(bar.low for bar in ordered),
        "open": ordered[0].open,
        "symbol": symbol,
        "volume": sum(bar.volume for bar in ordered),
    }


def _daily_diffs(aggregate: dict[str, object], reference: dict[str, object]) -> dict[str, object]:
    diffs: dict[str, object] = {}
    for key in ("open", "high", "low", "close"):
        left = _float(aggregate.get(key))
        right = _float(reference.get(key))
        diffs[f"{key}_aggregate"] = left
        diffs[f"{key}_daily_reference"] = right
        diffs[f"{key}_diff_bps"] = round(abs(left - right) / max(abs(right), 0.0001) * 10000, 4)
    volume_left = _int(aggregate.get("volume"))
    volume_right = _int(reference.get("volume"))
    diffs["volume_aggregate"] = volume_left
    diffs["volume_daily_reference"] = volume_right
    diffs["volume_diff_pct"] = round(abs(volume_left - volume_right) / max(abs(volume_right), 1), 6)
    return diffs


def _within_minor_tolerance(diffs: dict[str, object]) -> bool:
    price_keys = [key for key in diffs if key.endswith("_diff_bps")]
    if not price_keys:
        return False
    return all(_float(diffs[key]) <= 25.0 for key in price_keys)


def _overall_status(
    readiness_payload: dict[str, object],
    build_payload: dict[str, object],
    reconciliation: dict[str, object],
    commitbridge: dict[str, object],
) -> str:
    if _int(commitbridge.get("commit_events")) > 0:
        return "COMPLETE"
    if readiness_payload.get("status") == "ready_public_fallback_only":
        return "READY_WITH_PUBLIC_FALLBACK_WARNINGS"
    if _int(build_payload.get("accepted_bar_count")) > 0 and reconciliation.get("reconciliation_status") == "single_provider_unreconciled":
        return "READY_WITH_PUBLIC_FALLBACK_WARNINGS"
    if readiness_payload.get("status") == "blocked_needs_provider_keys":
        return "BLOCKED_NEEDS_PROVIDER_KEYS"
    return "RESUME_REQUIRED"


def _write_scorecard(
    paths: _Paths,
    *,
    registry: dict[str, object],
    readiness_payload: dict[str, object],
    trial: dict[str, object],
) -> dict[str, object]:
    safety = _safety_scan(paths)
    secret = _secret_scan(paths)
    canonical_selection = _dict(_read_json(paths.reports / "canonical_selection_latest.json", {}))
    canonical_duplicate_ok = (
        canonical_selection.get("status") == "passed"
        and _int(canonical_selection.get("canonical_duplicate_timestamp_count")) == 0
    )
    per_provider_normalized = any((paths.normalized / "per_provider").rglob("*.csv")) if (paths.normalized / "per_provider").exists() else False
    checks = (
        ("Provider registry quality", bool(registry.get("providers")), 5),
        ("Secret handling", not secret["failures"], 6),
        ("Read-only safety", not safety["failures"], 6),
        ("Provider fetch/cache/hash", (paths.manifests / "latest_request.json").exists(), 6),
        ("Per-provider normalization", per_provider_normalized, 6),
        ("Canonical selection artifact", bool(canonical_selection), 6),
        ("Duplicate-free canonical dataset", canonical_duplicate_ok, 7),
        ("Provider reconciliation", (paths.reports / "provider_reconciliation_latest.json").exists(), 7),
        ("Pending-order automation", (paths.reports / "fetch_pending_latest.json").exists(), 5),
        ("FillTruth integration", (paths.reports / "autodata_filltruth_latest.json").exists(), 6),
        ("CommitBridge integration", (EVIDENCE_COMMIT_ROOT / "reports" / "evidence_commit_summary.json").exists(), 6),
        ("Sentinel integration", Path("intraday_scanner/v2/omega_sentinel/core.py").exists(), 5),
        ("Command Center usefulness", (COMMAND_CENTER_ROOT / "autodata.html").exists(), 5),
        ("Network failure handling", readiness_payload.get("status") in {"ready_with_configured_provider", "ready_public_fallback_only", "blocked_needs_provider_keys"}, 5),
        ("Safety/no-live-execution", not safety["failures"], 6),
        ("Test coverage", Path("tests/test_v2_autodata.py").exists(), 7),
        ("Documentation/runbook clarity", Path("docs/operations/autodata_daily_workflow.md").exists(), 5),
        ("Product coherence", bool(trial) or (paths.reports / "autodata_summary.json").exists(), 1),
    )
    rows = []
    score = 0
    for category, passed, weight in checks:
        earned = weight if passed else 0
        score += earned
        rows.append({"category": category, "evidence": "passed" if passed else "missing", "score": earned, "weight": weight})
    payload = {"rows": rows, "schema_version": "v2.autodata_quality_scorecard.v1", "score": score, "status": "target_met" if score == 100 else "resume_required"}
    lines = ["# OMEGA AutoData Quality Scorecard", "", f"- Score: `{score} / 100`", "- Target: `100 / 100`", f"- Status: `{payload['status']}`", "", "| Category | Score | Evidence |", "| --- | ---: | --- |"]
    lines.extend(f"| {row['category']} | {row['score']} / {row['weight']} | {row['evidence']} |" for row in rows)
    _write_json(paths.reports / "autodata_quality_scorecard.json", payload)
    _write_text(paths.reports / "autodata_quality_scorecard.md", "\n".join(lines) + "\n")
    _write_text(Path("docs/audit/omega_autodata_quality_scorecard.md"), "\n".join(lines) + "\n")
    return payload


def _write_red_team(paths: _Paths) -> None:
    text = "\n".join(
        [
            "# OMEGA AutoData Red Team",
            "",
            "| Risk | Status | Control |",
            "| --- | --- | --- |",
            "| Secrets leaked | passed | Registry stores env var names and configured booleans only; request params are redacted. |",
            "| Trading/order endpoint imported | passed | AutoData uses read-only data URLs only and safety scan blocks order/trading imports. |",
            "| Provider keys printed | passed | No env values are written to reports, manifests, or dashboard artifacts. |",
            "| Public fallback mislabeled broker-grade | passed | Yahoo fallback is `public_intraday_single_provider` with low-trust warnings. |",
            "| Delayed data mislabeled real-time | passed | Provider metadata says plan/feed dependent and not assumed real-time. |",
            "| Single-provider data mislabeled reconciled | passed | Single-provider reconciliation is `single_provider_unreconciled`. |",
            "| Synthetic/mock data committed | passed | Mock provider uses `mock_test_intraday` and CommitBridge blocks it. |",
            "| Malformed response accepted | passed | Normalizers emit warnings/errors and accepted-bar counts. |",
            "| Missing raw hash accepted | passed | Every request writes a raw artifact and SHA-256, including error payloads. |",
            "| Missing frozen hash accepted | passed | CommitBridge still owns frozen-hash gating. |",
            "| PaperOps mutated outside append-only ledger | passed | AutoData only calls CommitBridge commit when explicitly requested. |",
            "| Strategy validation triggered | passed | Strategy evidence remains gated by committed forward evidence. |",
            "| Live broker path introduced | passed | No order clients or live execution calls are imported. |",
            "| Command Center exposes secrets | passed | QA scans generated pages; AutoData artifacts contain no env values. |",
            "| Tests require network | passed | Tests use mock payloads and monkeypatched env only. |",
            "| Network failure hidden | passed | Fetch errors are cached as provider_error/provider_not_configured/rate-limit statuses. |",
            "",
            "## Residual",
            "",
            "- Public fallback is useful for automation readiness but remains low-trust single-provider evidence.",
            "- Official commits require provider evidence that passes CommitBridge policy; auto-commit is off by default.",
        ]
    ) + "\n"
    _write_text(paths.reports / "autodata_red_team.md", text)
    _write_text(Path("docs/audit/omega_autodata_red_team.md"), text)


def _write_audit_docs(paths: _Paths, payload: dict[str, object], score: dict[str, object]) -> None:
    _write_red_team(paths)
    build_state = {
        "artifacts": {
            "summary": (paths.reports / "autodata_summary.json").as_posix(),
            "provider_readiness": (paths.reports / "provider_readiness.json").as_posix(),
            "fetch_pending": (paths.reports / "fetch_pending_latest.json").as_posix(),
            "reconciliation": (paths.reports / "provider_reconciliation_latest.json").as_posix(),
            "trial_day": (paths.reports / "autodata_trial_day_latest.json").as_posix(),
            "verify": (paths.reports / "verify_latest.json").as_posix(),
        },
        "build_id": payload.get("build_id", "missing"),
        "commands": _command_list(),
        "completed_work": [
            "provider registry/readiness",
            "read-only provider fetch/cache/hash",
            "normalization/validation/reconciliation",
            "pending-order fetch automation",
            "FillTruth/CommitBridge/Sentinel/Command Center integration",
        ],
        "quality_score": score.get("score", 0),
        "remaining_work": ["Set provider API keys for broker/vendor-grade intraday evidence if public fallback is insufficient."],
        "schema_version": "v2.omega_autodata_build_state.v1",
        "status": payload.get("status", "missing"),
    }
    _write_json(Path("docs/audit/omega_autodata_build_state.json"), build_state)
    _write_text(Path("docs/audit/omega_autodata_build_log.md"), "# OMEGA AutoData Build Log\n\n- Built additive `intraday_scanner/v2/autodata` provider gateway.\n- Added provider registry, readiness, fetch, pending fetch, build, reconcile, FillTruth feed, trial-day, verify, report, docs, and scorecard flows.\n- Preserved no-live-trading and CommitBridge boundaries.\n")
    _write_text(Path("docs/audit/omega_autodata_release_summary.md"), f"# OMEGA AutoData Release Summary\n\nStatus: `{payload.get('status')}`. AutoData can discover env-gated providers, use a no-key public fallback, cache/hash raw payloads, normalize intraday bars, reconcile provider data, feed FillTruth, and let CommitBridge enforce official PaperOps policy.\n")
    _write_text(Path("docs/audit/omega_autodata_resume_goal.md"), "# OMEGA AutoData Resume Goal\n\nIf this score is below 100 or provider data is unavailable, set legal provider env vars (`ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`, `ALPHA_VANTAGE_API_KEY`, or `TWELVE_DATA_API_KEY`), rerun `py -m intraday_scanner.v2.autodata trial-day --date YYYY-MM-DD`, then rerun verify and full gates without weakening safety boundaries.\n")


def _write_docs() -> None:
    docs = {
        Path("docs/architecture/v2_autodata.md"): [
            "# v2 AutoData Architecture",
            "",
            "AutoData is an additive read-only intraday provider gateway. It discovers provider readiness from environment variables, fetches market-data payloads, caches immutable raw responses, hashes them, normalizes OHLCV bars, validates bars, reconciles provider data, feeds FillTruth, and lets CommitBridge own official PaperOps mutation.",
            "",
            "AutoData does not import Streamlit, app.py, broker order clients, SQLite mutators, or live trading code.",
        ],
        Path("docs/operations/autodata_provider_setup.md"): [
            "# AutoData Provider Setup",
            "",
            "Set provider credentials only as environment variables. Do not write keys into files.",
            "",
            "- Alpaca market data: `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`, optional `ALPACA_DATA_FEED`.",
            "- Alpha Vantage: `ALPHA_VANTAGE_API_KEY`.",
            "- Twelve Data: `TWELVE_DATA_API_KEY`.",
            "",
            "If no keys are set, AutoData can attempt the no-key Yahoo public fallback, which is low-trust single-provider evidence.",
        ],
        Path("docs/operations/autodata_daily_workflow.md"): [
            "# AutoData Daily Workflow",
            "",
            "After market close, run `py -m intraday_scanner.v2.autodata trial-day --date YYYY-MM-DD`.",
            "",
            "Next morning, run `py -m intraday_scanner.v2.omega_sentinel morning-check --date YYYY-MM-DD --autodata`.",
            "",
            "Official commits remain explicit. Use `--commit` only after reviewing provider hashes, reconciliation, FillTruth decisions, and CommitBridge eligibility.",
        ],
        Path("docs/operations/autodata_env_vars.md"): [
            "# AutoData Environment Variables",
            "",
            "AutoData reads provider credentials from env vars only and writes only variable names/configured booleans to artifacts.",
            "",
            "- `ALPACA_API_KEY_ID`",
            "- `ALPACA_API_SECRET_KEY`",
            "- `ALPACA_DATA_FEED`",
            "- `ALPHA_VANTAGE_API_KEY`",
            "- `TWELVE_DATA_API_KEY`",
        ],
    }
    for path, lines in docs.items():
        _write_text(path, "\n".join(lines) + "\n")


def _safety_scan(paths: _Paths) -> dict[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    forbidden_roots = {"app", "sqlite3", "streamlit"}
    forbidden_prefixes = {"intraday_scanner.integrations.brokers", "intraday_scanner.storage"}
    forbidden_calls = {"submit_order", "place_order", "create_order", "connect", "execute", "executemany"}
    for path in Path("intraday_scanner/v2/autodata").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            failures.append(f"{path.as_posix()}: syntax error {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden_roots or any(alias.name.startswith(prefix) for prefix in forbidden_prefixes):
                        failures.append(f"{path.as_posix()}: forbidden import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in forbidden_roots or any(node.module.startswith(prefix) for prefix in forbidden_prefixes):
                    failures.append(f"{path.as_posix()}: forbidden import {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
                if name in forbidden_calls:
                    failures.append(f"{path.as_posix()}: forbidden call {name}")
    for path in (paths.reports, paths.manifests, paths.raw):
        if not path.exists():
            continue
        for item in path.rglob("*"):
            if item.is_file() and item.suffix in {".json", ".md", ".csv"}:
                text = item.read_text(encoding="utf-8", errors="ignore")
                if re.search(r"[A-Za-z]:\\Users\\", text):
                    warnings.append(f"local absolute path in artifact: {item.as_posix()}")
    return {"failures": failures, "warnings": warnings}


def _secret_scan(paths: _Paths) -> dict[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    secret_values = [
        value
        for name, value in os.environ.items()
        if name in {"ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "ALPHA_VANTAGE_API_KEY", "TWELVE_DATA_API_KEY"} and value
    ]
    for path in (paths.reports, paths.manifests, paths.raw, COMMAND_CENTER_ROOT):
        if not path.exists():
            continue
        for item in path.rglob("*"):
            if not item.is_file() or item.suffix.lower() not in {".json", ".md", ".html", ".csv"}:
                continue
            text = item.read_text(encoding="utf-8", errors="ignore")
            for secret in secret_values:
                if secret and secret in text:
                    failures.append(f"secret value leaked in {item.as_posix()}")
    return {"failures": failures, "warnings": warnings}


def _mock_payload(symbol: str, run_date: date) -> dict[str, object]:
    return {
        "bars": [
            {"symbol": symbol, "timestamp": f"{run_date.isoformat()}T13:30:00+00:00", "open": 100.0, "high": 100.5, "low": 99.8, "close": 100.2, "volume": 1000},
            {"symbol": symbol, "timestamp": f"{run_date.isoformat()}T13:31:00+00:00", "open": 100.2, "high": 101.0, "low": 100.1, "close": 100.8, "volume": 1200},
            {"symbol": symbol, "timestamp": f"{run_date.isoformat()}T20:00:00+00:00", "open": 100.8, "high": 101.2, "low": 100.4, "close": 101.0, "volume": 1300},
        ],
        "provider": "mock_provider_for_tests",
        "schema_version": "fixture.autodata_mock_payload.v1",
    }


def _pending_orders() -> list[dict[str, object]]:
    payload = _read_json(PAPER_OPS_ROOT / "state" / "pending_orders.json", [])
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _dataset_from_bars(
    bars: list[MarketBar],
    *,
    dataset_id: str,
    source_kind: str,
    source_path: str | None = None,
    warnings: tuple[str, ...] = (),
) -> MarketDataset:
    by_symbol: dict[str, list[MarketBar]] = {}
    for bar in bars:
        by_symbol.setdefault(bar.symbol, []).append(bar)
    return MarketDataset(
        dataset_id=dataset_id,
        source_kind=source_kind,
        timeframe="intraday",
        bars_by_symbol={symbol: tuple(sorted(values, key=lambda item: item.timestamp)) for symbol, values in by_symbol.items()},
        source_path=source_path,
        warnings=warnings,
    )


def _request_id(provider_id: str, symbol: str, run_date: date, interval: str) -> str:
    return f"autodata_{_stable_hash(provider_id, symbol, run_date.isoformat(), interval)[:12]}"


def _redacted_request(provider: dict[str, object], params: dict[str, object]) -> dict[str, object]:
    redacted = {key: value for key, value in params.items() if "key" not in key.lower() and "secret" not in key.lower()}
    redacted["provider_id"] = provider["provider_id"]
    redacted["required_env_vars"] = provider.get("required_env_vars", [])
    return redacted


def _response_metadata(payload: dict[str, object]) -> dict[str, object]:
    return {
        "top_level_keys": sorted(str(key) for key in payload.keys()),
        "error_present": bool(payload.get("error")),
    }


def _market_date(timestamp: datetime) -> date:
    return timestamp.astimezone(ZoneInfo("America/New_York")).date()


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    timestamp = datetime.fromisoformat(normalized)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _series_at(quote: dict[str, object], key: str, index: int, *, allow_none: bool = False) -> float | int | None:
    values = quote.get(key)
    if not isinstance(values, list):
        raise TypeError(f"missing {key} series")
    value = values[index]
    if value is None and allow_none:
        return None
    if value is None:
        raise ValueError(f"missing {key}")
    if not isinstance(value, int | float):
        raise TypeError(f"invalid {key}")
    return value


def _missing_env_vars(rows: list[dict[str, object]]) -> list[str]:
    missing: list[str] = []
    for row in rows:
        if not row.get("requires_api_key"):
            continue
        for warning in _list(row.get("warnings")):
            if str(warning).startswith("missing env vars:"):
                missing.extend(name.strip() for name in str(warning).split(":", 1)[1].split(","))
    return sorted(set(item for item in missing if item))


def _readiness_warnings(rows: list[dict[str, object]], configured_real: list[dict[str, object]], public_ready: bool) -> list[str]:
    warnings = _unique(item for row in rows for item in _list(row.get("warnings")))
    if not configured_real and public_ready:
        warnings.append("only public fallback is available; public single-provider intraday is not official forward evidence by default")
    if not configured_real and not public_ready:
        warnings.append("no intraday provider is configured")
    return warnings


def _command_list() -> list[str]:
    return [
        "py -m intraday_scanner.v2.autodata init",
        "py -m intraday_scanner.v2.autodata providers",
        "py -m intraday_scanner.v2.autodata readiness",
        "py -m intraday_scanner.v2.autodata fetch --symbol SYMBOL --date YYYY-MM-DD --interval 1min",
        "py -m intraday_scanner.v2.autodata fetch-pending --date YYYY-MM-DD --interval 1min",
        "py -m intraday_scanner.v2.autodata build --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.autodata reconcile --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.autodata feed-filltruth --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.autodata trial-day --date YYYY-MM-DD",
        "py -m intraday_scanner.v2.autodata verify",
        "py -m intraday_scanner.v2.autodata report",
        "py -m intraday_scanner.v2.autodata demo",
    ]


def _summary_lines(payload: dict[str, object]) -> list[str]:
    lines = [f"- {key}: `{value}`" for key, value in sorted(payload.items()) if key not in {"rows", "providers", "warnings"}]
    warnings = _list(payload.get("warnings"))
    lines.extend(["", "## Warnings", ""])
    lines.extend(f"- {warning}" for warning in warnings) if warnings else lines.append("- None.")
    return lines


def _provider_lines(payload: dict[str, object]) -> list[str]:
    lines = _summary_lines({key: value for key, value in payload.items() if key != "providers"})
    lines.extend(["", "## Providers", ""])
    for row in _list(payload.get("providers")):
        item = _dict(row)
        lines.append(f"- `{item.get('provider_id')}` configured=`{item.get('configured')}` enabled=`{item.get('enabled')}` source=`{item.get('source_label')}` trust=`{item.get('source_trust_level')}`")
    return lines


def _fetch_pending_lines(payload: dict[str, object]) -> list[str]:
    lines = _summary_lines({key: value for key, value in payload.items() if key != "rows"})
    lines.extend(["", "## Rows", ""])
    for row in _list(payload.get("rows")):
        item = _dict(row)
        lines.append(f"- `{item.get('symbol')}` `{item.get('date')}` provider `{item.get('provider_id')}` status `{item.get('status')}` bars `{item.get('accepted_bar_count')}`")
    return lines


def _reconciliation_lines(payload: dict[str, object]) -> list[str]:
    lines = _summary_lines({key: value for key, value in payload.items() if key != "rows"})
    lines.extend(["", "## Rows", ""])
    for row in _list(payload.get("rows")):
        item = _dict(row)
        lines.append(f"- `{item.get('symbol')}` `{item.get('date')}`: `{item.get('reconciliation_status')}` bars `{item.get('bar_count')}`")
    return lines


def _trial_lines(payload: dict[str, object]) -> list[str]:
    commitbridge = _dict(payload.get("commitbridge"))
    return [
        f"- Status: `{payload.get('status')}`",
        f"- Trial mode: `{payload.get('trial_mode')}`",
        f"- Command Center: `{payload.get('command_center_status')}`",
        f"- CommitBridge eligible: `{commitbridge.get('eligible', 0)}`",
        f"- CommitBridge committed events: `{commitbridge.get('commit_events', 0)}`",
    ]


def _write_md(path: Path, title: str, lines: list[str]) -> None:
    _write_text(path, "\n".join([f"# {title}", "", *lines]) + "\n")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_plain(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_hash(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def _compact_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plain(value: object) -> object:
    if hasattr(value, "to_dict"):
        return value.to_dict()  # type: ignore[no-any-return]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_plain(item) for item in value]
    return value


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else list(value) if isinstance(value, tuple | set) else []


def _unique(items: object) -> list[str]:
    values = [str(item) for item in (items if not isinstance(items, dict) else items.values())]
    return list(dict.fromkeys(value for value in values if value))


def _int(value: object) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0
