"""Deterministic signal-to-close PaperOps trade blotter."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from intraday_scanner.v2.paper_ops.models import PaperCloseReason
from intraday_scanner.v2.paper_ops.source_bar_truth import verify_source_bar_truth
from intraday_scanner.v2.paper_ops.storage import read_json, read_jsonl, write_csv, write_json

BLOTTER_FIELDS = (
    "record_id",
    "mode",
    "signal_date",
    "run_id",
    "fill_run_id",
    "close_run_id",
    "data_snapshot_id",
    "fill_data_snapshot_id",
    "close_data_snapshot_id",
    "strategy_id",
    "strategy_version",
    "strategy_status",
    "strategy_semantics_fingerprint",
    "execution_policy_version",
    "challenger_id",
    "series_role",
    "symbol",
    "direction",
    "lifecycle_status",
    "decision_status",
    "decision_reason",
    "signal_id",
    "signal_time",
    "setup_score",
    "order_id",
    "order_block_reason",
    "expected_fill_rule",
    "earliest_fill_date",
    "entry_reference",
    "stop",
    "target",
    "quantity_requested",
    "fill_id",
    "fill_time",
    "fill_price",
    "quantity_filled",
    "position_id",
    "last_mark_price",
    "unrealized_pnl",
    "close_id",
    "close_time",
    "close_price",
    "close_reason",
    "gross_pnl",
    "entry_fee",
    "exit_fee",
    "fees_paid",
    "entry_slippage",
    "exit_slippage",
    "slippage_paid",
    "net_pnl",
    "r_multiple",
    "trade_return_pct",
)


class ReadOnlyBlotterRows(list[dict[str, object]]):
    """Materialized rows plus the exact immutable-input identity consumed.

    An empty blotter still needs a source identity. Carrying it on the batch,
    rather than only on individual rows, also lets daily learning freeze one
    internally consistent point-in-time cohort without re-reading a ledger
    that may have grown after the cutoff.
    """

    def __init__(
        self,
        values: list[dict[str, object]],
        *,
        input_hash_sha256: str,
        ledger_hash_sha256: str,
        warnings: list[str],
        input_generation: dict[str, object] | None = None,
    ) -> None:
        super().__init__(values)
        self.read_only_input_hash_sha256 = input_hash_sha256
        self.ledger_source_hash_sha256 = ledger_hash_sha256
        self.blotter_warnings = tuple(warnings)
        self.read_only_input_generation = dict(input_generation or {})


def build_trade_blotter(
    *,
    output_root: Path,
    mode: str | None = None,
    run_date: str | None = None,
) -> dict[str, object]:
    from intraday_scanner.v2.paper_ops.observer_safety import require_observer_command

    require_observer_command(output_root, "blotter")
    return _build_trade_blotter_writer(
        output_root=output_root,
        mode=mode,
        run_date=run_date,
    )


def _build_trade_blotter_writer(
    *,
    output_root: Path,
    mode: str | None = None,
    run_date: str | None = None,
) -> dict[str, object]:
    """Write blotter artifacts for an already writer-authorized tree."""

    rows, warnings = _materialize_rows_unchecked(output_root)
    selected = [
        row
        for row in rows
        if (mode is None or row["mode"] == mode)
        and (run_date is None or row["signal_date"] == run_date)
    ]
    payload = _payload(selected, warnings, mode=mode, run_date=run_date)
    exports = output_root / "exports"
    write_json(exports / "paper_trade_blotter.json", _payload(rows, warnings))
    write_csv(exports / "paper_trade_blotter.csv", rows, BLOTTER_FIELDS)
    if mode is not None and run_date is not None:
        stem = f"paper_trade_blotter_{mode}_{run_date}"
        write_json(exports / f"{stem}.json", payload)
        write_csv(exports / f"{stem}.csv", selected, BLOTTER_FIELDS)
    write_json(output_root / "reports" / "paper_trade_blotter_summary.json", payload)
    return payload


def verify_trade_blotter(
    *,
    output_root: Path,
    mode: str | None = None,
) -> dict[str, object]:
    from intraday_scanner.v2.paper_ops.observer_safety import require_observer_command

    # The canonical export is global.  Therefore verification is deliberately
    # all-mode too: selecting forward must never allow unattested replay rows
    # in that same stored artifact to escape review.
    require_observer_command(output_root, "verify-blotter", mode=None)
    rows, warnings = _materialize_rows_unchecked(output_root)
    stored = read_json(output_root / "exports" / "paper_trade_blotter.json", {})
    stored_rows = stored.get("rows", []) if isinstance(stored, dict) else []
    mismatches: list[str] = []
    source_truth: dict[str, object]
    try:
        source_result = verify_source_bar_truth(output_root=output_root, mode=None)
        source_truth = source_result.to_dict()
        if source_result.status != "passed":
            mismatches.extend(f"source-bar truth: {warning}" for warning in source_result.warnings)
    except (OSError, TypeError, ValueError) as exc:
        source_truth = {"status": "failed", "warnings": [str(exc)]}
        mismatches.append(f"source-bar truth verification failed: {exc}")
    if stored_rows != rows:
        mismatches.append("stored blotter differs from deterministic ledger rebuild")
    record_counts = Counter(str(row.get("record_id") or "") for row in rows)
    duplicates = sorted(key for key, count in record_counts.items() if key and count > 1)
    if duplicates:
        mismatches.extend(f"duplicate blotter record {item}" for item in duplicates)
    status = "passed" if not warnings and not mismatches else "failed"
    result = {
        "mismatches": mismatches,
        "row_count": len(rows),
        "schema_version": "v2.paper_trade_blotter_verification.v2",
        "source_bar_truth": source_truth,
        "status": status,
        "warnings": warnings,
    }
    write_json(output_root / "reconciliation" / "trade_blotter_verify_latest.json", result)
    return result


def load_trade_blotter_readonly(
    *,
    output_root: Path,
    mode: str | None = None,
    run_date: str | None = None,
    strategy_id: str | None = None,
    strategy_version: str | None = None,
    series_role: str | None = None,
) -> ReadOnlyBlotterRows:
    """Materialize PaperOps lifecycle rows without writing any artifact.

    The regular blotter command writes exports and is therefore unsuitable for
    read-only learning.  This boundary reads the immutable ledger and applies
    the same deterministic materializer, attaching the ledger file hash as
    provenance for downstream attribution.
    """

    from intraday_scanner.v2.paper_ops.observer_safety import require_observer_command

    require_observer_command(output_root, "blotter")
    input_generation = describe_trade_blotter_readonly_inputs(output_root)
    input_hash_before = _hash_trade_blotter_input_description(input_generation)
    rows, warnings = _materialize_rows_unchecked(output_root)
    ledger_path = output_root / "ledger" / "paper_ledger.jsonl"
    ledger_hash = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    input_hash_after = hash_trade_blotter_readonly_inputs(output_root)
    if input_hash_before != input_hash_after:
        raise ValueError("PaperOps immutable inputs changed during read-only materialization")
    input_hash = input_hash_after
    selected: list[dict[str, object]] = []
    for row in rows:
        if mode is not None and str(row.get("mode") or "") != mode:
            continue
        if run_date is not None and str(row.get("signal_date") or "") != run_date[:10]:
            continue
        if strategy_id is not None and str(row.get("strategy_id") or "") != strategy_id:
            continue
        if (
            strategy_version is not None
            and str(row.get("strategy_version") or "") != strategy_version
        ):
            continue
        if series_role is not None and str(row.get("series_role") or "") != series_role:
            continue
        selected.append(
            {
                **row,
                "ledger_source_hash_sha256": ledger_hash,
                "read_only_input_hash_sha256": input_hash,
                "blotter_warnings": list(warnings),
                "read_only_materialization": True,
            }
        )
    return ReadOnlyBlotterRows(
        selected,
        input_hash_sha256=input_hash,
        ledger_hash_sha256=ledger_hash,
        warnings=warnings,
        input_generation=input_generation,
    )


# Explicitly named alias for callers that use the source vocabulary.
load_paper_ops_blotter_readonly = load_trade_blotter_readonly


def hash_trade_blotter_readonly_inputs(output_root: Path) -> str:
    """Hash the immutable bytes consumed by the read-only blotter materializer.

    This is intentionally separate from the ledger provenance hash.  The
    materializer also reads the execution-cost contract, strategy registry,
    and run manifests; binding all of those bytes prevents a daily-learning
    retry from reusing a receipt merely because its human source label stayed
    the same.  No export or reconciliation artifact is included because those
    are materializer outputs rather than inputs.
    """

    descriptions = describe_trade_blotter_readonly_inputs(output_root)
    return _hash_trade_blotter_input_description(descriptions)


def _hash_trade_blotter_input_description(descriptions: dict[str, object]) -> str:
    """Hash one stable input description without re-reading its files."""

    digest = hashlib.sha256()
    files = descriptions.get("files")
    if not isinstance(files, list):
        raise ValueError("PaperOps immutable input description is malformed")
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("PaperOps immutable input description entry is malformed")
        relative = str(item["path"]).encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        payload_hash = str(item.get("sha256") or "<missing>").encode("ascii")
        digest.update(len(payload_hash).to_bytes(8, "big"))
        digest.update(payload_hash)
        digest.update(int(item.get("size") or 0).to_bytes(8, "big"))
    return digest.hexdigest()


def describe_trade_blotter_readonly_inputs(output_root: Path) -> dict[str, object]:
    """Describe the exact immutable PaperOps bytes used by materialization."""

    requested_root = Path(output_root)
    if requested_root.is_symlink():
        raise ValueError(f"PaperOps immutable input root is a symlink: {requested_root}")
    root = requested_root.resolve()
    candidates = [
        root / "ledger" / "paper_ledger.jsonl",
        root / "state" / "paper_ops_config.json",
        root / "state" / "strategy_registry.json",
        root / "state" / "execution_policy_manifest.json",
    ]
    manifests = root / "manifests"
    if manifests.is_dir():
        candidates.extend(sorted(manifests.glob("*.json")))
    if not any(path.is_file() for path in candidates if path.parent != manifests):
        raise FileNotFoundError(f"PaperOps immutable input root is missing: {root}")
    files: list[dict[str, object]] = []
    for path in sorted(set(candidates), key=lambda item: item.relative_to(root).as_posix()):
        resolved_path = path.resolve()
        try:
            resolved_path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"PaperOps immutable input escapes its root: {path}") from exc
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError(f"PaperOps immutable input is a symlink: {path}")
        try:
            before = path.stat()
        except FileNotFoundError:
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": None,
                    "size": 0,
                    "mtime_ns": None,
                }
            )
            continue
        payload = path.read_bytes()
        after = path.stat()
        if (
            before.st_size,
            before.st_mtime_ns,
            before.st_ino,
        ) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
        ):
            raise ValueError(f"PaperOps immutable input changed during read: {path}")
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "mtime_ns": int(after.st_mtime_ns),
            }
        )
    ledger = next((item for item in files if item["path"] == "ledger/paper_ledger.jsonl"), None)
    ledger_size: object = ledger.get("size") if ledger else 0
    if not isinstance(ledger_size, (int, float, str, bytes, bytearray)):
        raise ValueError("PaperOps ledger size is malformed")
    return {
        "root": str(root),
        "files": files,
        "ledger_head_sha256": ledger.get("sha256") if ledger else None,
        "ledger_size": int(ledger_size or 0),
    }


def _materialize_rows(output_root: Path) -> tuple[list[dict[str, object]], list[str]]:
    from intraday_scanner.v2.paper_ops.observer_safety import require_observer_command

    require_observer_command(output_root, "blotter")
    return _materialize_rows_unchecked(output_root)


def _materialize_rows_unchecked(
    output_root: Path,
) -> tuple[list[dict[str, object]], list[str]]:
    events = read_jsonl(output_root / "ledger" / "paper_ledger.jsonl")
    ordered = list(events)
    snapshot_by_run = _snapshot_ids_by_run(output_root)
    registry = _live_registry(output_root)
    cost_settings = _execution_cost_settings(output_root)
    live_keys = {
        (
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or "unknown"),
            str(row.get("execution_policy_version") or "legacy_unspecified"),
            str(row.get("strategy_semantics_fingerprint") or "unknown"),
        ): row
        for row in registry
    }
    live_triples = {
        (
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or "unknown"),
            str(row.get("execution_policy_version") or "legacy_unspecified"),
        ): str(row.get("strategy_semantics_fingerprint") or "unknown")
        for row in registry
    }
    decisions: dict[str, dict[str, object]] = {}
    orders: dict[str, dict[str, object]] = {}
    blocks: dict[str, dict[str, object]] = {}
    fills_by_order: dict[str, list[dict[str, object]]] = {}
    positions_by_order: dict[str, dict[str, object]] = {}
    positions_by_id: dict[str, dict[str, object]] = {}
    opened_position_ids: set[str] = set()
    closes_by_position: dict[str, list[dict[str, object]]] = {}
    warnings: list[str] = []
    event_ids = [str(event.get("event_id") or "") for event in ordered]
    duplicate_event_ids = sorted(
        event_id for event_id, count in Counter(event_ids).items() if event_id and count > 1
    )
    warnings.extend(f"duplicate event id {event_id}" for event_id in duplicate_event_ids)
    for event in ordered:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            warnings.append(f"event {event.get('event_id')} has no object payload")
            continue
        _validate_event_envelope(event, payload, warnings)
        event_type = str(event.get("event_type") or "")
        if event_type == "paper_pick_decision":
            decision_id = str(payload.get("pick_id") or "")
            if decision_id:
                _store_unique(
                    decisions,
                    decision_id,
                    {**payload, "event_run_id": event.get("run_id")},
                    "decision",
                    warnings,
                )
        elif event_type == "paper_no_setup_decision":
            decision_id = str(payload.get("decision_id") or "")
            if decision_id:
                _store_unique(
                    decisions,
                    decision_id,
                    {**payload, "event_run_id": event.get("run_id")},
                    "decision",
                    warnings,
                )
        elif event_type == "paper_order_created":
            order_id = str(payload.get("order_id") or "")
            if order_id:
                _store_unique(orders, order_id, dict(payload), "order", warnings)
        elif event_type == "paper_order_blocked":
            order_id = str(payload.get("order_id") or "")
            if order_id:
                _store_unique(blocks, order_id, dict(payload), "order block", warnings)
                orders.setdefault(order_id, dict(payload))
        elif event_type == "paper_fill":
            order_id = str(payload.get("order_id") or "")
            if order_id:
                fills_by_order.setdefault(order_id, []).append(
                    {**payload, "event_run_id": event.get("run_id")}
                )
        elif event_type in {
            "paper_position_opened",
            "paper_position_checked_no_action",
            "paper_position_marked_to_market",
        }:
            order_id = str(payload.get("order_id") or "")
            position_id = str(payload.get("position_id") or "")
            if order_id:
                positions_by_order[order_id] = dict(payload)
            if position_id:
                positions_by_id[position_id] = dict(payload)
                if event_type == "paper_position_opened":
                    if position_id in opened_position_ids:
                        warnings.append(f"duplicate position open {position_id}")
                    opened_position_ids.add(position_id)
        elif event_type == "paper_position_closed":
            position_id = str(payload.get("position_id") or "")
            if position_id:
                closes_by_position.setdefault(position_id, []).append(dict(payload))
    for order_id, _fills in fills_by_order.items():
        if order_id not in orders:
            warnings.append(f"orphan fill for order {order_id}")
    for order_id, position in positions_by_order.items():
        if order_id not in orders:
            warnings.append(f"orphan position for order {order_id}")
        if order_id not in fills_by_order:
            warnings.append(f"position without fill for order {order_id}")
        position_id = str(position.get("position_id") or "")
        if position_id and position_id not in opened_position_ids:
            warnings.append(f"position state without open event {position_id}")
    for position_id in closes_by_position:
        if position_id not in positions_by_id or position_id not in opened_position_ids:
            warnings.append(f"orphan close for position {position_id}")
    used_decisions: set[str] = set()
    rows: list[dict[str, object]] = []
    for order_id in sorted(orders):
        order = orders[order_id]
        pick_id = str(order.get("pick_id") or "")
        decision = decisions.get(pick_id, {})
        if not pick_id or not decision:
            warnings.append(f"order {order_id} has no source decision")
        if pick_id:
            used_decisions.add(pick_id)
        fills = fills_by_order.get(order_id, [])
        if len(fills) > 1:
            warnings.append(f"order {order_id} has {len(fills)} fills")
        fill = fills[0] if fills else {}
        position = positions_by_order.get(order_id, {})
        position_id = str(position.get("position_id") or "")
        closes = closes_by_position.get(position_id, [])
        if len(closes) > 1:
            warnings.append(f"position {position_id} has {len(closes)} closes")
        close = closes[0] if closes else {}
        block = blocks.get(order_id, {})
        _validate_linked_lifecycle(
            order_id,
            order=order,
            decision=decision,
            fill=fill,
            position=position,
            close=close,
            block=block,
            cost_settings=cost_settings,
            warnings=warnings,
        )
        status = (
            "blocked" if block else "closed" if close else "open" if fill or position else "pending"
        )
        rows.append(
            _lifecycle_row(
                order,
                decision,
                fill,
                position,
                close,
                block,
                status,
                snapshot_by_run=snapshot_by_run,
                live_keys=live_keys,
            )
        )
    for decision_id in sorted(decisions):
        if decision_id in used_decisions:
            continue
        decision = decisions[decision_id]
        status = str(
            decision.get("decision_status") or decision.get("decision") or "signal_recorded"
        )
        rows.append(
            _decision_only_row(
                decision_id,
                decision,
                status,
                snapshot_by_run=snapshot_by_run,
                live_keys=live_keys,
            )
        )
    rows.sort(
        key=lambda row: (
            str(row.get("signal_date") or ""),
            str(row.get("mode") or ""),
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
            str(row.get("execution_policy_version") or ""),
            str(row.get("symbol") or ""),
            str(row.get("record_id") or ""),
        )
    )
    warnings.extend(
        f"{row['record_id']}: current series has unknown entry data snapshot"
        for row in rows
        if row.get("series_role") in {"champion", "challenger"}
        and row.get("data_snapshot_id") == "unknown"
    )
    warnings.extend(
        f"{row['record_id']}: filled current series has unknown fill data snapshot"
        for row in rows
        if row.get("series_role") in {"champion", "challenger"}
        and row.get("fill_id")
        and row.get("fill_data_snapshot_id") == "unknown"
    )
    warnings.extend(
        f"{row['record_id']}: closed current series has unknown close data snapshot"
        for row in rows
        if row.get("series_role") in {"champion", "challenger"}
        and row.get("lifecycle_status") == "closed"
        and row.get("close_data_snapshot_id") == "unknown"
    )
    warnings.extend(
        f"{row['record_id']}: current strategy triple has a different semantics fingerprint"
        for row in rows
        if not row.get("challenger_id")
        and (
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or "unknown"),
            str(row.get("execution_policy_version") or "legacy_unspecified"),
        )
        in live_triples
        and str(row.get("strategy_semantics_fingerprint") or "unknown")
        != live_triples[
            (
                str(row.get("strategy_id") or ""),
                str(row.get("strategy_version") or "unknown"),
                str(row.get("execution_policy_version") or "legacy_unspecified"),
            )
        ]
    )
    return rows, sorted(set(warnings))


def _validate_event_envelope(
    event: dict[str, object],
    payload: dict[str, object],
    warnings: list[str],
) -> None:
    event_id = str(event.get("event_id") or "unknown")
    event_type = str(event.get("event_type") or "")
    for field in ("mode", "strategy_id", "symbol"):
        outer = event.get(field)
        inner = payload.get(field)
        if outer not in {None, ""} and inner not in {None, ""} and outer != inner:
            warnings.append(f"event {event_id} envelope/payload {field} mismatch")
    outer_run_id = event.get("run_id")
    payload_run_id = payload.get("run_id")
    lifecycle_run_id = (
        payload_run_id
        if event_type == "paper_fill"
        else payload.get("lifecycle_run_id", payload_run_id)
    )
    if event_type == "paper_fill":
        if outer_run_id in {None, ""}:
            warnings.append(f"event {event_id} fill envelope run_id is missing")
        if payload_run_id in {None, ""}:
            warnings.append(f"event {event_id} fill payload run_id is missing")
    if (
        outer_run_id not in {None, ""}
        and lifecycle_run_id not in {None, ""}
        and outer_run_id != lifecycle_run_id
    ):
        warnings.append(f"event {event_id} envelope/payload run_id mismatch")


def _validate_linked_lifecycle(
    order_id: str,
    *,
    order: dict[str, object],
    decision: dict[str, object],
    fill: dict[str, object],
    position: dict[str, object],
    close: dict[str, object],
    block: dict[str, object],
    cost_settings: tuple[float, float],
    warnings: list[str],
) -> None:
    components = (
        ("decision", decision),
        ("fill", fill),
        ("position", position),
        ("close", close),
        ("block", block),
    )
    defaults = {
        "execution_policy_version": "legacy_unspecified",
        "strategy_semantics_fingerprint": "unknown",
        "strategy_version": "unknown",
    }
    for label, component in components:
        if not component:
            continue
        for field in (
            "strategy_id",
            "strategy_version",
            "execution_policy_version",
            "strategy_semantics_fingerprint",
            "symbol",
        ):
            expected = str(order.get(field) or defaults.get(field, ""))
            observed = str(component.get(field) or defaults.get(field, ""))
            if observed != expected:
                warnings.append(f"order {order_id} {label} {field} lineage mismatch")
        for field in ("mode", "direction"):
            if field in component and component.get(field) not in {None, ""}:
                if component.get(field) != order.get(field):
                    warnings.append(f"order {order_id} {label} {field} lineage mismatch")
    if fill and str(fill.get("order_id") or "") != order_id:
        warnings.append(f"order {order_id} fill order_id lineage mismatch")
    if block and str(block.get("origin_run_id") or block.get("run_id") or "") != str(
        order.get("run_id") or ""
    ):
        warnings.append(f"order {order_id} block origin run_id lineage mismatch")
    if position and str(position.get("order_id") or "") != order_id:
        warnings.append(f"order {order_id} position order_id lineage mismatch")
    position_id = str(position.get("position_id") or "")
    if close and str(close.get("position_id") or "") != position_id:
        warnings.append(f"order {order_id} close position_id lineage mismatch")
    if fill and position:
        fill_quantity = _number(fill.get("quantity"))
        position_quantity = _number(position.get("quantity"))
        if fill_quantity != position_quantity:
            warnings.append(f"order {order_id} fill/position quantity mismatch")
        for field in ("stop", "target"):
            if _number(order.get(field)) != _number(position.get(field)):
                warnings.append(f"order {order_id} order/position {field} mismatch")
        for left_label, left_value, right_label, right_value in (
            (
                "order quantity",
                _number(order.get("quantity")),
                "fill quantity",
                fill_quantity,
            ),
            (
                "fill price",
                _number(fill.get("fill_price")),
                "position entry_price",
                _number(position.get("entry_price")),
            ),
            (
                "fill fee",
                _number(fill.get("fee")),
                "position entry_fee",
                _number(position.get("entry_fee")),
            ),
        ):
            if not _numbers_match(left_value, right_value):
                warnings.append(f"order {order_id} {left_label}/{right_label} economic mismatch")
    if close and fill and position:
        _validate_close_economics(
            order_id,
            fill=fill,
            position=position,
            close=close,
            cost_settings=cost_settings,
            warnings=warnings,
        )


def _validate_close_economics(
    order_id: str,
    *,
    fill: dict[str, object],
    position: dict[str, object],
    close: dict[str, object],
    cost_settings: tuple[float, float],
    warnings: list[str],
) -> None:
    fee_bps, slippage_bps = cost_settings
    close_reason = str(close.get("close_reason") or "")
    valid_close_reasons = {reason.value for reason in PaperCloseReason}
    if close_reason not in valid_close_reasons:
        warnings.append(f"order {order_id} close reason is invalid")
    for label, value in (
        ("entry slippage", _number(fill.get("slippage"))),
        ("exit slippage", _number(close.get("slippage"))),
    ):
        if value is None or value < 0:
            warnings.append(f"order {order_id} {label} is missing or invalid")
    direction = str(position.get("direction") or "")
    entry = _number(position.get("entry_price"))
    close_price = _number(close.get("close_price"))
    stop = _number(position.get("stop"))
    quantity = _number(position.get("quantity"))
    entry_fee = _number(position.get("entry_fee"))
    exit_fee = _number(close.get("fee"))
    values = (entry, close_price, stop, quantity, entry_fee, exit_fee)
    if direction not in {"long", "short"} or any(value is None for value in values):
        warnings.append(f"order {order_id} close economics are incomplete")
        return
    assert entry is not None
    assert close_price is not None
    assert stop is not None
    assert quantity is not None
    assert entry_fee is not None
    assert exit_fee is not None
    expected_entry_fee = entry * quantity * fee_bps / 10_000.0
    expected_exit_fee = close_price * quantity * fee_bps / 10_000.0
    if not _numbers_match(_number(fill.get("fee")), expected_entry_fee):
        warnings.append(f"order {order_id} entry fee does not match execution policy")
    if not _numbers_match(entry_fee, expected_entry_fee):
        warnings.append(f"order {order_id} position entry fee does not match execution policy")
    if not _numbers_match(_number(close.get("entry_fee")), entry_fee):
        warnings.append(f"order {order_id} close entry fee lineage mismatch")
    if not _numbers_match(exit_fee, expected_exit_fee):
        warnings.append(f"order {order_id} exit fee does not match execution policy")
    expected_gross = (
        (close_price - entry) * quantity
        if direction == "long"
        else (entry - close_price) * quantity
    )
    expected_net = expected_gross - entry_fee - exit_fee
    if not _numbers_match(_number(close.get("gross_pnl")), expected_gross):
        warnings.append(f"order {order_id} gross_pnl arithmetic mismatch")
    if not _numbers_match(_number(close.get("net_pnl")), expected_net):
        warnings.append(f"order {order_id} net_pnl arithmetic mismatch")
    slip_rate = slippage_bps / 10_000.0
    stop_fill = stop * (1.0 - slip_rate) if direction == "long" else stop * (1.0 + slip_rate)
    stop_gross = (
        (stop_fill - entry) * quantity if direction == "long" else (entry - stop_fill) * quantity
    )
    stop_exit_fee = stop_fill * quantity * fee_bps / 10_000.0
    risk_amount = max(0.0, -stop_gross) + entry_fee + stop_exit_fee
    expected_r = expected_net / risk_amount if risk_amount else 0.0
    if not _numbers_match(_number(close.get("r_multiple")), expected_r):
        warnings.append(f"order {order_id} r_multiple arithmetic mismatch")


def _execution_cost_settings(output_root: Path) -> tuple[float, float]:
    payload = read_json(output_root / "state" / "paper_ops_config.json", {})
    if not isinstance(payload, dict):
        raise ValueError("PaperOps config is malformed")
    fee_bps = _number(payload.get("fee_bps"))
    slippage_bps = _number(payload.get("slippage_bps"))
    if fee_bps is None or slippage_bps is None or fee_bps < 0 or slippage_bps < 0:
        raise ValueError("PaperOps execution-cost settings are missing or invalid")
    return fee_bps, slippage_bps


def _numbers_match(
    observed: float | None,
    expected: float | None,
    *,
    tolerance: float = 1e-8,
) -> bool:
    if observed is None or expected is None:
        return observed is expected
    scale = max(1.0, abs(observed), abs(expected))
    return abs(observed - expected) <= tolerance * scale


def _store_unique(
    target: dict[str, dict[str, object]],
    key: str,
    payload: dict[str, object],
    label: str,
    warnings: list[str],
) -> None:
    existing = target.get(key)
    if existing is not None and existing != payload:
        warnings.append(f"conflicting {label} identity {key}")
        return
    target[key] = payload


def _snapshot_ids_by_run(output_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    manifests = output_root / "manifests"
    if not manifests.exists():
        return result
    for path in sorted(manifests.glob("*.json")):
        payload = read_json(path, {})
        if not isinstance(payload, dict):
            continue
        run_id = str(payload.get("run_id") or "")
        snapshot_id = str(payload.get("data_snapshot_id") or "")
        if run_id and snapshot_id:
            prior = result.get(run_id)
            if prior is not None and prior != snapshot_id:
                raise ValueError(f"conflicting data snapshots for run {run_id}")
            result[run_id] = snapshot_id
    return result


def _live_registry(output_root: Path) -> list[dict[str, object]]:
    payload = read_json(output_root / "state" / "strategy_registry.json", [])
    if not isinstance(payload, list):
        raise ValueError("PaperOps strategy registry is malformed")
    return [dict(row) for row in payload if isinstance(row, dict)]


def _lifecycle_row(
    order: dict[str, object],
    decision: dict[str, object],
    fill: dict[str, object],
    position: dict[str, object],
    close: dict[str, object],
    block: dict[str, object],
    status: str,
    *,
    snapshot_by_run: dict[str, str],
    live_keys: dict[tuple[str, str, str, str], dict[str, object]],
) -> dict[str, object]:
    quantity = _number(fill.get("quantity"))
    fill_price = _number(fill.get("fill_price"))
    entry_notional = (
        quantity * fill_price if quantity is not None and fill_price is not None else None
    )
    entry_fee = _number(fill.get("fee"))
    exit_fee = _number(close.get("fee"))
    entry_slippage = _number(fill.get("slippage"))
    exit_slippage = _number(close.get("slippage"))
    net_pnl = _number(close.get("net_pnl"))
    run_id = str(order.get("run_id") or decision.get("event_run_id") or "")
    fill_run_id = str(fill.get("event_run_id") or "")
    close_run_id = str(close.get("run_id") or "")
    strategy_id = str(order.get("strategy_id") or "")
    strategy_version = str(order.get("strategy_version") or "unknown")
    policy = str(order.get("execution_policy_version") or "legacy_unspecified")
    observed_fingerprint = str(
        order.get("strategy_semantics_fingerprint")
        or decision.get("strategy_semantics_fingerprint")
        or "unknown"
    )
    live_row = live_keys.get(
        (strategy_id, strategy_version, policy, observed_fingerprint),
        {},
    )
    challenger_id = str(order.get("challenger_id") or decision.get("challenger_id") or "")
    return {
        "record_id": str(order.get("order_id") or ""),
        "mode": str(order.get("mode") or ""),
        "signal_date": str(order.get("trade_date") or ""),
        "run_id": run_id,
        "fill_run_id": fill_run_id,
        "close_run_id": close_run_id,
        "data_snapshot_id": snapshot_by_run.get(run_id, "unknown"),
        "fill_data_snapshot_id": (snapshot_by_run.get(fill_run_id, "unknown") if fill else None),
        "close_data_snapshot_id": (snapshot_by_run.get(close_run_id, "unknown") if close else None),
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "strategy_status": str(
            decision.get("strategy_status") or live_row.get("strategy_status") or "unknown"
        ),
        "strategy_semantics_fingerprint": str(observed_fingerprint),
        "execution_policy_version": policy,
        "challenger_id": challenger_id,
        "series_role": ("challenger" if challenger_id else "champion" if live_row else "archived"),
        "symbol": str(order.get("symbol") or ""),
        "direction": str(order.get("direction") or ""),
        "lifecycle_status": status,
        "decision_status": str(decision.get("decision") or "accepted"),
        "decision_reason": str(decision.get("reason") or ""),
        "signal_id": str(order.get("pick_id") or ""),
        "signal_time": str(order.get("signal_time") or decision.get("signal_time") or ""),
        "setup_score": _number(decision.get("setup_score")),
        "order_id": str(order.get("order_id") or ""),
        "order_block_reason": str(block.get("reason") or ""),
        "expected_fill_rule": str(order.get("expected_fill_rule") or ""),
        "earliest_fill_date": str(order.get("earliest_fill_date") or ""),
        "entry_reference": _number(order.get("entry")),
        "stop": _number(order.get("stop")),
        "target": _number(order.get("target")),
        "quantity_requested": _number(order.get("quantity")),
        "fill_id": str(fill.get("fill_id") or ""),
        "fill_time": str(fill.get("fill_time") or ""),
        "fill_price": fill_price,
        "quantity_filled": quantity,
        "position_id": str(position.get("position_id") or close.get("position_id") or ""),
        "last_mark_price": _number(position.get("last_mark_price")),
        "unrealized_pnl": _number(position.get("unrealized_pnl")),
        "close_id": str(close.get("close_id") or ""),
        "close_time": str(close.get("close_time") or ""),
        "close_price": _number(close.get("close_price")),
        "close_reason": str(close.get("close_reason") or ""),
        "gross_pnl": _number(close.get("gross_pnl")),
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees_paid": _sum_optional(entry_fee, exit_fee),
        "entry_slippage": entry_slippage,
        "exit_slippage": exit_slippage,
        "slippage_paid": _sum_optional(entry_slippage, exit_slippage),
        "net_pnl": net_pnl,
        "r_multiple": _number(close.get("r_multiple")),
        "trade_return_pct": (
            net_pnl / entry_notional
            if net_pnl is not None and entry_notional is not None and entry_notional != 0.0
            else None
        ),
    }


def _decision_only_row(
    decision_id: str,
    decision: dict[str, object],
    status: str,
    *,
    snapshot_by_run: dict[str, str],
    live_keys: dict[tuple[str, str, str, str], dict[str, object]],
) -> dict[str, object]:
    row: dict[str, object] = {field: None for field in BLOTTER_FIELDS}
    run_id = str(decision.get("run_id") or decision.get("event_run_id") or "")
    strategy_id = str(decision.get("strategy_id") or "")
    strategy_version = str(decision.get("strategy_version") or "unknown")
    policy = str(decision.get("execution_policy_version") or "legacy_unspecified")
    observed_fingerprint = str(decision.get("strategy_semantics_fingerprint") or "unknown")
    live_row = live_keys.get(
        (strategy_id, strategy_version, policy, observed_fingerprint),
        {},
    )
    challenger_id = str(decision.get("challenger_id") or "")
    row.update(
        {
            "record_id": decision_id,
            "mode": str(decision.get("mode") or ""),
            "signal_date": str(decision.get("trade_date") or ""),
            "run_id": run_id,
            "fill_run_id": None,
            "close_run_id": None,
            "data_snapshot_id": snapshot_by_run.get(run_id, "unknown"),
            "fill_data_snapshot_id": None,
            "close_data_snapshot_id": None,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "strategy_status": str(
                decision.get("strategy_status") or live_row.get("strategy_status") or "unknown"
            ),
            "strategy_semantics_fingerprint": str(observed_fingerprint),
            "execution_policy_version": policy,
            "challenger_id": challenger_id,
            "series_role": (
                "challenger" if challenger_id else "champion" if live_row else "archived"
            ),
            "symbol": str(decision.get("symbol") or ""),
            "direction": str(decision.get("direction") or ""),
            "lifecycle_status": status,
            "decision_status": status,
            "decision_reason": str(decision.get("reason") or ""),
            "signal_id": decision_id,
            "signal_time": str(decision.get("signal_time") or ""),
            "setup_score": _number(decision.get("setup_score")),
            "entry_reference": _number(decision.get("entry_reference")),
            "stop": _number(decision.get("stop")),
            "target": _number(decision.get("target")),
        }
    )
    return row


def _payload(
    rows: list[dict[str, object]],
    warnings: list[str],
    *,
    mode: str | None = None,
    run_date: str | None = None,
) -> dict[str, object]:
    closed = [row for row in rows if row["lifecycle_status"] == "closed"]
    allocated = _sum_product(closed, "fill_price", "quantity_filled")
    net = _sum_field(closed, "net_pnl")
    champion_closed = [row for row in closed if row.get("series_role") == "champion"]
    champion_allocated = _sum_product(
        champion_closed,
        "fill_price",
        "quantity_filled",
    )
    champion_net = _sum_field(champion_closed, "net_pnl")
    series_summaries: list[dict[str, object]] = []
    series_keys = sorted(
        {
            (
                str(row.get("strategy_id") or ""),
                str(row.get("strategy_version") or "unknown"),
                str(row.get("execution_policy_version") or "legacy_unspecified"),
                str(row.get("strategy_semantics_fingerprint") or "unknown"),
                str(row.get("challenger_id") or ""),
                str(row.get("series_role") or "archived"),
            )
            for row in rows
        }
    )
    for key in series_keys:
        series_rows = [
            row
            for row in rows
            if (
                str(row.get("strategy_id") or ""),
                str(row.get("strategy_version") or "unknown"),
                str(row.get("execution_policy_version") or "legacy_unspecified"),
                str(row.get("strategy_semantics_fingerprint") or "unknown"),
                str(row.get("challenger_id") or ""),
                str(row.get("series_role") or "archived"),
            )
            == key
        ]
        series_closed = [row for row in series_rows if row.get("lifecycle_status") == "closed"]
        series_allocated = _sum_product(
            series_closed,
            "fill_price",
            "quantity_filled",
        )
        series_net = _sum_field(series_closed, "net_pnl")
        series_summaries.append(
            {
                "allocated_notional_return_pct": (
                    series_net / series_allocated if series_closed and series_allocated else None
                ),
                "challenger_id": key[4],
                "closed_trades": len(series_closed),
                "execution_policy_version": key[2],
                "row_count": len(series_rows),
                "series_role": key[5],
                "strategy_semantics_fingerprint": key[3],
                "strategy_id": key[0],
                "strategy_version": key[1],
                "total_net_pnl": series_net if series_closed else None,
            }
        )
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return {
        "artifact_id": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "mode": mode or "all",
        "account_return_pct": None,
        "capital_weighted_closed_trade_return_pct": (
            net / allocated if closed and allocated else None
        ),
        "champion_row_count": sum(1 for row in rows if row.get("series_role") == "champion"),
        "row_count": len(rows),
        "rows": rows,
        "run_date": run_date,
        "schema_version": "v2.paper_trade_blotter.v2",
        "series_summaries": series_summaries,
        "official_champion_allocated_notional_return_pct": (
            champion_net / champion_allocated if champion_closed and champion_allocated else None
        ),
        "official_champion_total_net_pnl": champion_net if champion_closed else None,
        "status": "passed" if not warnings else "failed",
        "status_counts": dict(
            sorted(Counter(str(row["lifecycle_status"]) for row in rows).items())
        ),
        "total_net_pnl": net if closed else None,
        "warnings": warnings,
    }


def _number(value: object) -> float | None:
    if value in {None, ""}:
        return None
    if isinstance(value, int | float | str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _sum_field(rows: list[dict[str, object]], field: str) -> float:
    total = 0.0
    for row in rows:
        value = _number(row.get(field))
        if value is not None:
            total += value
    return total


def _sum_product(
    rows: list[dict[str, object]],
    left_field: str,
    right_field: str,
) -> float:
    total = 0.0
    for row in rows:
        left = _number(row.get(left_field))
        right = _number(row.get(right_field))
        if left is not None and right is not None:
            total += left * right
    return total


def _sum_optional(left: float | None, right: float | None) -> float | None:
    if left is None and right is None:
        return None
    return (left or 0.0) + (right or 0.0)
