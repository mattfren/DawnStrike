"""Attest PaperOps lifecycle economics to immutable retained daily bars."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.data_truth import load_datatruth_snapshot
from intraday_scanner.v2.paper_ops.models import PaperRunMode, stable_id
from intraday_scanner.v2.paper_ops.storage import read_json, read_jsonl, write_json

ATTESTED_EVENT_TYPES = {
    "paper_fill",
    "paper_position_opened",
    "paper_position_checked_no_action",
    "paper_position_marked_to_market",
    "paper_position_closed",
}
_MANIFEST_SCHEMA = "v2.paper_ops_manifest.v3"
_SHADOW_RUN_MANIFEST_SCHEMA = "v2.paper_ops_shadow_run.v1"
_POLICY_MANIFEST_SCHEMA = "v2.paper_execution_policy_manifest.v1"
_REFERENCE_STRATEGIES = {
    "benchmark_buy_hold_equal_weight",
    "cash_no_trade_baseline",
}


@dataclass(frozen=True)
class SourceBarTruthResult:
    status: str
    audited_event_count: int
    audited_run_count: int
    warnings: tuple[str, ...]
    mode: str | None = None
    audited_reference_row_count: int = 0
    schema_version: str = "v2.paper_ops_source_bar_truth.v2"

    def to_dict(self) -> dict[str, object]:
        return {
            "audited_event_count": self.audited_event_count,
            "audited_reference_row_count": self.audited_reference_row_count,
            "audited_run_count": self.audited_run_count,
            "mode": self.mode,
            "schema_version": self.schema_version,
            "status": self.status,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class _ExecutionPolicy:
    version: str
    fingerprint: str
    fee_bps: float
    slippage_bps: float
    timeout_days: int
    starting_equity: float
    universe_id: str
    universe_symbols: tuple[str, ...]


def verify_source_bar_truth(
    *,
    output_root: Path = Path("data/v2_paper_ops"),
    mode: PaperRunMode | str | None = None,
) -> SourceBarTruthResult:
    """Verify scoped lifecycle prices and reference returns against retained bytes."""

    from intraday_scanner.v2.paper_ops.engine import (
        PaperOpsPaths,
    )

    selected_mode = _selected_mode(mode)
    from intraday_scanner.v2.paper_ops.observer_safety import require_observer_command

    require_observer_command(output_root, "verify-source-bars", mode=selected_mode)
    paths = PaperOpsPaths.resolve(output_root)
    warnings: list[str] = []
    policies = _execution_policies(
        paths.state / "execution_policy_manifest.json",
        warnings,
    )
    manifests = _run_manifests(
        paths.manifests,
        output_root,
        warnings,
        selected_mode=selected_mode,
    )
    datasets: dict[str, MarketDataset] = {}
    for run_id, run_manifest in manifests.items():
        dataset = _load_bound_snapshot(run_manifest, policies, warnings)
        if dataset is not None:
            datasets[run_id] = dataset
    if not datasets:
        label = selected_mode or "forward/replay"
        warnings.append(f"no attestable {label} PaperOps run manifests are retained")

    events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    orders: dict[str, dict[str, object]] = {}
    fills: dict[str, dict[str, object]] = {}
    opened_positions: dict[str, dict[str, object]] = {}
    active_positions: dict[str, dict[str, object]] = {}
    open_count_by_order: dict[str, int] = {}
    audited = 0
    for event in events:
        event_mode = str(event.get("mode") or "")
        if event_mode not in {PaperRunMode.FORWARD.value, PaperRunMode.REPLAY.value}:
            continue
        if selected_mode is not None and event_mode != selected_mode:
            continue
        event_type = str(event.get("event_type") or "")
        if event_type != "paper_order_created" and event_type not in ATTESTED_EVENT_TYPES:
            continue
        payload = event.get("payload")
        event_id = str(event.get("event_id") or "unknown")
        if not isinstance(payload, dict):
            warnings.append(f"event {event_id} payload is not an object")
            continue
        run_id = str(event.get("run_id") or "")
        event_manifest = manifests.get(run_id)
        dataset = datasets.get(run_id)
        if event_manifest is None or dataset is None:
            warnings.append(f"event {event_id} has no verified immutable run snapshot")
            continue
        _validate_event_run(event, event_manifest, warnings)
        policy = _event_policy(payload, event_manifest, policies, event_id, warnings)

        if event_type == "paper_order_created":
            _validate_order_origin(event_id, payload, event, warnings)
            _store_unique(orders, payload, "order_id", "order", warnings)
            continue

        bar = _attested_bar(event, payload, event_manifest, dataset, warnings)
        if bar is None:
            continue
        if event_type == "paper_fill":
            order_id = str(payload.get("order_id") or "")
            order = orders.get(order_id)
            if order is None:
                warnings.append(f"fill {event_id} has no exact originating order")
            elif policy is not None:
                _validate_fill(
                    event_id,
                    payload,
                    order,
                    event,
                    dataset,
                    bar,
                    policy,
                    warnings,
                )
            _store_unique(fills, payload, "order_id", "fill", warnings)
        elif event_type == "paper_position_opened":
            order_id = str(payload.get("order_id") or "")
            order = orders.get(order_id)
            fill = fills.get(order_id)
            if order is None:
                warnings.append(f"position open {event_id} has no exact order")
            if fill is None:
                warnings.append(f"position open {event_id} has no exact fill")
            if order is not None and fill is not None and policy is not None:
                _validate_open_position(
                    event_id,
                    payload,
                    order,
                    fill,
                    bar,
                    policy,
                    warnings,
                )
            position_id = str(payload.get("position_id") or "")
            _store_unique(
                opened_positions,
                payload,
                "position_id",
                "position",
                warnings,
            )
            if position_id:
                if position_id in active_positions:
                    warnings.append(f"position {position_id} was opened more than once")
                active_positions[position_id] = dict(payload)
            open_count_by_order[order_id] = open_count_by_order.get(order_id, 0) + 1
        elif event_type in {
            "paper_position_checked_no_action",
            "paper_position_marked_to_market",
        }:
            position_id = str(payload.get("position_id") or "")
            opened = opened_positions.get(position_id)
            if opened is None or position_id not in active_positions:
                warnings.append(f"mark {event_id} has no active exact opened position")
            elif policy is not None:
                _validate_mark(
                    event_id,
                    payload,
                    opened,
                    event,
                    dataset,
                    bar,
                    policy,
                    warnings,
                )
        elif event_type == "paper_position_closed":
            position_id = str(payload.get("position_id") or "")
            opened = opened_positions.get(position_id)
            if opened is None or position_id not in active_positions:
                warnings.append(f"close {event_id} has no active exact opened position")
            elif policy is not None:
                _validate_close(
                    event_id,
                    payload,
                    opened,
                    event,
                    dataset,
                    bar,
                    policy,
                    warnings,
                )
                active_positions.pop(position_id, None)
        audited += 1

    for order_id in sorted(fills):
        count = open_count_by_order.get(order_id, 0)
        if count != 1:
            warnings.append(
                f"fill for order {order_id} has {count} opened-position events; expected 1"
            )

    audited_reference_rows = _verify_reference_returns(
        paths.calendar / "strategy_daily_returns.csv",
        manifests,
        datasets,
        policies,
        warnings,
        selected_mode=selected_mode,
    )
    result = SourceBarTruthResult(
        status="passed" if not warnings else "failed",
        audited_event_count=audited,
        audited_run_count=len(datasets),
        audited_reference_row_count=audited_reference_rows,
        mode=selected_mode,
        warnings=tuple(sorted(set(warnings))),
    )
    filename = (
        f"source_bar_truth_{selected_mode}_latest.json"
        if selected_mode is not None
        else "source_bar_truth_latest.json"
    )
    write_json(paths.reconciliation / filename, result.to_dict())
    return result


def _selected_mode(mode: PaperRunMode | str | None) -> str | None:
    if isinstance(mode, PaperRunMode):
        value = mode.value
    elif mode is None:
        return None
    else:
        value = str(mode)
    if value not in {PaperRunMode.FORWARD.value, PaperRunMode.REPLAY.value}:
        raise ValueError("source-bar truth mode must be forward, replay, or omitted")
    return value


def _execution_policies(
    manifest_path: Path,
    warnings: list[str],
) -> dict[str, _ExecutionPolicy]:
    payload = read_json(manifest_path, {})
    if not isinstance(payload, dict) or payload.get("schema_version") != _POLICY_MANIFEST_SCHEMA:
        warnings.append("execution-policy manifest is missing or unsupported")
        return {}
    raw_policies = payload.get("policies")
    if not isinstance(raw_policies, dict) or not raw_policies:
        warnings.append("execution-policy manifest has no frozen policies")
        return {}
    result: dict[str, _ExecutionPolicy] = {}
    for raw_version, raw_policy in sorted(raw_policies.items(), key=lambda item: str(item[0])):
        version = str(raw_version).strip()
        if not version or not isinstance(raw_policy, dict):
            warnings.append("execution-policy manifest contains a malformed policy")
            continue
        configuration = raw_policy.get("configuration")
        fingerprint = str(raw_policy.get("fingerprint") or "")
        if not isinstance(configuration, dict):
            warnings.append(f"execution policy {version} has no frozen configuration")
            continue
        expected_fingerprint = _payload_sha256(configuration)
        if fingerprint != expected_fingerprint:
            warnings.append(f"execution policy {version} fingerprint mismatch")
            continue
        fee_bps = _number(configuration.get("fee_bps"))
        slippage_bps = _number(configuration.get("slippage_bps"))
        timeout_days = _integer(configuration.get("paper_timeout_days"))
        starting_equity = _number(configuration.get("starting_equity"))
        universe_id = str(configuration.get("universe_id") or "").strip()
        raw_symbols = configuration.get("universe_symbols")
        universe_symbols = (
            tuple(str(symbol).upper() for symbol in raw_symbols)
            if isinstance(raw_symbols, list)
            else ()
        )
        if (
            fee_bps is None
            or fee_bps < 0
            or slippage_bps is None
            or slippage_bps < 0
            or timeout_days is None
            or timeout_days < 1
            or starting_equity is None
            or starting_equity <= 0
            or not universe_id
            or not universe_symbols
            or len(universe_symbols) != len(set(universe_symbols))
        ):
            warnings.append(f"execution policy {version} configuration is invalid")
            continue
        result[version] = _ExecutionPolicy(
            version=version,
            fingerprint=fingerprint,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            timeout_days=timeout_days,
            starting_equity=starting_equity,
            universe_id=universe_id,
            universe_symbols=universe_symbols,
        )
    active = str(payload.get("active_execution_policy_version") or "")
    if active not in result:
        warnings.append("active execution policy is absent from the frozen manifest")
    return result


def _run_manifests(
    manifest_dir: Path,
    output_root: Path,
    warnings: list[str],
    *,
    selected_mode: str | None,
) -> dict[str, dict[str, object]]:
    by_run: dict[str, dict[str, object]] = {}
    for path in sorted(manifest_dir.glob("*.json")):
        payload = read_json(path, {})
        if not isinstance(payload, dict) or not payload.get("run_id"):
            continue
        if payload.get("schema_version") == _SHADOW_RUN_MANIFEST_SCHEMA:
            # A shadow execution artifact is bound to the champion run_id so
            # candidate events can reuse the exact immutable source snapshot.
            # It is not itself a PaperOps run manifest and must not compete
            # with the champion manifest for that run_id.
            continue
        mode = str(payload.get("mode") or "")
        if selected_mode is not None and mode != selected_mode:
            continue
        if mode not in {PaperRunMode.FORWARD.value, PaperRunMode.REPLAY.value}:
            continue
        run_id = str(payload["run_id"])
        if run_id in by_run and by_run[run_id] != payload:
            warnings.append(f"run {run_id} has conflicting PaperOps manifests")
            continue
        if payload.get("schema_version") != _MANIFEST_SCHEMA:
            warnings.append(f"run {run_id} has unattestable PaperOps manifest schema")
            by_run[run_id] = payload
            continue
        observed_hash = str(payload.get("manifest_payload_hash") or "")
        hash_payload = dict(payload)
        hash_payload.pop("manifest_payload_hash", None)
        if observed_hash != _payload_sha256(hash_payload):
            warnings.append(f"run {run_id} PaperOps manifest payload hash mismatch")
        relative_root = str(payload.get("data_truth_root_relative") or "")
        if not relative_root or Path(relative_root).is_absolute():
            warnings.append(f"run {run_id} DataTruth root binding is invalid")
        else:
            resolved = (output_root.resolve() / relative_root).resolve()
            expected = (
                (output_root.resolve().parent / "v2_data_truth").resolve()
                if mode == PaperRunMode.FORWARD.value
                else (output_root.resolve() / "data_truth_replay").resolve()
            )
            if resolved != expected:
                warnings.append(f"run {run_id} DataTruth root is not canonical for {mode}")
            else:
                payload = {**payload, "resolved_data_truth_root": resolved.as_posix()}
        by_run[run_id] = payload
    return by_run


def _load_bound_snapshot(
    run_manifest: dict[str, object],
    policies: dict[str, _ExecutionPolicy],
    warnings: list[str],
) -> MarketDataset | None:
    run_id = str(run_manifest.get("run_id") or "unknown")
    snapshot_id = str(run_manifest.get("data_snapshot_id") or "")
    root_text = str(run_manifest.get("resolved_data_truth_root") or "")
    if not snapshot_id or not root_text:
        warnings.append(f"run {run_id} immutable snapshot binding is incomplete")
        return None
    try:
        dataset, manifest = load_datatruth_snapshot(snapshot_id, Path(root_text))
    except (OSError, TypeError, ValueError) as exc:
        warnings.append(f"run {run_id} immutable snapshot failed verification: {exc}")
        return None
    comparisons = {
        "content hash": (
            run_manifest.get("data_snapshot_content_hash"),
            manifest.snapshot_content_hash,
        ),
        "manifest payload hash": (
            run_manifest.get("data_snapshot_manifest_payload_hash"),
            manifest.manifest_payload_hash,
        ),
        "normalized hash": (
            run_manifest.get("data_snapshot_normalized_hash"),
            manifest.normalized_artifact_hash,
        ),
        "normalized path": (
            run_manifest.get("data_snapshot_normalized_path"),
            manifest.normalized_artifact_path,
        ),
    }
    for label, (observed, expected) in comparisons.items():
        if not observed or observed != expected:
            warnings.append(f"run {run_id} DataTruth {label} binding mismatch")
    run_date = str(run_manifest.get("run_date") or "")
    if manifest.accepted_end != run_date:
        warnings.append(f"run {run_id} DataTruth accepted end does not equal run date")
    policy_version = str(run_manifest.get("execution_policy_version") or "")
    policy_fingerprint = str(run_manifest.get("execution_policy_fingerprint") or "")
    policy = policies.get(policy_version)
    if policy is None or policy.fingerprint != policy_fingerprint:
        warnings.append(f"run {run_id} execution policy binding is invalid")
    raw_universe = run_manifest.get("universe_symbols")
    expected_universe = (
        tuple(str(symbol).upper() for symbol in raw_universe)
        if isinstance(raw_universe, list)
        else ()
    )
    dataset_universe = tuple(str(symbol).upper() for symbol in dataset.symbols)
    if (
        not expected_universe
        or len(expected_universe) != len(set(expected_universe))
        or len(dataset_universe) != len(set(dataset_universe))
        or set(dataset_universe) != set(expected_universe)
    ):
        warnings.append(f"run {run_id} DataTruth universe binding mismatch")
    if policy is not None and (
        policy.universe_symbols != expected_universe
        or policy.universe_id != str(run_manifest.get("universe_id") or "")
    ):
        warnings.append(f"run {run_id} universe differs from its execution policy")
    return dataset


def _validate_event_run(
    event: dict[str, object],
    run_manifest: dict[str, object],
    warnings: list[str],
) -> None:
    event_id = str(event.get("event_id") or "unknown")
    comparisons = {
        "run_id": (event.get("run_id"), run_manifest.get("run_id")),
        "mode": (event.get("mode"), run_manifest.get("mode")),
        "trade_date": (event.get("trade_date"), run_manifest.get("run_date")),
    }
    for label, (observed, expected) in comparisons.items():
        if observed != expected:
            warnings.append(f"event {event_id} does not match run manifest {label}")


def _event_policy(
    payload: dict[str, object],
    run_manifest: dict[str, object],
    policies: dict[str, _ExecutionPolicy],
    event_id: str,
    warnings: list[str],
) -> _ExecutionPolicy | None:
    version = str(payload.get("execution_policy_version") or "")
    if version != str(run_manifest.get("execution_policy_version") or ""):
        warnings.append(f"event {event_id} execution policy differs from run manifest")
    policy = policies.get(version)
    if policy is None:
        warnings.append(f"event {event_id} execution policy is not frozen")
        return None
    return policy


def _validate_order_origin(
    event_id: str,
    order: dict[str, object],
    event: dict[str, object],
    warnings: list[str],
) -> None:
    for label, observed, expected in (
        ("run_id", order.get("run_id"), event.get("run_id")),
        ("mode", order.get("mode"), event.get("mode")),
        ("trade_date", order.get("trade_date"), event.get("trade_date")),
        ("strategy_id", order.get("strategy_id"), event.get("strategy_id")),
        ("symbol", order.get("symbol"), event.get("symbol")),
    ):
        if observed != expected:
            warnings.append(f"order {event_id} origin {label} mismatch")


def _attested_bar(
    event: dict[str, object],
    payload: dict[str, object],
    run_manifest: dict[str, object],
    dataset: MarketDataset,
    warnings: list[str],
) -> MarketBar | None:
    event_id = str(event.get("event_id") or "unknown")
    source = payload.get("source_bar")
    if not isinstance(source, dict):
        warnings.append(f"event {event_id} has no source bar evidence")
        return None
    required = {"symbol", "timestamp", "open", "high", "low", "close", "volume"}
    if set(source) != required:
        warnings.append(f"event {event_id} source bar fields are incomplete")
        return None
    observed_hash = str(payload.get("source_bar_sha256") or "")
    if observed_hash != _payload_sha256(source):
        warnings.append(f"event {event_id} source bar hash mismatch")
    if payload.get("data_snapshot_id") != run_manifest.get("data_snapshot_id"):
        warnings.append(f"event {event_id} source snapshot identity mismatch")
    symbol = str(source.get("symbol") or "")
    timestamp = str(source.get("timestamp") or "")
    if symbol != str(payload.get("symbol") or ""):
        warnings.append(f"event {event_id} source bar symbol mismatch")
    try:
        source_time = datetime.fromisoformat(timestamp)
    except ValueError:
        warnings.append(f"event {event_id} source bar timestamp is invalid")
        return None
    if source_time.date().isoformat() != str(event.get("trade_date") or ""):
        warnings.append(f"event {event_id} source bar session date mismatch")
    matches = [
        bar
        for bar in dataset.bars_by_symbol.get(symbol, ())
        if bar.timestamp.isoformat() == timestamp
    ]
    if len(matches) != 1:
        warnings.append(f"event {event_id} source bar is absent or ambiguous in snapshot")
        return None
    retained = matches[0]
    expected_source = _source_bar_payload(retained)
    if _payload_sha256(source) != _payload_sha256(expected_source):
        for field in ("open", "high", "low", "close", "volume"):
            if source.get(field) != expected_source[field]:
                warnings.append(f"event {event_id} source bar {field} differs from snapshot")
    return retained


def _validate_fill(
    event_id: str,
    fill: dict[str, object],
    order: dict[str, object],
    event: dict[str, object],
    dataset: MarketDataset,
    bar: MarketBar,
    policy: _ExecutionPolicy,
    warnings: list[str],
) -> None:
    _compare_lineage(
        f"fill {event_id}",
        fill,
        order,
        (
            "mode",
            "strategy_id",
            "strategy_version",
            "symbol",
            "execution_policy_version",
            "strategy_semantics_fingerprint",
        ),
        warnings,
    )
    order_id = str(order.get("order_id") or "")
    direction = str(order.get("direction") or "")
    quantity = _integer(fill.get("quantity"))
    order_quantity = _integer(order.get("quantity"))
    if direction not in {"long", "short"} or quantity is None or quantity <= 0:
        warnings.append(f"fill {event_id} direction or quantity is invalid")
        return
    if quantity != order_quantity:
        warnings.append(f"fill {event_id} quantity does not match its order")
    if fill.get("order_id") != order_id:
        warnings.append(f"fill {event_id} order identity mismatch")
    if fill.get("run_id") != event.get("run_id") or fill.get("mode") != event.get("mode"):
        warnings.append(f"fill {event_id} lifecycle run identity mismatch")
    if fill.get("fill_time") != bar.timestamp.isoformat():
        warnings.append(f"fill {event_id} time does not match source bar")
    if fill.get("fill_id") != stable_id("fill", order_id, bar.timestamp.isoformat()):
        warnings.append(f"fill {event_id} stable identity mismatch")
    expected_bar = _first_fill_bar(order, event, dataset, warnings, event_id)
    if expected_bar is None or expected_bar.timestamp != bar.timestamp:
        warnings.append(f"fill {event_id} does not use the exact first eligible bar")
    earliest = str(order.get("earliest_fill_date") or "")
    if earliest and bar.timestamp.date().isoformat() < earliest:
        warnings.append(f"fill {event_id} predates earliest eligible fill date")
    rate = policy.slippage_bps / 10_000.0
    expected_price = bar.open * (1 + rate if direction == "long" else 1 - rate)
    expected_fee = expected_price * quantity * policy.fee_bps / 10_000.0
    expected_slippage = abs(expected_price - bar.open) * quantity
    for label, observed, expected in (
        ("price", fill.get("fill_price"), expected_price),
        ("fee", fill.get("fee"), expected_fee),
        ("slippage", fill.get("slippage"), expected_slippage),
    ):
        if not _matches(_number(observed), expected):
            warnings.append(f"fill {event_id} {label} does not match source bar policy")


def _first_fill_bar(
    order: dict[str, object],
    event: dict[str, object],
    dataset: MarketDataset,
    warnings: list[str],
    event_id: str,
) -> MarketBar | None:
    try:
        signal_time = datetime.fromisoformat(str(order.get("signal_time") or ""))
        run_date = date.fromisoformat(str(event.get("trade_date") or ""))
    except ValueError:
        warnings.append(f"fill {event_id} order timing is invalid")
        return None
    symbol = str(order.get("symbol") or "")
    return next(
        (
            bar
            for bar in dataset.bars_by_symbol.get(symbol, ())
            if bar.timestamp > signal_time and bar.timestamp.date() <= run_date
        ),
        None,
    )


def _validate_open_position(
    event_id: str,
    position: dict[str, object],
    order: dict[str, object],
    fill: dict[str, object],
    bar: MarketBar,
    policy: _ExecutionPolicy,
    warnings: list[str],
) -> None:
    del policy
    _compare_lineage(
        f"position {event_id}",
        position,
        order,
        (
            "order_id",
            "strategy_id",
            "strategy_version",
            "symbol",
            "direction",
            "execution_policy_version",
            "strategy_semantics_fingerprint",
        ),
        warnings,
    )
    order_id = str(order.get("order_id") or "")
    if position.get("position_id") != stable_id("position", order_id):
        warnings.append(f"position {event_id} stable identity mismatch")
    for label, observed, expected in (
        ("entry price", position.get("entry_price"), fill.get("fill_price")),
        ("entry fee", position.get("entry_fee"), fill.get("fee")),
        ("quantity", position.get("quantity"), fill.get("quantity")),
        ("stop", position.get("stop"), order.get("stop")),
        ("target", position.get("target"), order.get("target")),
        ("last mark", position.get("last_mark_price"), fill.get("fill_price")),
    ):
        if not _matches(_number(observed), _number(expected)):
            warnings.append(f"position {event_id} {label} lineage mismatch")
    if position.get("opened_at") != fill.get("fill_time"):
        warnings.append(f"position {event_id} opened_at does not match fill time")
    if position.get("opened_at") != bar.timestamp.isoformat():
        warnings.append(f"position {event_id} source bar does not match its fill")
    if str(position.get("status") or "") != "open":
        warnings.append(f"position {event_id} is not opened with open status")
    entry_fee = _number(position.get("entry_fee"))
    if not _matches(_number(position.get("realized_pnl")), 0.0):
        warnings.append(f"position {event_id} opening realized P&L is invalid")
    if entry_fee is None or not _matches(
        _number(position.get("unrealized_pnl")),
        -entry_fee,
    ):
        warnings.append(f"position {event_id} opening unrealized P&L is invalid")


def _validate_mark(
    event_id: str,
    mark: dict[str, object],
    opened: dict[str, object],
    event: dict[str, object],
    dataset: MarketDataset,
    bar: MarketBar,
    policy: _ExecutionPolicy,
    warnings: list[str],
) -> None:
    _compare_position_lineage(event_id, mark, opened, warnings)
    _validate_latest_evaluation_bar(event_id, event, dataset, bar, warnings)
    direction = str(opened.get("direction") or "")
    entry = _number(opened.get("entry_price"))
    stop = _number(opened.get("stop"))
    target = _number(opened.get("target"))
    quantity = _integer(opened.get("quantity"))
    entry_fee = _number(opened.get("entry_fee"))
    if (
        direction not in {"long", "short"}
        or entry is None
        or stop is None
        or quantity is None
        or entry_fee is None
    ):
        warnings.append(f"mark {event_id} position economics are incomplete")
        return
    expected_reason, _raw_price = _expected_close(
        opened,
        bar,
        policy.timeout_days,
        stop=stop,
        target=target,
        direction=direction,
    )
    if expected_reason is not None:
        warnings.append(f"mark {event_id} bypasses a required {expected_reason} close")
    expected = _directional_pnl(direction, entry, bar.close, quantity) - entry_fee
    if not _matches(_number(mark.get("last_mark_price")), bar.close):
        warnings.append(f"mark {event_id} price does not match source close")
    if not _matches(_number(mark.get("unrealized_pnl")), expected):
        warnings.append(f"mark {event_id} unrealized P&L does not match source close")
    if not _matches(_number(mark.get("realized_pnl")), _number(opened.get("realized_pnl"))):
        warnings.append(f"mark {event_id} realized P&L changed while position is open")


def _validate_close(
    event_id: str,
    close: dict[str, object],
    position: dict[str, object],
    event: dict[str, object],
    dataset: MarketDataset,
    bar: MarketBar,
    policy: _ExecutionPolicy,
    warnings: list[str],
) -> None:
    _compare_lineage(
        f"close {event_id}",
        close,
        position,
        (
            "position_id",
            "strategy_id",
            "strategy_version",
            "symbol",
            "execution_policy_version",
            "strategy_semantics_fingerprint",
        ),
        warnings,
    )
    if close.get("run_id") != event.get("run_id") or close.get("mode") != event.get("mode"):
        warnings.append(f"close {event_id} lifecycle run identity mismatch")
    _validate_latest_evaluation_bar(event_id, event, dataset, bar, warnings)
    direction = str(position.get("direction") or "")
    entry = _number(position.get("entry_price"))
    stop = _number(position.get("stop"))
    target = _number(position.get("target"))
    quantity = _integer(position.get("quantity"))
    entry_fee = _number(position.get("entry_fee"))
    if (
        direction not in {"long", "short"}
        or entry is None
        or stop is None
        or quantity is None
        or entry_fee is None
    ):
        warnings.append(f"close {event_id} position economics are incomplete")
        return
    expected_reason, raw_price = _expected_close(
        position,
        bar,
        policy.timeout_days,
        stop=stop,
        target=target,
        direction=direction,
    )
    if expected_reason is None or raw_price is None:
        warnings.append(f"close {event_id} is not triggered by its source bar")
        return
    if str(close.get("close_reason") or "") != expected_reason:
        warnings.append(f"close {event_id} reason contradicts source bar precedence")
    if close.get("close_time") != bar.timestamp.isoformat():
        warnings.append(f"close {event_id} time does not match source bar")
    position_id = str(position.get("position_id") or "")
    expected_close_id = stable_id(
        "close",
        position_id,
        bar.timestamp.isoformat(),
        expected_reason,
    )
    if close.get("close_id") != expected_close_id:
        warnings.append(f"close {event_id} stable identity mismatch")
    rate = policy.slippage_bps / 10_000.0
    expected_price = raw_price * (1 - rate if direction == "long" else 1 + rate)
    exit_fee = expected_price * quantity * policy.fee_bps / 10_000.0
    gross = _directional_pnl(direction, entry, expected_price, quantity)
    net = gross - entry_fee - exit_fee
    stop_fill = stop * (1 - rate if direction == "long" else 1 + rate)
    stop_gross = _directional_pnl(direction, entry, stop_fill, quantity)
    stop_fee = stop_fill * quantity * policy.fee_bps / 10_000.0
    risk = max(0.0, -stop_gross) + entry_fee + stop_fee
    expected_r = net / risk if risk else 0.0
    expected_slippage = abs(expected_price - raw_price) * quantity
    for label, observed, expected in (
        ("price", close.get("close_price"), expected_price),
        ("slippage", close.get("slippage"), expected_slippage),
        ("exit fee", close.get("fee"), exit_fee),
        ("entry fee", close.get("entry_fee"), entry_fee),
        ("gross P&L", close.get("gross_pnl"), gross),
        ("net P&L", close.get("net_pnl"), net),
        ("R-multiple", close.get("r_multiple"), expected_r),
    ):
        if not _matches(_number(observed), expected):
            warnings.append(f"close {event_id} {label} does not match source bar policy")


def _compare_position_lineage(
    event_id: str,
    observed: dict[str, object],
    opened: dict[str, object],
    warnings: list[str],
) -> None:
    string_fields = (
        "position_id",
        "order_id",
        "strategy_id",
        "strategy_version",
        "symbol",
        "direction",
        "status",
        "opened_at",
        "execution_policy_version",
        "strategy_semantics_fingerprint",
    )
    _compare_lineage(f"mark {event_id}", observed, opened, string_fields, warnings)
    for field in ("quantity", "entry_price", "stop", "target", "entry_fee"):
        if not _matches(_number(observed.get(field)), _number(opened.get(field))):
            warnings.append(f"mark {event_id} immutable {field} lineage mismatch")


def _compare_lineage(
    label: str,
    observed: dict[str, object],
    expected: dict[str, object],
    fields: tuple[str, ...],
    warnings: list[str],
) -> None:
    for field in fields:
        if observed.get(field) != expected.get(field):
            warnings.append(f"{label} {field} lineage mismatch")


def _validate_latest_evaluation_bar(
    event_id: str,
    event: dict[str, object],
    dataset: MarketDataset,
    bar: MarketBar,
    warnings: list[str],
) -> None:
    try:
        run_date = date.fromisoformat(str(event.get("trade_date") or ""))
    except ValueError:
        warnings.append(f"event {event_id} trade date is invalid")
        return
    eligible = [
        candidate
        for candidate in dataset.bars_by_symbol.get(bar.symbol, ())
        if candidate.timestamp.date() <= run_date
    ]
    if not eligible or eligible[-1].timestamp != bar.timestamp:
        warnings.append(f"event {event_id} does not use the latest eligible source bar")


def _verify_reference_returns(
    calendar_path: Path,
    manifests: dict[str, dict[str, object]],
    datasets: dict[str, MarketDataset],
    policies: dict[str, _ExecutionPolicy],
    warnings: list[str],
    *,
    selected_mode: str | None,
) -> int:
    if not calendar_path.exists():
        return 0
    with calendar_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    scoped = [
        row
        for row in rows
        if row.get("strategy_id") in _REFERENCE_STRATEGIES
        and (selected_mode is None or row.get("mode") == selected_mode)
    ]
    counts: dict[tuple[str, str], int] = {}
    prior_ending: dict[tuple[str, str, str, str], float] = {}
    prior_peak: dict[tuple[str, str, str, str], float] = {}
    audited = 0
    for row in sorted(scoped, key=lambda item: (item.get("date", ""), item.get("strategy_id", ""))):
        run_id = str(row.get("run_id") or "")
        run_manifest = manifests.get(run_id)
        dataset = datasets.get(run_id)
        row_id = f"{row.get('date')}:{row.get('mode')}:{row.get('strategy_id')}"
        if run_manifest is None or dataset is None:
            warnings.append(f"reference row {row_id} has no verified run snapshot")
            continue
        policy = policies.get(str(run_manifest.get("execution_policy_version") or ""))
        if policy is None:
            warnings.append(f"reference row {row_id} has no frozen run policy")
            continue
        if row.get("date") != run_manifest.get("run_date") or row.get("mode") != run_manifest.get(
            "mode"
        ):
            warnings.append(f"reference row {row_id} run date or mode mismatch")
        if row.get("data_snapshot_id") != run_manifest.get("data_snapshot_id"):
            warnings.append(f"reference row {row_id} snapshot identity mismatch")
        strategy_id = str(row.get("strategy_id") or "")
        expected_status = (
            "benchmark" if strategy_id == "benchmark_buy_hold_equal_weight" else "baseline"
        )
        if row.get("strategy_version") != "v1.0" or row.get("strategy_status") != expected_status:
            warnings.append(f"reference row {row_id} strategy identity mismatch")
        if str(row.get("strategy_semantics_fingerprint") or "unknown") != "unknown":
            warnings.append(f"reference row {row_id} semantics identity mismatch")
        expected_daily = 0.0
        if strategy_id == "benchmark_buy_hold_equal_weight":
            try:
                expected_daily = _benchmark_daily_return(
                    dataset,
                    str(row.get("date") or ""),
                )
            except ValueError as exc:
                warnings.append(f"reference row {row_id} {exc}")
                continue
            if str(row.get("execution_policy_version") or "") != ("equal_weight_close_to_close_v1"):
                warnings.append(f"reference row {row_id} benchmark policy mismatch")
        elif str(row.get("execution_policy_version") or "") != "cash_zero_interest_v1":
            warnings.append(f"reference row {row_id} cash policy mismatch")
        series = (
            str(row.get("mode") or ""),
            strategy_id,
            str(row.get("strategy_version") or ""),
            str(row.get("execution_policy_version") or ""),
        )
        previous = prior_ending.get(series, policy.starting_equity)
        ending = previous * (1.0 + expected_daily)
        peak = max(prior_peak.get(series, policy.starting_equity), ending)
        expected_values = {
            "starting_equity": policy.starting_equity,
            "ending_equity": ending,
            "realized_pnl": 0.0,
            "unrealized_pnl": ending - policy.starting_equity,
            "total_pnl": ending - previous,
            "daily_return_pct": expected_daily,
            "cumulative_return_pct": (ending - policy.starting_equity) / policy.starting_equity,
            "drawdown_pct": (ending - peak) / peak if peak else 0.0,
            "trades_opened": 0.0,
            "trades_closed": 0.0,
            "pending_orders": 0.0,
            "open_positions": 0.0,
            "wins": 0.0,
            "losses": 0.0,
            "flats": 0.0,
            "average_r": 0.0,
            "expectancy_r": 0.0,
            "exposure_pct": 0.0,
            "fees_paid": 0.0,
            "slippage_estimate": 0.0,
        }
        for field, expected in expected_values.items():
            if not _matches(_number(row.get(field)), expected):
                warnings.append(f"reference row {row_id} {field} source mismatch")
        prior_ending[series] = ending
        prior_peak[series] = peak
        count_key = (str(row.get("date") or ""), str(row.get("mode") or ""))
        counts[count_key] = counts.get(count_key, 0) + 1
        audited += 1
    calendar_run_keys = {
        (str(row.get("date") or ""), str(row.get("mode") or ""))
        for row in rows
        if str(row.get("run_id") or "") in datasets
        and (selected_mode is None or row.get("mode") == selected_mode)
    }
    for run_key in sorted(calendar_run_keys):
        if counts.get(run_key, 0) != 2:
            warnings.append(
                f"run {run_key[0]}:{run_key[1]} has {counts.get(run_key, 0)} reference rows; "
                "expected cash and benchmark"
            )
    return audited


def _benchmark_daily_return(dataset: MarketDataset, run_date: str) -> float:
    day = date.fromisoformat(run_date)
    returns: list[float] = []
    for symbol in dataset.symbols:
        eligible = [
            bar for bar in dataset.bars_by_symbol.get(symbol, ()) if bar.timestamp.date() <= day
        ]
        if len(eligible) < 2 or eligible[-1].timestamp.date() != day:
            raise ValueError(
                f"benchmark has no exact close-to-close source for {symbol}:{run_date}"
            )
        prior = eligible[-2].close
        if prior <= 0:
            raise ValueError(f"benchmark has an invalid prior close for {symbol}:{run_date}")
        returns.append((eligible[-1].close - prior) / prior)
    if not returns:
        raise ValueError(f"benchmark has no configured symbols for {run_date}")
    return sum(returns) / len(returns)


def _expected_close(
    position: dict[str, object],
    bar: MarketBar,
    timeout_days: int,
    *,
    stop: float,
    target: float | None,
    direction: str,
) -> tuple[str | None, float | None]:
    stop_gap = bar.open <= stop if direction == "long" else bar.open >= stop
    if stop_gap:
        return "stop", bar.open
    target_gap = target is not None and (
        bar.open >= target if direction == "long" else bar.open <= target
    )
    if target_gap:
        return "target", bar.open
    stop_hit = bar.low <= stop if direction == "long" else bar.high >= stop
    if stop_hit:
        return "stop", stop
    target_hit = target is not None and (
        bar.high >= target if direction == "long" else bar.low <= target
    )
    if target_hit:
        return "target", target
    try:
        opened = datetime.fromisoformat(str(position.get("opened_at") or ""))
    except ValueError:
        return None, None
    if (bar.timestamp.date() - opened.date()).days >= timeout_days:
        return "timeout", bar.close
    return None, None


def _store_unique(
    target: dict[str, dict[str, object]],
    payload: dict[str, object],
    field: str,
    label: str,
    warnings: list[str],
) -> None:
    key = str(payload.get(field) or "")
    if not key:
        warnings.append(f"{label} has no {field}")
    elif key in target and target[key] != payload:
        warnings.append(f"conflicting {label} lineage for {key}")
    else:
        target[key] = dict(payload)


def _source_bar_payload(bar: MarketBar) -> dict[str, object]:
    return {
        "close": bar.close,
        "high": bar.high,
        "low": bar.low,
        "open": bar.open,
        "symbol": bar.symbol,
        "timestamp": bar.timestamp.isoformat(),
        "volume": bar.volume,
    }


def _payload_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _number(value: object) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value: object) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _matches(observed: float | None, expected: float | None) -> bool:
    if observed is None or expected is None:
        return observed is expected
    return math.isclose(observed, expected, rel_tol=1e-9, abs_tol=1e-8)


def _directional_pnl(
    direction: str,
    entry: float,
    exit_price: float,
    quantity: int,
) -> float:
    return (
        (exit_price - entry) * quantity if direction == "long" else (entry - exit_price) * quantity
    )
