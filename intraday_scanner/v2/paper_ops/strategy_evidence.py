"""Objective strategy evidence scoring for PaperOps."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from intraday_scanner.v2.paper_ops.engine import (
    PaperOpsPaths,
    _recover_pending_transaction,
)
from intraday_scanner.v2.paper_ops.source_bar_truth import verify_source_bar_truth
from intraday_scanner.v2.paper_ops.storage import read_json, read_jsonl, write_csv, write_json

MIN_FORWARD_DAYS = 60
MIN_CLOSED_TRADES = 100
MIN_PROFIT_FACTOR = 1.2
MAX_DRAWDOWN_LIMIT = -0.08
AUTO_PAUSE_MIN_CLOSED_TRADES = 10


@dataclass(frozen=True)
class StrategyEvidenceResult:
    status: str
    scores: tuple[dict[str, object], ...]
    warnings: tuple[str, ...]
    schema_version: str = "v2.paper_ops_strategy_evidence.v2"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scores": list(self.scores),
            "status": self.status,
            "warnings": list(self.warnings),
        }


def score_strategy_evidence(
    *,
    output_root: Path = Path("data/v2_paper_ops"),
    alpha_root: Path | None = None,
) -> StrategyEvidenceResult:
    paths = PaperOpsPaths.create(output_root)
    _recover_pending_transaction(paths)
    try:
        source_truth = verify_source_bar_truth(
            output_root=output_root,
            mode="forward",
        )
    except Exception as exc:
        result = StrategyEvidenceResult(
            status="blocked",
            scores=(),
            warnings=(f"source-bar truth verification failed: {exc}",),
        )
        _write_reports(paths, result)
        return result
    if source_truth.status != "passed":
        result = StrategyEvidenceResult(
            status="blocked",
            scores=(),
            warnings=tuple(
                f"source-bar truth: {warning}" for warning in source_truth.warnings
            ),
        )
        _write_reports(paths, result)
        return result
    alpha_root = alpha_root or output_root.parent / "v2_alpha_lab"
    registry = _registry(paths)
    calendar_rows = _read_calendar(paths.calendar / "strategy_daily_returns.csv")
    events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    data_status = _forward_data_status(paths, calendar_rows)
    robustness_by_strategy = _robustness_by_strategy(alpha_root)
    commit_overlay = _commit_overlay_by_strategy()
    scores: list[dict[str, object]] = []
    for strategy in _evidence_series(registry, calendar_rows, events):
        strategy_id = str(strategy["strategy_id"])
        strategy_version = str(strategy["strategy_version"])
        execution_policy_version = str(strategy["execution_policy_version"])
        strategy_semantics_fingerprint = str(
            strategy.get("strategy_semantics_fingerprint") or "unknown"
        )
        current_series = bool(strategy.get("current_series"))
        commit_row = _exact_overlay(
            commit_overlay.get(strategy_id, {}),
            strategy_version,
            execution_policy_version,
            strategy_semantics_fingerprint,
        )
        robustness = _versioned_robustness(
            robustness_by_strategy.get(strategy_id, {}),
            strategy_version,
            strategy_semantics_fingerprint,
        )
        robustness_status = _robustness_status(strategy_id, robustness)
        robustness_blockers = _robustness_blockers(strategy_id, robustness)
        strategy_rows = [
            row
            for row in calendar_rows
            if row.get("strategy_id") == strategy_id
            and str(row.get("strategy_version") or "") == strategy_version
            and str(row.get("execution_policy_version") or "")
            == execution_policy_version
            and str(row.get("strategy_semantics_fingerprint") or "unknown")
            == strategy_semantics_fingerprint
        ]
        forward_rows = [row for row in strategy_rows if row.get("mode") == "forward"]
        replay_rows = [row for row in strategy_rows if row.get("mode") == "replay"]
        strategy_events = [
            event
            for event in events
            if _event_matches_series(
                event,
                strategy_id,
                strategy_version,
                execution_policy_version,
                strategy_semantics_fingerprint,
            )
        ]
        forward_days = len({row["date"] for row in strategy_rows if row.get("mode") == "forward"})
        replay_days = len({row["date"] for row in strategy_rows if row.get("mode") == "replay"})
        forward_closes = _closed_events(strategy_events, mode="forward")
        replay_closes = _closed_events(strategy_events, mode="replay")
        all_closes = forward_closes + replay_closes
        expectancy = _average([_to_float(item.get("net_pnl")) for item in forward_closes])
        replay_expectancy = _average([_to_float(item.get("net_pnl")) for item in replay_closes])
        profit_factor = _profit_factor(forward_closes)
        max_drawdown = _min_float(forward_rows, "drawdown_pct")
        replay_max_drawdown = _min_float(replay_rows, "drawdown_pct")
        score = _score(
            data_status=data_status,
            forward_days=forward_days,
            forward_closed=len(forward_closes),
            expectancy=expectancy,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            robustness_status=robustness_status,
        )
        promoted_status, blockers = _status(
            base_status=str(strategy.get("strategy_status", "experimental")),
            data_status=data_status,
            forward_days=forward_days,
            forward_closed=len(forward_closes),
            expectancy=expectancy,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            robustness_status=robustness_status,
            robustness_blockers=robustness_blockers,
        )
        commit_blockers = _commit_blockers(commit_row)
        if commit_blockers:
            blockers = tuple(list(blockers) + list(commit_blockers))
            if promoted_status == "validated":
                promoted_status = "watch"
        if not current_series:
            promoted_status = "archived_reporting_only"
            blockers = tuple(
                [*blockers, "archived version/policy series cannot be promoted"]
            )
        scores.append(
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                "current_series": current_series,
                "base_status": strategy.get("strategy_status", "experimental"),
                "evidence_status": promoted_status,
                "overall_score": score,
                "backtest_evidence_score": 20,
                "replay_evidence_score": min(20, replay_days * 5 + len(replay_closes)),
                "forward_evidence_score": min(25, forward_days + len(forward_closes)),
                "data_quality_score": (
                    20
                    if data_status in {"reconciled", "reconciled_with_minor_diffs"}
                    else 10
                ),
                "trade_sample_score": min(10, len(all_closes)),
                "expectancy": expectancy,
                "replay_expectancy": replay_expectancy,
                "profit_factor": profit_factor,
                "max_drawdown_pct": max_drawdown,
                "replay_max_drawdown_pct": replay_max_drawdown,
                "robustness_status": robustness_status,
                "robustness_test_return_pct": robustness.get("test_return_pct", "n/a"),
                "robustness_test_trade_count": robustness.get("test_trade_count", "n/a"),
                "robustness_warnings": _robustness_text(robustness),
                "forward_days": forward_days,
                "replay_days": replay_days,
                "forward_closed_trades": len(forward_closes),
                "replay_closed_trades": len(replay_closes),
                "committed_filltruth_forward_count": _int(
                    commit_row.get("committed_filltruth_forward_count")
                ),
                "uncommitted_overlay_count": _int(commit_row.get("uncommitted_overlay_count")),
                "rejected_filltruth_count": _int(commit_row.get("rejected_filltruth_count")),
                "approximate_fill_penalty": _int(commit_row.get("approximate_fill_penalty")),
                "intraday_supported_forward_fill_count": _int(
                    commit_row.get("intraday_supported_forward_fill_count")
                ),
                "validation_blocked_reason": str(
                    commit_row.get("validation_blocked_reason", "")
                ),
                "blockers": " | ".join(blockers),
            }
        )
    result = StrategyEvidenceResult(status="passed", scores=tuple(scores), warnings=())
    _write_reports(paths, result)
    _update_governance_overlay(paths, result.scores)
    return result


def _registry(paths: PaperOpsPaths) -> list[dict[str, object]]:
    payload = read_json(paths.state / "strategy_registry.json", [])
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _evidence_series(
    registry: list[dict[str, object]],
    calendar_rows: list[dict[str, str]],
    events: list[dict[str, object]],
) -> list[dict[str, object]]:
    by_key: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for registry_row in registry:
        key = (
            str(registry_row.get("strategy_id") or ""),
            str(registry_row.get("strategy_version") or "unknown"),
            str(
                registry_row.get("execution_policy_version")
                or "legacy_unspecified"
            ),
            str(registry_row.get("strategy_semantics_fingerprint") or "unknown"),
        )
        if not key[0]:
            continue
        by_key[key] = {**registry_row, "current_series": True}
    for calendar_row in calendar_rows:
        key = (
            str(calendar_row.get("strategy_id") or ""),
            str(calendar_row.get("strategy_version") or "unknown"),
            str(
                calendar_row.get("execution_policy_version")
                or "legacy_unspecified"
            ),
            str(calendar_row.get("strategy_semantics_fingerprint") or "unknown"),
        )
        if key[0] and key not in by_key:
            by_key[key] = {
                "strategy_id": key[0],
                "strategy_version": key[1],
                "execution_policy_version": key[2],
                "strategy_semantics_fingerprint": key[3],
                "strategy_status": "archived",
                "current_series": False,
            }
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        strategy_id = str(event.get("strategy_id") or payload.get("strategy_id") or "")
        if not strategy_id:
            continue
        key = (
            strategy_id,
            str(payload.get("strategy_version") or "unknown"),
            str(payload.get("execution_policy_version") or "legacy_unspecified"),
            str(payload.get("strategy_semantics_fingerprint") or "unknown"),
        )
        if key not in by_key:
            by_key[key] = {
                "strategy_id": key[0],
                "strategy_version": key[1],
                "execution_policy_version": key[2],
                "strategy_semantics_fingerprint": key[3],
                "strategy_status": "archived",
                "current_series": False,
            }
    return [by_key[key] for key in sorted(by_key)]


def _event_matches_series(
    event: dict[str, object],
    strategy_id: str,
    strategy_version: str,
    execution_policy_version: str,
    strategy_semantics_fingerprint: str,
) -> bool:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return False
    return (
        str(event.get("strategy_id") or payload.get("strategy_id") or "") == strategy_id
        and str(payload.get("strategy_version") or "") == strategy_version
        and str(payload.get("execution_policy_version") or "")
        == execution_policy_version
        and str(payload.get("strategy_semantics_fingerprint") or "unknown")
        == strategy_semantics_fingerprint
    )


def _exact_overlay(
    row: dict[str, object],
    strategy_version: str,
    execution_policy_version: str,
    strategy_semantics_fingerprint: str,
) -> dict[str, object]:
    if (
        str(row.get("strategy_version") or "") != strategy_version
        or str(row.get("execution_policy_version") or "")
        != execution_policy_version
        or str(row.get("strategy_semantics_fingerprint") or "")
        != strategy_semantics_fingerprint
    ):
        return {}
    return row


def _versioned_robustness(
    row: dict[str, object],
    strategy_version: str,
    strategy_semantics_fingerprint: str,
) -> dict[str, object]:
    if (
        str(row.get("strategy_version") or "") != strategy_version
        or str(row.get("strategy_semantics_fingerprint") or "")
        != strategy_semantics_fingerprint
    ):
        return {}
    return row


def _read_calendar(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _closed_events(events: list[dict[str, object]], *, mode: str) -> list[dict[str, object]]:
    closes: list[dict[str, object]] = []
    for event in events:
        if event.get("mode") != mode or event.get("event_type") != "paper_position_closed":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            closes.append(payload)
    return closes


def _forward_data_status(
    paths: PaperOpsPaths,
    calendar_rows: list[dict[str, str]],
) -> str:
    forward_rows = [
        row
        for row in calendar_rows
        if row.get("mode") == "forward"
        and str(row.get("strategy_status") or "") not in {"baseline", "benchmark"}
    ]
    if not forward_rows:
        return "unknown_no_forward_evidence"
    statuses: list[str] = []
    rows_by_date: dict[str, list[dict[str, str]]] = {}
    for row in forward_rows:
        rows_by_date.setdefault(str(row.get("date") or ""), []).append(row)
    for run_date, rows in sorted(rows_by_date.items()):
        payload = read_json(paths.exports / f"preflight_forward_{run_date}.json", {})
        if not isinstance(payload, dict) or not payload:
            return "unknown_missing_forward_preflight"
        snapshot_ids = {
            str(row.get("data_snapshot_id") or "") for row in rows
        }
        if snapshot_ids != {str(payload.get("data_snapshot_id") or "")}:
            return "unknown_forward_snapshot_mismatch"
        status = str(payload.get("reconciliation_status") or "unknown_unverified")
        statuses.append(status)
    accepted = {"reconciled", "reconciled_with_minor_diffs"}
    if any(status not in accepted for status in statuses):
        return next(status for status in statuses if status not in accepted)
    if "reconciled_with_minor_diffs" in statuses:
        return "reconciled_with_minor_diffs"
    return "reconciled"


def _robustness_by_strategy(root: Path) -> dict[str, dict[str, object]]:
    payload = read_json(root / "reports" / "robustness_summary.json", {})
    rows = payload.get("rows") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return {}
    return {str(row.get("strategy_id")): row for row in rows if isinstance(row, dict)}


def _commit_overlay_by_strategy(
    root: Path = Path("data/v2_forward_evidence/strategy_evidence"),
) -> dict[str, dict[str, object]]:
    payload = read_json(root / "evidence_commit_strategy_overlay.json", {})
    rows = payload.get("rows") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return {}
    return {str(row.get("strategy_id")): row for row in rows if isinstance(row, dict)}


def _commit_blockers(row: dict[str, object]) -> tuple[str, ...]:
    if not row:
        return ("FillTruth CommitBridge evidence is missing",)
    blockers: list[str] = []
    if _int(row.get("uncommitted_overlay_count")) > 0:
        blockers.append("FillTruth evidence exists only as uncommitted overlay")
    if _int(row.get("rejected_filltruth_count")) > 0:
        blockers.append("Rejected FillTruth evidence cannot support validation")
    if _int(row.get("committed_filltruth_forward_count")) < 1:
        blockers.append("No committed FillTruth forward fills yet")
    return tuple(blockers)


def _robustness_status(strategy_id: str, row: dict[str, object]) -> str:
    if not row:
        return "unknown"
    if _is_benchmark_or_baseline(strategy_id, row):
        return str(row.get("robustness_status", "non_candidate"))
    return str(row.get("robustness_status", "unknown"))


def _robustness_blockers(strategy_id: str, row: dict[str, object]) -> tuple[str, ...]:
    if not row or _is_benchmark_or_baseline(strategy_id, row):
        return ()
    status = str(row.get("robustness_status", "unknown"))
    warnings = _robustness_text(row)
    if status == "fragile":
        return (f"Alpha Lab robustness status is fragile: {warnings}",)
    if status == "insufficient_oos_trades":
        return ("Alpha Lab robustness has insufficient out-of-sample trades",)
    return ()


def _robustness_text(row: dict[str, object]) -> str:
    if not row:
        return "No robustness row available."
    return str(row.get("warnings", "none"))


def _is_benchmark_or_baseline(strategy_id: str, row: dict[str, object]) -> bool:
    status = str(row.get("status", "")).lower()
    return (
        status in {"benchmark", "baseline"}
        or strategy_id.startswith("benchmark_")
        or strategy_id.startswith("cash_")
    )


def _score(
    *,
    data_status: str,
    forward_days: int,
    forward_closed: int,
    expectancy: float | None,
    profit_factor: float | None,
    max_drawdown: float | None,
    robustness_status: str,
) -> int:
    score = 0
    score += 20 if data_status in {"reconciled", "reconciled_with_minor_diffs"} else 10
    score += min(25, forward_days)
    score += min(20, forward_closed)
    score += 15 if expectancy is not None and expectancy > 0 else 0
    score += (
        10
        if profit_factor is not None
        and profit_factor >= MIN_PROFIT_FACTOR
        else 0
    )
    score += (
        10
        if max_drawdown is not None
        and max_drawdown >= MAX_DRAWDOWN_LIMIT
        else 0
    )
    if robustness_status == "fragile":
        score = max(0, score - 20)
    elif robustness_status == "insufficient_oos_trades":
        score = max(0, score - 5)
    return score


def _status(
    *,
    base_status: str,
    data_status: str,
    forward_days: int,
    forward_closed: int,
    expectancy: float | None,
    profit_factor: float | None,
    max_drawdown: float | None,
    robustness_status: str,
    robustness_blockers: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    blockers: list[str] = []
    allowed_data_statuses = {
        "reconciled",
        "reconciled_with_minor_diffs",
        "single_provider_unreconciled",
    }
    if data_status not in allowed_data_statuses:
        blockers.append(f"data status {data_status} blocks promotion")
    if forward_days < MIN_FORWARD_DAYS:
        blockers.append(f"needs {MIN_FORWARD_DAYS - forward_days} more forward paper days")
    if forward_closed < MIN_CLOSED_TRADES:
        blockers.append(f"needs {MIN_CLOSED_TRADES - forward_closed} more forward closed trades")
    if expectancy is None:
        blockers.append("forward expectancy is unavailable without closed trades")
    elif expectancy <= 0:
        blockers.append("forward expectancy is not positive after costs")
    if profit_factor is None or profit_factor < MIN_PROFIT_FACTOR:
        blockers.append("profit factor below threshold")
    if max_drawdown is None:
        blockers.append("forward drawdown is unavailable")
    elif max_drawdown < MAX_DRAWDOWN_LIMIT:
        blockers.append("drawdown exceeds threshold")
    blockers.extend(robustness_blockers)
    if robustness_status == "fragile":
        return "quarantined", tuple(blockers)
    if forward_closed and expectancy is not None and expectancy < 0:
        return "quarantined", tuple(blockers)
    if blockers:
        status = "watch" if base_status in {"experimental", "candidate"} else "probation"
        return status, tuple(blockers)
    return (
        "watch",
        (
            "strict promotion packet with 98% coverage, benchmark/cash excess, "
            "concentration, walk-forward, holdout, 1.5x slippage stress, "
            "no-lookahead, reconciliation, and operator review is required",
        ),
    )


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _profit_factor(closes: list[dict[str, object]]) -> float | None:
    if not closes:
        return None
    wins = sum(
        _to_float(item.get("net_pnl"))
        for item in closes
        if _to_float(item.get("net_pnl")) > 0
    )
    losses = abs(
        sum(
            _to_float(item.get("net_pnl"))
            for item in closes
            if _to_float(item.get("net_pnl")) < 0
        )
    )
    if losses == 0:
        return 1_000_000.0 if wins else None
    return wins / losses


def _min_float(
    rows: list[dict[str, str]],
    field: str,
) -> float | None:
    values = [float(row.get(field, "0") or 0.0) for row in rows]
    return min(values) if values else None


def _to_float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, str | int | float):
        return float(value)
    return 0.0


def _int(value: object) -> int:
    return int(_to_float(value))


def _write_reports(paths: PaperOpsPaths, result: StrategyEvidenceResult) -> None:
    write_json(paths.reports / "strategy_evidence_scores.json", result.to_dict())
    fields = (
        "strategy_id",
        "strategy_version",
        "execution_policy_version",
        "current_series",
        "base_status",
        "evidence_status",
        "overall_score",
        "forward_days",
        "forward_closed_trades",
        "committed_filltruth_forward_count",
        "uncommitted_overlay_count",
        "rejected_filltruth_count",
        "approximate_fill_penalty",
        "intraday_supported_forward_fill_count",
        "validation_blocked_reason",
        "replay_days",
        "replay_closed_trades",
        "expectancy",
        "profit_factor",
        "max_drawdown_pct",
        "replay_max_drawdown_pct",
        "robustness_status",
        "robustness_test_return_pct",
        "robustness_test_trade_count",
        "robustness_warnings",
        "blockers",
    )
    write_csv(paths.reports / "strategy_evidence_scores.csv", list(result.scores), fields)
    lines = [
        "# PaperOps Strategy Evidence",
        "",
        "No strategy is promoted to validated unless all explicit gates pass.",
        "",
        "## Scores",
        "",
    ]
    for row in result.scores:
        lines.append(
            f"- `{row['strategy_id']}@{row['strategy_version']}` under "
            f"`{row['execution_policy_version']}` status `{row['evidence_status']}`, "
            f"robustness `{row.get('robustness_status', 'unknown')}`, "
            f"score `{row['overall_score']}`, blockers: {row['blockers'] or 'none'}"
        )
    (paths.reports / "strategy_evidence_summary.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _update_governance_overlay(
    paths: PaperOpsPaths,
    scores: tuple[dict[str, object], ...],
) -> None:
    overlay_path = paths.state / "strategy_governance_overlay.json"
    payload = read_json(overlay_path, {})
    existing = payload.get("entries", []) if isinstance(payload, dict) else []
    if not isinstance(existing, list) or not all(isinstance(row, dict) for row in existing):
        raise ValueError("PaperOps strategy governance overlay is malformed")
    by_key = {
        (
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
            str(row.get("execution_policy_version") or ""),
            str(row.get("strategy_semantics_fingerprint") or "unknown"),
        ): dict(row)
        for row in existing
    }
    changed = False
    for score in scores:
        if (
            score.get("current_series") is not True
            or str(score.get("evidence_status") or "") != "quarantined"
            or _int(score.get("forward_closed_trades")) < AUTO_PAUSE_MIN_CLOSED_TRADES
        ):
            continue
        key = (
            str(score.get("strategy_id") or ""),
            str(score.get("strategy_version") or ""),
            str(score.get("execution_policy_version") or ""),
            str(score.get("strategy_semantics_fingerprint") or "unknown"),
        )
        prior = by_key.get(key, {})
        paused = {
            **prior,
            "allow_entries": False,
            "execution_policy_version": key[2],
            "strategy_semantics_fingerprint": key[3],
            "paused_at": prior.get("paused_at")
            or datetime.now(timezone.utc).isoformat(),
            "reason": str(score.get("blockers") or "negative after-cost evidence"),
            "source": "strategy_evidence",
            "strategy_id": key[0],
            "strategy_version": key[1],
        }
        if paused != prior:
            by_key[key] = paused
            changed = True
    if changed or not overlay_path.exists():
        write_json(
            overlay_path,
            {
                "entries": [by_key[key] for key in sorted(by_key)],
                "schema_version": "v2.strategy_governance_overlay.v1",
            },
        )
