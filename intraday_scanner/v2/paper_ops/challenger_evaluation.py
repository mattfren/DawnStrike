"""Deterministic, forward-only PaperOps challenger evidence evaluation.

This module is deliberately governance-only.  It reads retained PaperOps truth,
writes evaluation artifacts, and never mutates strategy code, the strategy
registry, account state, orders, positions, or execution policy.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from intraday_scanner.v2.paper_ops import engine as paper_engine
from intraday_scanner.v2.paper_ops.observer_safety import PaperOpsObserverBlocked
from intraday_scanner.v2.paper_ops.source_bar_truth import verify_source_bar_truth
from intraday_scanner.v2.paper_ops.storage import (
    append_jsonl_unique,
    read_json,
    read_jsonl,
    write_csv,
    write_json,
)

JsonDict = dict[str, Any]

_SCHEMA_VERSION = "v2.paper_ops_challenger_evaluation.v1"
_CHALLENGER_REGISTRY_SCHEMA = "v2.paper_ops_challenger_registry.v1"
_OPERATOR_PROCESS_SCHEMA = "v2.paper_ops_operator_promotion_process.v1"
_REFERENCE_IDS = {
    "benchmark": (
        "benchmark_buy_hold_equal_weight",
        "v1.0",
        "equal_weight_close_to_close_v1",
    ),
    "cash": ("cash_no_trade_baseline", "v1.0", "cash_zero_interest_v1"),
}
_ALLOWED_DECISIONS = {"accepted", "rejected", "skipped", "no_setup"}
_NUMERIC_CALENDAR_FIELDS = (
    "starting_equity",
    "ending_equity",
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "daily_return_pct",
    "cumulative_return_pct",
    "drawdown_pct",
    "trades_opened",
    "trades_closed",
    "wins",
    "losses",
    "flats",
    "fees_paid",
    "slippage_estimate",
)


@dataclass(frozen=True)
class ChallengerEvaluationConfig:
    """Fail-closed evidence and performance gates.

    Return fields are fractions of strategy equity (``0.01`` means 1%).
    """

    min_forward_sessions: int = 60
    min_closed_trades: int = 100
    min_coverage_pct: float = 98.0
    holdout_fraction: float = 0.30
    min_holdout_sessions: int = 10
    min_walk_forward_folds: int = 3
    min_sessions_per_fold: int = 5
    min_positive_walk_forward_ratio: float = 2.0 / 3.0
    min_profit_factor: float = 1.20
    max_gain_loss_concentration_pct: float = 25.0
    slippage_stress_multiplier: float = 1.50
    max_drawdown_pct: float = -8.0
    max_drawdown_worsening_pct: float = 0.0
    max_win_rate_decline_pct_points: float = 5.0
    min_excess_return_vs_champion_pct: float = 0.0
    require_benchmark: bool = True
    require_cash: bool = True
    require_truth_audits: bool = True

    @classmethod
    def from_mapping(cls, payload: dict[str, object]) -> ChallengerEvaluationConfig:
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown challenger evaluation config fields: {unknown}")
        result = cls(**payload)  # type: ignore[arg-type]
        result.validate()
        return result

    def validate(self) -> None:
        if self.min_forward_sessions < 1 or self.min_closed_trades < 0:
            raise ValueError("minimum forward sessions must be positive and trades non-negative")
        if self.min_forward_sessions < 60 or self.min_closed_trades < 100:
            raise ValueError(
                "promotion evidence requires at least 60 forward sessions "
                "and 100 closed trades"
            )
        if not 0.0 < self.min_coverage_pct <= 100.0:
            raise ValueError("min_coverage_pct must be in (0, 100]")
        if self.min_coverage_pct < 98.0:
            raise ValueError("promotion truth coverage cannot be below 98%")
        if not 0.0 < self.holdout_fraction < 1.0:
            raise ValueError("holdout_fraction must be in (0, 1)")
        if self.min_holdout_sessions < 1:
            raise ValueError("min_holdout_sessions must be positive")
        if self.min_walk_forward_folds < 1 or self.min_sessions_per_fold < 1:
            raise ValueError("walk-forward fold counts and sizes must be positive")
        if not 0.0 <= self.min_positive_walk_forward_ratio <= 1.0:
            raise ValueError("min_positive_walk_forward_ratio must be in [0, 1]")
        if self.min_profit_factor < 1.20:
            raise ValueError("promotion profit factor cannot be below 1.20")
        if self.max_gain_loss_concentration_pct > 25.0:
            raise ValueError(
                "promotion gain/loss concentration cannot exceed 25%"
            )
        if self.slippage_stress_multiplier < 1.50:
            raise ValueError("promotion slippage stress cannot be below 1.5x")
        if self.max_drawdown_pct < -8.0:
            raise ValueError("promotion drawdown limit cannot exceed 8%")
        required = (
            self.min_holdout_sessions
            + self.min_walk_forward_folds * self.min_sessions_per_fold
        )
        if self.min_forward_sessions < required:
            raise ValueError(
                "min_forward_sessions must cover the holdout and required walk-forward folds"
            )


@dataclass(frozen=True)
class _SeriesKey:
    strategy_id: str
    strategy_version: str
    execution_policy_version: str
    strategy_semantics_fingerprint: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class _SessionEvidence:
    session_date: str
    row: JsonDict
    closes: tuple[JsonDict, ...]
    fills: tuple[JsonDict, ...]


@dataclass(frozen=True)
class _SeriesEvidence:
    key: _SeriesKey
    expected_dates: tuple[str, ...]
    eligible: tuple[_SessionEvidence, ...]
    exclusions: dict[str, tuple[str, ...]]


def evaluate_paperops_challengers(
    *,
    output_root: Path = Path("data/v2_paper_ops_live"),
    config: ChallengerEvaluationConfig | None = None,
) -> JsonDict:
    """Evaluate registered shadow challengers without changing production state."""

    state = output_root / "state"
    reports = output_root / "reports"
    effective_config = config or _load_config(state)
    effective_config.validate()

    champions, registry_warnings = _champions(state)
    champion_inceptions, inception_reasons = _champion_coverage_inceptions(
        output_root,
        champions,
    )
    challengers, challenger_warnings = _challenger_registry(state)
    calendar_rows = _read_csv(output_root / "calendar" / "strategy_daily_returns.csv")
    ledger_events = read_jsonl(output_root / "ledger" / "paper_ledger.jsonl")
    universe = _configured_universe(state)
    completed_reports, report_warnings = _completed_forward_reports(reports / "daily")
    source_truth_reasons: tuple[str, ...]
    try:
        source_truth = verify_source_bar_truth(
            output_root=output_root,
            mode="forward",
        )
        source_truth_reasons = (
            ()
            if source_truth.status == "passed"
            else tuple(f"source_bar_truth: {warning}" for warning in source_truth.warnings)
        )
    except Exception as exc:
        source_truth_reasons = (f"source_bar_truth verification error: {exc}",)
    truth_sources = _authoritative_truth_sources(output_root)
    truth_reasons = (
        *source_truth_reasons,
        *_truth_audit_reasons(
            output_root,
            effective_config,
            source_paths=truth_sources,
        ),
    )
    blotter_rows, blotter_reasons = _verified_blotter_rows(output_root)
    operator_process_status, operator_process_reasons = _operator_process_status(state)

    calendar_index = _calendar_index(calendar_rows)
    expected_dates = tuple(sorted(completed_reports))
    proposals: list[JsonDict] = []
    registered_by_strategy: dict[str, list[JsonDict]] = {}
    for challenger in challengers:
        registered_by_strategy.setdefault(str(challenger.get("strategy_id") or ""), []).append(
            challenger
        )

    champion_ids = {champion.strategy_id for champion in champions}
    warnings = [
        *registry_warnings,
        *challenger_warnings,
        *report_warnings,
        *(f"truth audit: {reason}" for reason in truth_reasons),
    ]
    unknown_registration_reasons = [
        f"challenger registration references unknown strategy: {strategy_id}"
        for strategy_id in sorted(set(registered_by_strategy) - champion_ids)
        if strategy_id
    ]
    warnings.extend(
        f"challenger registration references unknown strategy: {strategy_id}"
        for strategy_id in sorted(set(registered_by_strategy) - champion_ids)
        if strategy_id
    )

    evidence_cache: dict[tuple[_SeriesKey, str | None, str | None], _SeriesEvidence] = {}

    def evidence(
        key: _SeriesKey,
        frozen_after: str | None,
        candidate_registration: JsonDict | None = None,
    ) -> _SeriesEvidence:
        challenger_id = (
            str(candidate_registration.get("challenger_id") or "")
            if candidate_registration
            else None
        )
        cache_key = (key, frozen_after, challenger_id)
        if cache_key not in evidence_cache:
            series_expected_dates = expected_dates
            if candidate_registration is None:
                inception = champion_inceptions.get(key)
                series_expected_dates = tuple(
                    session_date
                    for session_date in expected_dates
                    if inception is not None and session_date >= inception
                )
            evidence_cache[cache_key] = _series_evidence(
                output_root=output_root,
                key=key,
                expected_dates=series_expected_dates,
                completed_reports=completed_reports,
                calendar_index=calendar_index,
                ledger_events=ledger_events,
                universe=universe,
                champion_inceptions=champion_inceptions,
                global_truth_reasons=truth_reasons,
                blotter_rows=blotter_rows,
                blotter_reasons=blotter_reasons,
                frozen_after=frozen_after,
                candidate_registration=candidate_registration,
            )
        return evidence_cache[cache_key]

    for champion in champions:
        registrations = sorted(
            registered_by_strategy.get(champion.strategy_id, []),
            key=lambda row: str(row.get("challenger_id") or ""),
        )
        if not registrations:
            champion_evidence = evidence(champion, None)
            proposals.append(
                _no_challenger_proposal(
                    champion,
                    champion_evidence,
                    effective_config,
                    operator_process_status,
                    operator_process_reasons,
                )
            )
            continue
        for registration in registrations:
            proposals.append(
                _evaluate_registration(
                    output_root=output_root,
                    champion=champion,
                    registration=registration,
                    get_evidence=evidence,
                    calendar_index=calendar_index,
                    completed_reports=completed_reports,
                    config=effective_config,
                    operator_process_status=operator_process_status,
                    operator_process_reasons=operator_process_reasons,
                )
            )

    ignored_series = _unregistered_series(calendar_index, champions, challengers)
    warnings.extend(f"unregistered forward series ignored: {item}" for item in ignored_series)
    proposals.sort(key=lambda row: (str(row["strategy_id"]), str(row.get("challenger_id") or "")))

    evidence_as_of = max(expected_dates, default=None)
    proposal_integrity_reasons = _proposal_integrity_reasons(proposals)
    operational_reasons = [
        *truth_reasons,
        *blotter_reasons,
        *registry_warnings,
        *inception_reasons,
        *challenger_warnings,
        *unknown_registration_reasons,
        *proposal_integrity_reasons,
    ]
    if not champions:
        operational_reasons.append("no exact champion strategies are registered")
    if not expected_dates:
        operational_reasons.append("no completed forward close reports are retained")
    base_payload: JsonDict = {
        "schema_version": _SCHEMA_VERSION,
        "status": "failed" if operational_reasons else "passed",
        "operational_blockers": sorted(dict.fromkeys(operational_reasons)),
        "evidence_as_of": evidence_as_of,
        "source_mode": "forward_only",
        "research_only": True,
        "broker_execution_allowed": False,
        "automatic_promotion_enabled": False,
        "operator_process_status": operator_process_status,
        "config": asdict(effective_config),
        "champion_count": len(champions),
        "registered_challenger_count": len(challengers),
        "completed_forward_session_count": len(expected_dates),
        "authoritative_truth_input_sha256": _path_set_fingerprint(
            output_root,
            truth_sources,
        ),
        "verified_trade_blotter_sha256": (
            _stable_digest(blotter_rows) if not blotter_reasons else None
        ),
        "proposals": proposals,
        "warnings": sorted(dict.fromkeys(warnings)),
    }
    evaluation_id = _stable_digest(base_payload)
    payload = {"evaluation_id": evaluation_id, **base_payload}
    artifacts = _write_artifacts(reports, payload)
    append_jsonl_unique(
        reports / "challenger_evaluation_history.jsonl",
        [payload],
        "evaluation_id",
    )
    return {**payload, "artifacts": artifacts}


def _proposal_integrity_reasons(proposals: list[JsonDict]) -> tuple[str, ...]:
    markers = (
        "hash mismatch",
        "sha256 mismatch",
        "cross-series contamination",
        "unmatched or cross-series contamination",
        "duplicate canonical",
        "conflicting frozen run lineage",
    )
    reasons: list[str] = []
    for proposal in proposals:
        excluded = proposal.get("excluded_dates")
        if not isinstance(excluded, dict):
            continue
        for series, by_date in excluded.items():
            if not isinstance(by_date, dict):
                continue
            for session_date, raw_reasons in by_date.items():
                if not isinstance(raw_reasons, list | tuple):
                    continue
                for reason in raw_reasons:
                    text = str(reason)
                    if any(marker in text for marker in markers):
                        reasons.append(
                            f"{series} evidence integrity failed on {session_date}: {text}"
                        )
    return tuple(sorted(dict.fromkeys(reasons)))


def _load_config(state: Path) -> ChallengerEvaluationConfig:
    payload = read_json(state / "challenger_evaluation_config.json", {})
    if not isinstance(payload, dict) or not payload:
        return ChallengerEvaluationConfig()
    return ChallengerEvaluationConfig.from_mapping(payload)


def _champions(state: Path) -> tuple[list[_SeriesKey], list[str]]:
    payload = read_json(state / "strategy_registry.json", [])
    rows = payload if isinstance(payload, list) else []
    champions: list[_SeriesKey] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            warnings.append("non-object strategy registry row ignored")
            continue
        strategy_id = str(raw.get("strategy_id") or "").strip()
        status = str(raw.get("strategy_status") or "").strip().lower()
        if status in {"benchmark", "baseline"} or strategy_id.startswith(("benchmark_", "cash_")):
            continue
        key = _SeriesKey(
            strategy_id=strategy_id,
            strategy_version=str(raw.get("strategy_version") or "").strip(),
            execution_policy_version=str(raw.get("execution_policy_version") or "").strip(),
            strategy_semantics_fingerprint=str(
                raw.get("strategy_semantics_fingerprint") or ""
            ).strip(),
        )
        if not all(key.to_dict().values()):
            warnings.append(f"incomplete champion identity ignored: {strategy_id or 'unknown'}")
            continue
        if strategy_id in seen:
            warnings.append(f"duplicate champion strategy id ignored: {strategy_id}")
            continue
        champions.append(key)
        seen.add(strategy_id)
    champions.sort(key=lambda item: item.strategy_id)
    return champions, warnings


def _champion_coverage_inceptions(
    output_root: Path,
    champions: list[_SeriesKey],
) -> tuple[dict[_SeriesKey, str], tuple[str, ...]]:
    """Resolve fail-closed forward inception for every exact champion series."""

    paths = paper_engine.PaperOpsPaths.create(output_root)
    inceptions: dict[_SeriesKey, str] = {}
    reasons: list[str] = []
    for champion in champions:
        try:
            inceptions[champion] = paper_engine._strategy_coverage_inception(
                paths,
                strategy_id=champion.strategy_id,
                strategy_version=champion.strategy_version,
                execution_policy_version=champion.execution_policy_version,
                strategy_semantics_fingerprint=(
                    champion.strategy_semantics_fingerprint
                ),
            ).isoformat()
        except (OSError, TypeError, ValueError) as exc:
            reasons.append(
                "champion coverage inception is unavailable for "
                f"{champion.strategy_id}@{champion.strategy_version}: {exc}"
            )
    return inceptions, tuple(sorted(dict.fromkeys(reasons)))


def _challenger_registry(state: Path) -> tuple[list[JsonDict], list[str]]:
    path = state / "strategy_challenger_registry.json"
    payload = read_json(path, {})
    if payload == {}:
        return [], ["strategy challenger registry is missing; no challenger was invented"]
    if isinstance(payload, dict):
        if payload.get("schema_version") != _CHALLENGER_REGISTRY_SCHEMA:
            return [], ["strategy challenger registry schema is missing or unsupported"]
        rows = payload.get("challengers")
    else:
        rows = None
    if not isinstance(rows, list):
        return [], ["strategy challenger registry rows are missing"]
    warnings = [
        f"strategy challenger registry row {index} is not an object"
        for index, row in enumerate(rows)
        if not isinstance(row, dict)
    ]
    return [dict(row) for row in rows if isinstance(row, dict)], warnings


def _configured_universe(state: Path) -> tuple[str, ...]:
    payload = read_json(state / "paper_ops_config.json", {})
    if not isinstance(payload, dict):
        return ()
    symbols = payload.get("universe_symbols")
    if not isinstance(symbols, list):
        return ()
    return tuple(sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}))


def _completed_forward_reports(path: Path) -> tuple[dict[str, JsonDict], list[str]]:
    by_date: dict[str, list[JsonDict]] = {}
    warnings: list[str] = []
    if not path.exists():
        return {}, ["no completed forward close reports are retained"]
    for report_path in sorted(path.glob("*.json")):
        payload = read_json(report_path, {})
        if not isinstance(payload, dict) or payload.get("mode") != "forward":
            continue
        stats = payload.get("stats")
        if not isinstance(stats, dict) or stats.get("phase") != "close":
            continue
        session_date = str(payload.get("date") or "")[:10]
        try:
            date.fromisoformat(session_date)
        except ValueError:
            warnings.append(f"invalid forward close report date ignored: {report_path.name}")
            continue
        by_date.setdefault(session_date, []).append(payload)
    completed: dict[str, JsonDict] = {}
    for session_date, rows in sorted(by_date.items()):
        canonical = {_stable_json(row) for row in rows}
        if len(canonical) != 1:
            warnings.append(f"conflicting forward close reports exclude {session_date}")
            continue
        completed[session_date] = rows[0]
    if not completed:
        warnings.append("no completed forward close reports are retained")
    return completed, warnings


def _truth_audit_reasons(
    output_root: Path,
    config: ChallengerEvaluationConfig,
    *,
    source_paths: tuple[Path, ...],
) -> tuple[str, ...]:
    pending_journal = output_root / "state" / "paper_transaction_pending.json"
    if pending_journal.exists():
        return ("PaperOps transaction journal is pending recovery",)
    if not config.require_truth_audits:
        return ()
    newest_source = max(
        (path.stat().st_mtime_ns for path in source_paths if path.exists()),
        default=0,
    )
    required = (
        ("reconciliation", output_root / "reconciliation" / "reconciliation_latest.json"),
        ("calendar_truth", output_root / "reconciliation" / "calendar_truth_latest.json"),
        ("ledger_rebuild", output_root / "reconciliation" / "ledger_rebuild_latest.json"),
        (
            "source_bar_truth",
            output_root / "reconciliation" / "source_bar_truth_forward_latest.json",
        ),
    )
    reasons: list[str] = []
    for label, path in required:
        payload = read_json(path, {})
        if not isinstance(payload, dict) or payload.get("status") != "passed":
            reasons.append(f"{label} audit is missing or not passed")
            continue
        if path.stat().st_mtime_ns < newest_source:
            reasons.append(
                f"{label} audit is stale relative to authoritative retained evidence"
            )
    return tuple(reasons)


def _authoritative_truth_sources(output_root: Path) -> tuple[Path, ...]:
    """Return every mutable input that can change evaluation eligibility or economics."""

    fixed = (
        output_root / "calendar" / "strategy_daily_returns.csv",
        output_root / "ledger" / "paper_ledger.jsonl",
        output_root / "state" / "paper_ops_config.json",
        output_root / "state" / "execution_policy_manifest.json",
        output_root / "state" / "strategy_registry.json",
        output_root / "state" / "strategy_semantics_manifest.json",
        output_root / "state" / "strategy_challenger_registry.json",
        output_root / "state" / "shadow_registration_ledger.jsonl",
        output_root / "state" / "challenger_evaluation_config.json",
        output_root / "state" / "audited_operator_promotion_process.json",
        output_root / "state" / "paper_accounts.json",
        output_root / "state" / "pending_orders.json",
        output_root / "state" / "open_positions.json",
        output_root / "state" / "paper_transaction_pending.json",
    )
    patterns = (
        "state/*_paper_accounts.json",
        "state/*_pending_orders.json",
        "state/*_open_positions.json",
        "state/shadow/**/*.json",
        "manifests/*.json",
        "reports/daily/*.json",
        "exports/preflight_*.json",
        "exports/strategy_decisions_*.json",
        "exports/shadow_strategy_decisions_*.json",
        "exports/shadow_picks_*.json",
        "exports/shadow_order_decisions_*.json",
    )
    paths = {path for path in fixed if path.exists()}
    operator_payload = read_json(
        output_root / "state" / "audited_operator_promotion_process.json",
        {},
    )
    if isinstance(operator_payload, dict):
        raw_audit_path = str(operator_payload.get("audit_artifact_path") or "").strip()
        if raw_audit_path:
            candidate = (output_root / raw_audit_path).resolve()
            try:
                candidate.relative_to(output_root.resolve())
            except ValueError:
                pass
            else:
                if candidate.is_file():
                    paths.add(candidate)
    for pattern in patterns:
        paths.update(path for path in output_root.glob(pattern) if path.is_file())
    return tuple(sorted(paths, key=lambda path: path.as_posix()))


def _path_set_fingerprint(output_root: Path, paths: tuple[Path, ...]) -> str:
    entries: list[JsonDict] = []
    for path in paths:
        try:
            relative = path.relative_to(output_root).as_posix()
        except ValueError:
            relative = path.as_posix()
        entries.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return _stable_digest(entries)


def _verified_blotter_rows(
    output_root: Path,
) -> tuple[list[JsonDict], tuple[str, ...]]:
    """Bind evaluator economics to the deterministic lifecycle verifier."""

    if (output_root / "state" / "paper_transaction_pending.json").exists():
        return [], ("trade blotter blocked by pending PaperOps transaction journal",)
    try:
        from intraday_scanner.v2.paper_ops.trade_blotter import _materialize_rows

        materialized, warnings = _materialize_rows(output_root)
    except (OSError, PaperOpsObserverBlocked, TypeError, ValueError) as exc:
        return [], (f"trade blotter deterministic rebuild failed: {exc}",)
    reasons = [f"trade blotter lifecycle verification: {item}" for item in warnings]
    stored = read_json(output_root / "exports" / "paper_trade_blotter.json", {})
    if not isinstance(stored, dict) or stored.get("status") != "passed":
        reasons.append("stored trade blotter is missing or not passed")
    elif stored.get("rows") != materialized:
        reasons.append("stored trade blotter differs from current deterministic lifecycle rebuild")
    verification_path = (
        output_root / "reconciliation" / "trade_blotter_verify_latest.json"
    )
    verification = read_json(verification_path, {})
    if not isinstance(verification, dict) or verification.get("status") != "passed":
        reasons.append("trade blotter verification is missing or not passed")
    elif _integer(verification.get("row_count")) != len(materialized):
        reasons.append("trade blotter verification row count is stale")
    blotter_inputs = tuple(
        path
        for path in (
            output_root / "ledger" / "paper_ledger.jsonl",
            output_root / "state" / "paper_ops_config.json",
            output_root / "state" / "strategy_registry.json",
            *sorted((output_root / "manifests").glob("*.json")),
        )
        if path.exists()
    )
    newest_input = max(
        (path.stat().st_mtime_ns for path in blotter_inputs),
        default=0,
    )
    if verification_path.exists() and verification_path.stat().st_mtime_ns < newest_input:
        reasons.append("trade blotter verification is stale relative to lifecycle inputs")
    return [dict(row) for row in materialized], tuple(sorted(dict.fromkeys(reasons)))


def _operator_process_status(state: Path) -> tuple[str, tuple[str, ...]]:
    payload = read_json(state / "audited_operator_promotion_process.json", {})
    if not isinstance(payload, dict) or not payload:
        return "missing", ("audited manual operator promotion process is missing",)
    reasons: list[str] = []
    if payload.get("schema_version") != _OPERATOR_PROCESS_SCHEMA:
        reasons.append("operator process schema is unsupported")
    if payload.get("status") != "active":
        reasons.append("operator process is not active")
    if payload.get("review_mode") != "manual_only":
        reasons.append("operator process is not manual-only")
    if payload.get("automatic_promotion_allowed") is not False:
        reasons.append("operator process does not explicitly prohibit automatic promotion")
    for field in (
        "process_id",
        "approved_by",
        "approved_at",
        "audit_artifact_path",
        "audit_artifact_sha256",
    ):
        if not str(payload.get(field) or "").strip():
            reasons.append(f"operator process is missing {field}")
    audit_hash = str(payload.get("audit_artifact_sha256") or "")
    if audit_hash and not _is_sha256(audit_hash):
        reasons.append("operator process audit_artifact_sha256 is invalid")
    raw_audit_path = str(payload.get("audit_artifact_path") or "").strip()
    if raw_audit_path:
        audit_path = (state.parent / raw_audit_path).resolve()
        try:
            audit_path.relative_to(state.parent.resolve())
        except ValueError:
            reasons.append("operator process audit artifact path escapes PaperOps root")
        else:
            if not audit_path.is_file():
                reasons.append("operator process audit artifact is missing")
            elif audit_hash and hashlib.sha256(audit_path.read_bytes()).hexdigest() != audit_hash:
                reasons.append("operator process audit artifact content hash mismatch")
    return ("invalid", tuple(reasons)) if reasons else ("audited_manual_only", ())


def _calendar_index(
    rows: list[JsonDict],
) -> dict[tuple[str, _SeriesKey], list[JsonDict]]:
    index: dict[tuple[str, _SeriesKey], list[JsonDict]] = {}
    for row in rows:
        if str(row.get("mode") or "").lower() != "forward":
            continue
        key = _SeriesKey(
            strategy_id=str(row.get("strategy_id") or "").strip(),
            strategy_version=str(row.get("strategy_version") or "").strip(),
            execution_policy_version=str(row.get("execution_policy_version") or "").strip(),
            strategy_semantics_fingerprint=str(
                row.get("strategy_semantics_fingerprint") or ""
            ).strip(),
        )
        session_date = str(row.get("date") or "")[:10]
        index.setdefault((session_date, key), []).append(row)
    return index


def _series_evidence(
    *,
    output_root: Path,
    key: _SeriesKey,
    expected_dates: tuple[str, ...],
    completed_reports: dict[str, JsonDict],
    calendar_index: dict[tuple[str, _SeriesKey], list[JsonDict]],
    ledger_events: list[JsonDict],
    universe: tuple[str, ...],
    champion_inceptions: dict[_SeriesKey, str],
    global_truth_reasons: tuple[str, ...],
    blotter_rows: list[JsonDict],
    blotter_reasons: tuple[str, ...],
    frozen_after: str | None,
    candidate_registration: JsonDict | None,
) -> _SeriesEvidence:
    scoped_dates = tuple(
        session_date
        for session_date in expected_dates
        if frozen_after is None or session_date > frozen_after
    )
    eligible: list[_SessionEvidence] = []
    exclusions: dict[str, tuple[str, ...]] = {}
    for session_date in scoped_dates:
        reasons = [*global_truth_reasons, *blotter_reasons]
        report = completed_reports[session_date]
        rows = calendar_index.get((session_date, key), [])
        if len(rows) != 1:
            reasons.append(
                "canonical calendar row is missing"
                if not rows
                else "duplicate canonical calendar rows"
            )
            exclusions[session_date] = tuple(sorted(dict.fromkeys(reasons)))
            continue
        row = rows[0]
        run_id = str(report.get("run_id") or "")
        snapshot_id = str(report.get("data_snapshot_id") or "")
        if str(row.get("run_id") or "") != run_id:
            reasons.append("calendar run_id does not match completed close report")
        if str(row.get("data_snapshot_id") or "") != snapshot_id:
            reasons.append("calendar data snapshot does not match completed close report")
        if candidate_registration is not None and row.get(
            "strategy_semantics_fingerprint"
        ) != candidate_registration.get("candidate_strategy_semantics_fingerprint"):
            reasons.append("shadow calendar strategy semantics fingerprint mismatch")
        if _unsafe_snapshot(snapshot_id):
            reasons.append("data snapshot lacks eligible sourced forward lineage")
        for field in _NUMERIC_CALENDAR_FIELDS:
            if _number(row.get(field)) is None:
                reasons.append(f"calendar field {field} is missing or non-numeric")
        reasons.extend(
            _preflight_reasons(output_root, session_date, report, universe)
        )
        reasons.extend(
            _decision_coverage_reasons(
                output_root,
                session_date,
                report,
                key,
                universe,
                champion_inceptions=champion_inceptions,
                candidate_registration=candidate_registration,
            )
        )
        if candidate_registration is not None:
            reasons.extend(
                _shadow_manifest_reasons(
                    output_root,
                    session_date,
                    report,
                    key,
                    candidate_registration,
                )
            )
        closes, fills, ledger_reasons = _ledger_session(
            ledger_events,
            blotter_rows,
            session_date,
            run_id,
            key,
            challenger_id=(
                str(candidate_registration.get("challenger_id") or "")
                if candidate_registration is not None
                else ""
            ),
        )
        reasons.extend(ledger_reasons)
        if _integer(row.get("trades_closed")) != len(closes):
            reasons.append("calendar trades_closed does not match exact ledger closes")
        if _integer(row.get("trades_opened")) != len(fills):
            reasons.append("calendar trades_opened does not match exact ledger fills")
        realized = _number(row.get("realized_pnl"))
        if realized is not None and not math.isclose(
            realized,
            sum(float(close["net_pnl"]) for close in closes),
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            reasons.append("calendar realized_pnl does not match exact after-cost ledger closes")
        outcomes = [float(close["net_pnl"]) for close in closes]
        if _integer(row.get("wins")) != sum(value > 0 for value in outcomes):
            reasons.append("calendar wins do not match exact ledger closes")
        if _integer(row.get("losses")) != sum(value < 0 for value in outcomes):
            reasons.append("calendar losses do not match exact ledger closes")
        if _integer(row.get("flats")) != sum(value == 0 for value in outcomes):
            reasons.append("calendar flats do not match exact ledger closes")
        if reasons:
            exclusions[session_date] = tuple(sorted(dict.fromkeys(reasons)))
            continue
        eligible.append(
            _SessionEvidence(
                session_date=session_date,
                row=row,
                closes=closes,
                fills=fills,
            )
        )
    return _SeriesEvidence(
        key=key,
        expected_dates=scoped_dates,
        eligible=tuple(eligible),
        exclusions=exclusions,
    )


def _preflight_reasons(
    output_root: Path,
    session_date: str,
    report: JsonDict,
    universe: tuple[str, ...],
) -> tuple[str, ...]:
    payload = read_json(output_root / "exports" / f"preflight_forward_{session_date}.json", {})
    if not isinstance(payload, dict) or not payload:
        return ("forward preflight evidence is missing",)
    reasons: list[str] = []
    if payload.get("mode") != "forward":
        reasons.append("preflight mode is not forward")
    if payload.get("status") not in {"passed", "passed_with_warnings"}:
        reasons.append("preflight status is not passed")
    if str(payload.get("run_date") or "") != session_date:
        reasons.append("preflight run date mismatch")
    if str(payload.get("latest_completed_date") or "") != session_date:
        reasons.append("preflight did not source the completed session date")
    if str(payload.get("run_id") or "") != str(report.get("run_id") or ""):
        reasons.append("preflight run_id does not match close report")
    if str(payload.get("data_snapshot_id") or "") != str(
        report.get("data_snapshot_id") or ""
    ):
        reasons.append("preflight data snapshot does not match close report")
    if payload.get("universe_status") != "complete":
        reasons.append("preflight universe is not complete")
    symbols = payload.get("symbols")
    observed = tuple(
        sorted(str(symbol).strip().upper() for symbol in symbols)
    ) if isinstance(symbols, list) else ()
    if not universe:
        reasons.append("configured PaperOps universe is missing")
    elif observed != universe:
        reasons.append("preflight symbols do not match configured universe")
    return tuple(reasons)


def _decision_coverage_reasons(
    output_root: Path,
    session_date: str,
    report: JsonDict,
    key: _SeriesKey,
    universe: tuple[str, ...],
    *,
    champion_inceptions: dict[_SeriesKey, str],
    candidate_registration: JsonDict | None,
) -> tuple[str, ...]:
    challenger_id = (
        str(candidate_registration.get("challenger_id") or "")
        if candidate_registration
        else ""
    )
    artifact_name = (
        f"shadow_strategy_decisions_forward_{session_date}_{_safe(challenger_id)}.json"
        if challenger_id
        else f"strategy_decisions_forward_{session_date}.json"
    )
    payload = read_json(output_root / "exports" / artifact_name, [])
    if not isinstance(payload, list):
        return ("strategy decision coverage artifact is missing",)
    reasons: list[str] = []
    if any(not isinstance(row, dict) for row in payload):
        reasons.append("strategy decision coverage contains a non-object row")
    object_rows = [row for row in payload if isinstance(row, dict)]
    matches = [
        row
        for row in object_rows
        if row.get("strategy_id") == key.strategy_id
        and row.get("strategy_version") == key.strategy_version
        and row.get("execution_policy_version") == key.execution_policy_version
        and row.get("strategy_semantics_fingerprint")
        == key.strategy_semantics_fingerprint
    ]
    contaminated = len(object_rows) != len(payload)
    if candidate_registration is not None:
        # Shadow decision artifacts are dedicated to exactly one frozen candidate.
        contaminated = contaminated or len(matches) != len(object_rows)
    else:
        # Champion decisions are intentionally one shared daily artifact.  Other
        # rows are valid only when they belong to another exact champion series
        # whose immutable forward coverage has already begun.
        active_champions = {
            champion
            for champion, inception in champion_inceptions.items()
            if session_date >= inception
        }
        observed_series = {
            _SeriesKey(
                strategy_id=str(row.get("strategy_id") or "").strip(),
                strategy_version=str(row.get("strategy_version") or "").strip(),
                execution_policy_version=str(
                    row.get("execution_policy_version") or ""
                ).strip(),
                strategy_semantics_fingerprint=str(
                    row.get("strategy_semantics_fingerprint") or ""
                ).strip(),
            )
            for row in object_rows
        }
        has_shadow_lineage = any(
            str(row.get("challenger_id") or "").strip()
            or str(row.get("logic_artifact_sha256") or "").strip()
            for row in object_rows
        )
        contaminated = (
            contaminated
            or not observed_series.issubset(active_champions)
            or has_shadow_lineage
        )
    if contaminated:
        reasons.append(
            "strategy decision artifact contains unmatched or cross-series contamination"
        )
    symbols = [str(row.get("symbol") or "").strip().upper() for row in matches]
    if tuple(sorted(symbols)) != universe:
        reasons.append("strategy decision coverage does not exactly match configured universe")
    if len(symbols) != len(set(symbols)):
        reasons.append("strategy decision coverage contains duplicate symbols")
    for row in matches:
        if row.get("execution_policy_version") != key.execution_policy_version:
            reasons.append("strategy decision execution policy mismatch")
        if row.get("strategy_semantics_fingerprint") != key.strategy_semantics_fingerprint:
            reasons.append("strategy decision semantics fingerprint mismatch")
        if row.get("mode") != "forward":
            reasons.append("strategy decision mode is not forward")
        if candidate_registration is not None:
            if row.get("challenger_id") != challenger_id:
                reasons.append("shadow strategy decision challenger_id mismatch")
            if row.get("logic_artifact_sha256") != candidate_registration.get(
                "logic_artifact_sha256"
            ):
                reasons.append("shadow strategy decision logic hash mismatch")
            if row.get("strategy_semantics_fingerprint") != candidate_registration.get(
                "candidate_strategy_semantics_fingerprint"
            ):
                reasons.append("shadow strategy decision semantics fingerprint mismatch")
        if str(row.get("run_id") or "") != str(report.get("run_id") or ""):
            reasons.append("strategy decision run_id does not match close report")
        decision = str(row.get("decision_status") or "")
        if decision not in _ALLOWED_DECISIONS:
            reasons.append("strategy decision status is unsupported")
        if decision == "accepted" and row.get("trade_return_eligible") is not True:
            reasons.append("accepted decision is not explicitly trade-return eligible")
        if decision != "accepted" and (
            row.get("trade_return_eligible") is not False
            or row.get("trade_return_pct") is not None
        ):
            reasons.append("non-trade decision fabricates trade-return eligibility")
    return tuple(reasons)


def _shadow_manifest_reasons(
    output_root: Path,
    session_date: str,
    report: JsonDict,
    key: _SeriesKey,
    registration: JsonDict,
) -> tuple[str, ...]:
    challenger_id = str(registration.get("challenger_id") or "")
    path = (
        output_root
        / "manifests"
        / f"shadow_forward_{session_date}_{_safe(challenger_id)}.json"
    )
    payload = read_json(path, {})
    if not isinstance(payload, dict) or not payload:
        return ("shadow execution manifest is missing",)
    reasons: list[str] = []
    universe = _configured_universe(output_root / "state")
    decisions = read_json(
        output_root
        / "exports"
        / f"shadow_strategy_decisions_forward_{session_date}_{_safe(challenger_id)}.json",
        [],
    )
    expected = {
        "status": "completed",
        "date": session_date,
        "mode": "forward",
        "run_id": str(report.get("run_id") or ""),
        "data_snapshot_id": str(report.get("data_snapshot_id") or ""),
        "challenger_id": challenger_id,
        "strategy_id": key.strategy_id,
        "strategy_version": key.strategy_version,
        "execution_policy_version": key.execution_policy_version,
        "strategy_semantics_fingerprint": key.strategy_semantics_fingerprint,
        "logic_artifact_sha256": registration.get("logic_artifact_sha256"),
        "decision_coverage_status": "complete",
        "decision_coverage": len(universe),
        "decision_artifact_sha256": _stable_digest(decisions),
        "decision_symbols_sha256": _stable_digest(list(universe)),
        "research_only": True,
        "automatic_promotion_enabled": False,
        "broker_execution_allowed": False,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            reasons.append(f"shadow execution manifest {field} mismatch")
    return tuple(reasons)


def _ledger_session(
    events: list[JsonDict],
    blotter_rows: list[JsonDict],
    session_date: str,
    run_id: str,
    key: _SeriesKey,
    *,
    challenger_id: str,
) -> tuple[tuple[JsonDict, ...], tuple[JsonDict, ...], tuple[str, ...]]:
    closes: list[JsonDict] = []
    fills: list[JsonDict] = []
    reasons: list[str] = []
    for event in events:
        if (
            event.get("mode") != "forward"
            or str(event.get("trade_date") or "") != session_date
            or event.get("strategy_id") != key.strategy_id
        ):
            continue
        event_type = str(event.get("event_type") or "")
        if event_type not in {"paper_fill", "paper_position_closed"}:
            continue
        event_reason_count = len(reasons)
        payload = event.get("payload")
        if not isinstance(payload, dict):
            reasons.append(f"{event_type} payload is missing")
            continue
        event_key = _SeriesKey(
            strategy_id=str(payload.get("strategy_id") or event.get("strategy_id") or ""),
            strategy_version=str(payload.get("strategy_version") or ""),
            execution_policy_version=str(payload.get("execution_policy_version") or ""),
            strategy_semantics_fingerprint=str(
                payload.get("strategy_semantics_fingerprint") or ""
            ),
        )
        if event_key != key:
            if (
                not event_key.strategy_version
                or not event_key.execution_policy_version
                or not event_key.strategy_semantics_fingerprint
            ):
                reasons.append(f"{event_type} is missing exact version/policy identity")
            continue
        if str(event.get("run_id") or "") != run_id:
            reasons.append(f"{event_type} run_id does not match completed close report")
        required = (
            ("fill_id", "order_id", "fill_price", "quantity", "fee", "slippage")
            if event_type == "paper_fill"
            else (
                "close_id",
                "position_id",
                "close_price",
                "gross_pnl",
                "net_pnl",
                "entry_fee",
                "fee",
                "slippage",
                "r_multiple",
            )
        )
        for field in required:
            value = payload.get(field)
            if field.endswith("_id"):
                if not str(value or "").strip():
                    reasons.append(f"{event_type} lifecycle field {field} is missing")
            elif _number(value) is None:
                reasons.append(f"{event_type} cost/outcome field {field} is missing")
        if any(
            not str(payload.get(field) or "").strip()
            if field.endswith("_id")
            else _number(payload.get(field)) is None
            for field in required
        ):
            continue
        for field in ("fee", "slippage"):
            if float(payload[field]) < 0:
                reasons.append(f"{event_type} cost field {field} is negative")
        if event_type == "paper_position_closed" and float(payload["entry_fee"]) < 0:
            reasons.append("paper_position_closed cost field entry_fee is negative")
        lifecycle_id = str(
            payload.get("fill_id")
            if event_type == "paper_fill"
            else payload.get("close_id")
        )
        lifecycle_field = "fill_id" if event_type == "paper_fill" else "close_id"
        lifecycle_matches = [
            row
            for row in blotter_rows
            if str(row.get(lifecycle_field) or "") == lifecycle_id
        ]
        if len(lifecycle_matches) != 1:
            reasons.append(
                f"{event_type} has no unique verified order-fill-position-close lifecycle"
            )
            continue
        reasons.extend(
            _blotter_lifecycle_reasons(
                lifecycle_matches[0],
                payload,
                event_type=event_type,
                key=key,
                run_id=run_id,
                challenger_id=challenger_id,
            )
        )
        if len(reasons) > event_reason_count:
            continue
        if event_type == "paper_fill":
            fills.append(payload)
        else:
            closes.append(payload)
    return tuple(closes), tuple(fills), tuple(reasons)


def _blotter_lifecycle_reasons(
    row: JsonDict,
    payload: JsonDict,
    *,
    event_type: str,
    key: _SeriesKey,
    run_id: str,
    challenger_id: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    expected_identity = {
        "mode": "forward",
        "strategy_id": key.strategy_id,
        "strategy_version": key.strategy_version,
        "execution_policy_version": key.execution_policy_version,
        "strategy_semantics_fingerprint": key.strategy_semantics_fingerprint,
        "challenger_id": challenger_id,
        "symbol": str(payload.get("symbol") or ""),
    }
    for field, expected in expected_identity.items():
        if str(row.get(field) or "") != expected:
            reasons.append(f"verified lifecycle {field} mismatch")
    if event_type == "paper_fill":
        if row.get("lifecycle_status") not in {"open", "closed"}:
            reasons.append("verified fill is not linked to an opened position")
        mappings = {
            "fill_id": "fill_id",
            "order_id": "order_id",
            "fill_price": "fill_price",
            "quantity": "quantity_filled",
            "fee": "entry_fee",
            "slippage": "entry_slippage",
        }
    else:
        if row.get("lifecycle_status") != "closed":
            reasons.append("verified close lifecycle is not closed")
        if str(row.get("close_run_id") or "") != run_id:
            reasons.append("verified close lifecycle run_id mismatch")
        mappings = {
            "close_id": "close_id",
            "position_id": "position_id",
            "close_price": "close_price",
            "gross_pnl": "gross_pnl",
            "net_pnl": "net_pnl",
            "entry_fee": "entry_fee",
            "fee": "exit_fee",
            "slippage": "exit_slippage",
            "r_multiple": "r_multiple",
        }
    for payload_field, row_field in mappings.items():
        observed = payload.get(payload_field)
        verified = row.get(row_field)
        if payload_field.endswith("_id"):
            if str(observed or "") != str(verified or ""):
                reasons.append(
                    f"verified lifecycle {payload_field} lineage mismatch"
                )
            continue
        observed_number = _number(observed)
        verified_number = _number(verified)
        if (
            observed_number is None
            or verified_number is None
            or not math.isclose(
                observed_number,
                verified_number,
                rel_tol=1e-9,
                abs_tol=1e-8,
            )
        ):
            reasons.append(
                f"verified lifecycle {payload_field} economic mismatch"
            )
    return tuple(reasons)


def _evaluate_registration(
    *,
    output_root: Path,
    champion: _SeriesKey,
    registration: JsonDict,
    get_evidence: Any,
    calendar_index: dict[tuple[str, _SeriesKey], list[JsonDict]],
    completed_reports: dict[str, JsonDict],
    config: ChallengerEvaluationConfig,
    operator_process_status: str,
    operator_process_reasons: tuple[str, ...],
) -> JsonDict:
    registration_reasons = _registration_reasons(
        registration,
        champion,
        output_root=output_root,
    )
    challenger_id = str(registration.get("challenger_id") or "")
    candidate = _SeriesKey(
        strategy_id=str(registration.get("strategy_id") or champion.strategy_id),
        strategy_version=str(registration.get("candidate_strategy_version") or ""),
        execution_policy_version=str(registration.get("execution_policy_version") or ""),
        strategy_semantics_fingerprint=str(
            registration.get("candidate_strategy_semantics_fingerprint") or ""
        ),
    )
    frozen_date = str(registration.get("frozen_at") or "")[:10]
    registered_date = str(registration.get("registered_at") or "")[:10]
    frozen_after = max(frozen_date, registered_date) if frozen_date and registered_date else None
    if registration_reasons:
        return _proposal_base(
            champion=champion,
            challenger_id=challenger_id or None,
            candidate=candidate,
            status="invalid_challenger_registration",
            evidence_blockers=registration_reasons,
            performance_blockers=(),
            operator_process_status=operator_process_status,
            operator_process_reasons=operator_process_reasons,
            champion_metrics=None,
            candidate_metrics=None,
            comparison=None,
            champion_evidence_sha256=None,
            candidate_evidence_sha256=None,
            excluded_dates={},
            next_action="repair and independently audit the frozen shadow registration",
            frozen_at=str(registration.get("frozen_at") or "") or None,
            registered_at=str(registration.get("registered_at") or "") or None,
            hypothesis=str(registration.get("hypothesis") or "") or None,
            logic_artifact_sha256=(
                str(registration.get("logic_artifact_sha256") or "") or None
            ),
        )

    champion_evidence = get_evidence(champion, frozen_after)
    candidate_evidence = get_evidence(candidate, frozen_after, registration)
    champion_by_date = {row.session_date: row for row in champion_evidence.eligible}
    candidate_by_date = {row.session_date: row for row in candidate_evidence.eligible}
    aligned_dates = tuple(sorted(set(champion_by_date) & set(candidate_by_date)))
    expected_dates = tuple(
        sorted(
            set(champion_evidence.expected_dates)
            & set(candidate_evidence.expected_dates)
        )
    )
    champion_aligned = tuple(champion_by_date[item] for item in aligned_dates)
    candidate_aligned = tuple(candidate_by_date[item] for item in aligned_dates)
    champion_metrics = _metrics(
        champion_aligned,
        len(expected_dates),
        config,
    )
    candidate_metrics = _metrics(
        candidate_aligned,
        len(expected_dates),
        config,
    )
    comparison = _comparison(
        champion_aligned,
        candidate_aligned,
        aligned_dates,
        calendar_index,
        completed_reports,
        config,
    )

    evidence_blockers = _evidence_blockers(
        champion_metrics,
        candidate_metrics,
        comparison,
        config,
    )
    performance_blockers = (
        ()
        if evidence_blockers
        else _performance_blockers(champion_metrics, candidate_metrics, comparison, config)
    )
    if evidence_blockers:
        status = "insufficient_evidence"
        next_action = (
            "collect canonical post-freeze forward evidence without changing either version"
        )
    elif performance_blockers:
        status = "rejected_by_evidence"
        next_action = (
            "retain champion; archive this frozen hypothesis and design a new shadow version"
        )
    elif operator_process_status != "audited_manual_only":
        status = "evidence_passed_operator_process_missing"
        next_action = "establish an audited manual-only operator review process; do not promote"
    else:
        status = "eligible_for_audited_manual_review"
        next_action = "perform the independent audited manual review; this service cannot promote"

    excluded = {
        "champion": champion_evidence.exclusions,
        "candidate": candidate_evidence.exclusions,
    }
    return _proposal_base(
        champion=champion,
        challenger_id=challenger_id,
        candidate=candidate,
        status=status,
        evidence_blockers=evidence_blockers,
        performance_blockers=performance_blockers,
        operator_process_status=operator_process_status,
        operator_process_reasons=operator_process_reasons,
        champion_metrics=champion_metrics,
        candidate_metrics=candidate_metrics,
        comparison=comparison,
        champion_evidence_sha256=_evidence_fingerprint(champion_evidence),
        candidate_evidence_sha256=_evidence_fingerprint(candidate_evidence),
        excluded_dates=excluded,
        next_action=next_action,
        frozen_at=str(registration.get("frozen_at")),
        registered_at=str(registration.get("registered_at")),
        hypothesis=str(registration.get("hypothesis")),
        logic_artifact_sha256=str(registration.get("logic_artifact_sha256")),
    )


def _registration_reasons(
    registration: JsonDict,
    champion: _SeriesKey,
    *,
    output_root: Path,
) -> tuple[str, ...]:
    reasons: list[str] = []
    required = (
        "challenger_id",
        "strategy_id",
        "champion_strategy_version",
        "champion_strategy_semantics_fingerprint",
        "candidate_strategy_version",
        "candidate_strategy_semantics_fingerprint",
        "execution_policy_version",
        "champion_strategy_semantics_fingerprint",
        "frozen_at",
        "registered_at",
        "hypothesis",
        "logic_artifact_sha256",
        "candidate_strategy_semantics_fingerprint",
    )
    for field in required:
        if not str(registration.get(field) or "").strip():
            reasons.append(f"challenger registration is missing {field}")
    if registration.get("status") != "shadow":
        reasons.append("challenger registration status must be shadow")
    if registration.get("strategy_id") != champion.strategy_id:
        reasons.append("challenger strategy_id does not match champion")
    if registration.get("champion_strategy_version") != champion.strategy_version:
        reasons.append("challenger champion version does not match current registry")
    if registration.get(
        "champion_strategy_semantics_fingerprint"
    ) != champion.strategy_semantics_fingerprint:
        reasons.append("challenger champion semantics do not match current registry")
    if registration.get("candidate_strategy_version") == champion.strategy_version:
        reasons.append("challenger must use a distinct frozen strategy version")
    if registration.get("execution_policy_version") != champion.execution_policy_version:
        reasons.append("challenger execution policy must match champion for direct comparison")
    frozen = str(registration.get("frozen_at") or "")[:10]
    registered = str(registration.get("registered_at") or "")[:10]
    try:
        date.fromisoformat(frozen)
    except ValueError:
        reasons.append("challenger frozen_at date is invalid")
    try:
        date.fromisoformat(registered)
    except ValueError:
        reasons.append("challenger registered_at date is invalid")
    artifact_hash = str(registration.get("logic_artifact_sha256") or "")
    if artifact_hash and not _is_sha256(artifact_hash):
        reasons.append("challenger logic_artifact_sha256 is invalid")
    try:
        from intraday_scanner.v2.paper_ops.shadow_runner import (
            verify_registration_integrity,
        )

        reasons.extend(
            verify_registration_integrity(registration, output_root=output_root)
        )
    except (OSError, TypeError, ValueError) as exc:
        reasons.append(f"challenger immutable registration verification failed: {exc}")
    return tuple(reasons)


def _no_challenger_proposal(
    champion: _SeriesKey,
    evidence: _SeriesEvidence,
    config: ChallengerEvaluationConfig,
    operator_process_status: str,
    operator_process_reasons: tuple[str, ...],
) -> JsonDict:
    metrics = _metrics(
        evidence.eligible,
        len(evidence.expected_dates),
        config,
    )
    evidence_notes: list[str] = ["no registered frozen shadow challenger"]
    eligible_sessions = int(metrics["eligible_session_count"])
    closed_trades = int(metrics["closed_trade_count"])
    if eligible_sessions < config.min_forward_sessions:
        evidence_notes.append(
            f"champion needs {config.min_forward_sessions - eligible_sessions} "
            "more eligible forward sessions"
        )
    if closed_trades < config.min_closed_trades:
        evidence_notes.append(
            f"champion needs {config.min_closed_trades - closed_trades} "
            "more exact closed trades"
        )
    return _proposal_base(
        champion=champion,
        challenger_id=None,
        candidate=None,
        status="no_registered_challenger",
        evidence_blockers=tuple(evidence_notes),
        performance_blockers=(),
        operator_process_status=operator_process_status,
        operator_process_reasons=operator_process_reasons,
        champion_metrics=metrics,
        candidate_metrics=None,
        comparison=None,
        champion_evidence_sha256=_evidence_fingerprint(evidence),
        candidate_evidence_sha256=None,
        excluded_dates={"champion": evidence.exclusions},
        next_action=(
            "collect eligible forward evidence, then register a separately versioned frozen "
            "hypothesis; never tune the champion in place"
        ),
    )


def _metrics(
    sessions: tuple[_SessionEvidence, ...],
    expected_count: int,
    config: ChallengerEvaluationConfig,
) -> JsonDict:
    closes = [close for session in sessions for close in session.closes]
    fills = [fill for session in sessions for fill in session.fills]
    returns = [float(session.row["daily_return_pct"]) for session in sessions]
    pnl = [float(close["net_pnl"]) for close in closes]
    wins = sum(value > 0 for value in pnl)
    losses = sum(value < 0 for value in pnl)
    flats = sum(value == 0 for value in pnl)
    gross_profit = sum(value for value in pnl if value > 0)
    gross_loss = abs(sum(value for value in pnl if value < 0))
    slippage_cost = (
        sum(float(fill["slippage"]) for fill in fills)
        + sum(float(close["slippage"]) for close in closes)
    )
    gain_concentration = (
        max((value for value in pnl if value > 0), default=0.0)
        / gross_profit
        * 100.0
        if gross_profit > 0
        else 0.0
    )
    loss_concentration = (
        max((abs(value) for value in pnl if value < 0), default=0.0)
        / gross_loss
        * 100.0
        if gross_loss > 0
        else 0.0
    )
    stressed_net_pnl = sum(pnl) - (
        max(0.0, config.slippage_stress_multiplier - 1.0)
        * slippage_cost
    )
    return {
        "eligible_session_count": len(sessions),
        "expected_completed_session_count": expected_count,
        "coverage_pct": round((len(sessions) / expected_count) * 100.0, 10)
        if expected_count
        else None,
        "first_eligible_date": sessions[0].session_date if sessions else None,
        "last_eligible_date": sessions[-1].session_date if sessions else None,
        "closed_trade_count": len(closes),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate_pct": round((wins / len(closes)) * 100.0, 10) if closes else None,
        "net_after_cost_pnl": round(sum(pnl), 10),
        "after_cost_expectancy": (
            round(sum(pnl) / len(closes), 10) if closes else None
        ),
        "profit_factor": (
            round(gross_profit / gross_loss, 10)
            if gross_loss > 0
            else 1_000_000.0
            if gross_profit > 0
            else None
        ),
        "gain_concentration_pct": (
            round(gain_concentration, 10)
        ),
        "loss_concentration_pct": (
            round(loss_concentration, 10)
        ),
        "slippage_stress_multiplier": config.slippage_stress_multiplier,
        "slippage_stress_after_cost_expectancy": (
            round(stressed_net_pnl / len(closes), 10)
            if closes
            else None
        ),
        "net_after_cost_cumulative_return_pct": _compound_pct(returns),
        "max_drawdown_pct": min(
            (float(session.row["drawdown_pct"]) * 100.0 for session in sessions),
            default=None,
        ),
        "entry_fees": round(sum(float(fill["fee"]) for fill in fills), 10),
        "exit_fees": round(sum(float(close["fee"]) for close in closes), 10),
        "slippage_cost": round(slippage_cost, 10),
    }


def _comparison(
    champion: tuple[_SessionEvidence, ...],
    candidate: tuple[_SessionEvidence, ...],
    aligned_dates: tuple[str, ...],
    calendar_index: dict[tuple[str, _SeriesKey], list[JsonDict]],
    completed_reports: dict[str, JsonDict],
    config: ChallengerEvaluationConfig,
) -> JsonDict:
    holdout_count = max(
        config.min_holdout_sessions,
        math.ceil(len(aligned_dates) * config.holdout_fraction),
    )
    if holdout_count >= len(aligned_dates):
        research_dates: tuple[str, ...] = ()
        holdout_dates = aligned_dates
    else:
        research_dates = aligned_dates[:-holdout_count]
        holdout_dates = aligned_dates[-holdout_count:]
    by_champion = {row.session_date: row for row in champion}
    by_candidate = {row.session_date: row for row in candidate}
    fold_size = config.min_sessions_per_fold
    offset = len(research_dates) % fold_size
    fold_dates = tuple(
        research_dates[index : index + fold_size]
        for index in range(offset, len(research_dates), fold_size)
        if len(research_dates[index : index + fold_size]) == fold_size
    )
    folds: list[JsonDict] = []
    for index, dates in enumerate(fold_dates, start=1):
        champion_return = _return_for_dates(by_champion, dates)
        candidate_return = _return_for_dates(by_candidate, dates)
        folds.append(
            {
                "fold": index,
                "start": dates[0],
                "end": dates[-1],
                "session_count": len(dates),
                "champion_return_pct": champion_return,
                "candidate_return_pct": candidate_return,
                "candidate_excess_pct": round(candidate_return - champion_return, 10),
            }
        )
    positive_folds = sum(float(row["candidate_excess_pct"]) > 0 for row in folds)
    holdout_champion = _return_for_dates(by_champion, holdout_dates)
    holdout_candidate = _return_for_dates(by_candidate, holdout_dates)
    benchmark = _reference_metrics(
        calendar_index, completed_reports, aligned_dates, "benchmark"
    )
    cash = _reference_metrics(calendar_index, completed_reports, aligned_dates, "cash")
    holdout_benchmark = _reference_metrics(
        calendar_index, completed_reports, holdout_dates, "benchmark"
    )
    holdout_cash = _reference_metrics(
        calendar_index, completed_reports, holdout_dates, "cash"
    )
    return {
        "aligned_session_count": len(aligned_dates),
        "aligned_dates": list(aligned_dates),
        "research_session_count": len(research_dates),
        "holdout_session_count": len(holdout_dates),
        "holdout_start": holdout_dates[0] if holdout_dates else None,
        "holdout_end": holdout_dates[-1] if holdout_dates else None,
        "holdout_champion_return_pct": holdout_champion if holdout_dates else None,
        "holdout_candidate_return_pct": holdout_candidate if holdout_dates else None,
        "holdout_candidate_excess_pct": (
            round(holdout_candidate - holdout_champion, 10) if holdout_dates else None
        ),
        "walk_forward_fold_count": len(folds),
        "walk_forward_positive_excess_fold_count": positive_folds,
        "walk_forward_positive_excess_ratio": (
            round(positive_folds / len(folds), 10) if folds else None
        ),
        "walk_forward_folds": folds,
        "benchmark": benchmark,
        "cash": cash,
        "holdout_benchmark": holdout_benchmark,
        "holdout_cash": holdout_cash,
    }


def _reference_metrics(
    index: dict[tuple[str, _SeriesKey], list[JsonDict]],
    completed_reports: dict[str, JsonDict],
    session_dates: tuple[str, ...],
    kind: str,
) -> JsonDict:
    strategy_id, version, policy = _REFERENCE_IDS[kind]
    returns: list[float] = []
    source_rows: list[JsonDict] = []
    missing: list[str] = []
    for session_date in session_dates:
        rows = [
            row
            for (indexed_date, key), indexed_rows in index.items()
            if indexed_date == session_date
            and key.strategy_id == strategy_id
            and key.strategy_version == version
            and key.execution_policy_version == policy
            for row in indexed_rows
        ]
        report = completed_reports.get(session_date, {})
        if len(rows) != 1:
            missing.append(session_date)
            continue
        row = rows[0]
        if (
            _number(row.get("daily_return_pct")) is None
            or str(row.get("run_id") or "") != str(report.get("run_id") or "")
            or str(row.get("data_snapshot_id") or "")
            != str(report.get("data_snapshot_id") or "")
            or _unsafe_snapshot(str(row.get("data_snapshot_id") or ""))
        ):
            missing.append(session_date)
            continue
        returns.append(float(row["daily_return_pct"]))
        source_rows.append(row)
    return {
        "strategy_id": strategy_id,
        "policy_version": policy,
        "observation_count": len(returns),
        "expected_count": len(session_dates),
        "coverage_pct": round((len(returns) / len(session_dates)) * 100.0, 10)
        if session_dates
        else None,
        "cumulative_return_pct": _compound_pct(returns) if not missing and returns else None,
        "source_evidence_sha256": _stable_digest(source_rows) if source_rows else None,
        "missing_dates": missing,
    }


def _evidence_blockers(
    champion: JsonDict,
    candidate: JsonDict,
    comparison: JsonDict,
    config: ChallengerEvaluationConfig,
) -> tuple[str, ...]:
    blockers: list[str] = []
    for label, metrics in (("champion", champion), ("candidate", candidate)):
        sessions = int(metrics["eligible_session_count"])
        trades = int(metrics["closed_trade_count"])
        coverage = _number(metrics.get("coverage_pct"))
        if sessions < config.min_forward_sessions:
            blockers.append(f"{label} needs {config.min_forward_sessions - sessions} more sessions")
        if trades < config.min_closed_trades:
            blockers.append(f"{label} needs {config.min_closed_trades - trades} more closed trades")
        if coverage is None or coverage < config.min_coverage_pct:
            blockers.append(f"{label} canonical coverage is below {config.min_coverage_pct}%")
    if int(comparison["holdout_session_count"]) < config.min_holdout_sessions:
        blockers.append("untouched chronological holdout is too small")
    if int(comparison["walk_forward_fold_count"]) < config.min_walk_forward_folds:
        blockers.append("insufficient non-overlapping pre-holdout walk-forward folds")
    if config.require_benchmark and comparison["benchmark"]["cumulative_return_pct"] is None:  # type: ignore[index]
        blockers.append("sourced benchmark coverage is incomplete")
    if config.require_cash and comparison["cash"]["cumulative_return_pct"] is None:  # type: ignore[index]
        blockers.append("sourced cash-baseline coverage is incomplete")
    if (
        config.require_benchmark
        and comparison["holdout_benchmark"]["cumulative_return_pct"] is None
    ):
        blockers.append("holdout benchmark coverage is incomplete")
    if config.require_cash and comparison["holdout_cash"]["cumulative_return_pct"] is None:  # type: ignore[index]
        blockers.append("holdout cash-baseline coverage is incomplete")
    return tuple(blockers)


def _performance_blockers(
    champion: JsonDict,
    candidate: JsonDict,
    comparison: JsonDict,
    config: ChallengerEvaluationConfig,
) -> tuple[str, ...]:
    blockers: list[str] = []
    champion_return = float(champion["net_after_cost_cumulative_return_pct"])
    candidate_return = float(candidate["net_after_cost_cumulative_return_pct"])
    if candidate_return <= 0:
        blockers.append("candidate net after-cost cumulative return is not positive")
    expectancy = _number(candidate.get("after_cost_expectancy"))
    if expectancy is None or expectancy <= 0:
        blockers.append("candidate after-cost expectancy is not positive")
    profit_factor = _number(candidate.get("profit_factor"))
    if (
        profit_factor is None
        or profit_factor < config.min_profit_factor
    ):
        blockers.append(
            f"candidate profit factor is below {config.min_profit_factor}"
        )
    for label in ("gain", "loss"):
        concentration = _number(
            candidate.get(f"{label}_concentration_pct")
        )
        if (
            concentration is None
            or concentration > config.max_gain_loss_concentration_pct
        ):
            blockers.append(
                f"candidate {label} concentration exceeds "
                f"{config.max_gain_loss_concentration_pct}%"
            )
    stressed_expectancy = _number(
        candidate.get("slippage_stress_after_cost_expectancy")
    )
    if stressed_expectancy is None or stressed_expectancy <= 0:
        blockers.append(
            "candidate is not positive under the required 1.5x "
            "slippage stress"
        )
    if candidate_return - champion_return <= config.min_excess_return_vs_champion_pct:
        blockers.append("candidate does not exceed champion net after-cost return threshold")
    candidate_drawdown = _number(candidate.get("max_drawdown_pct"))
    champion_drawdown = _number(champion.get("max_drawdown_pct"))
    if candidate_drawdown is None or candidate_drawdown < config.max_drawdown_pct:
        blockers.append("candidate drawdown exceeds the absolute limit")
    if (
        candidate_drawdown is not None
        and champion_drawdown is not None
        and candidate_drawdown
        < champion_drawdown - config.max_drawdown_worsening_pct
    ):
        blockers.append("candidate drawdown is worse than champion tolerance")
    for kind in ("benchmark", "cash"):
        reference = comparison[kind]  # type: ignore[index]
        reference_return = _number(reference.get("cumulative_return_pct"))
        required = (
            config.require_benchmark
            if kind == "benchmark"
            else config.require_cash
        )
        if (
            required
            and (
                reference_return is None
                or candidate_return <= reference_return
            )
        ):
            blockers.append(
                f"candidate does not beat sourced {kind} across the "
                "full forward interval"
            )
    candidate_win = _number(candidate.get("win_rate_pct"))
    champion_win = _number(champion.get("win_rate_pct"))
    if candidate_win is None or champion_win is None:
        blockers.append("candidate/champion win-rate comparison is unavailable")
    elif candidate_win < champion_win - config.max_win_rate_decline_pct_points:
        blockers.append("candidate win rate declines beyond tolerance")
    ratio = _number(comparison.get("walk_forward_positive_excess_ratio"))
    if ratio is None or ratio < config.min_positive_walk_forward_ratio:
        blockers.append("candidate lacks consistent positive walk-forward excess")
    holdout_excess = _number(comparison.get("holdout_candidate_excess_pct"))
    if holdout_excess is None or holdout_excess <= 0:
        blockers.append("candidate does not beat champion on untouched holdout")
    holdout_candidate = _number(comparison.get("holdout_candidate_return_pct"))
    for kind in ("benchmark", "cash"):
        reference = comparison[f"holdout_{kind}"]  # type: ignore[index]
        reference_return = _number(reference.get("cumulative_return_pct"))
        required = config.require_benchmark if kind == "benchmark" else config.require_cash
        if required and (
            holdout_candidate is None
            or reference_return is None
            or holdout_candidate <= reference_return
        ):
            blockers.append(f"candidate does not beat sourced {kind} on untouched holdout")
    return tuple(blockers)


def _proposal_base(
    *,
    champion: _SeriesKey,
    challenger_id: str | None,
    candidate: _SeriesKey | None,
    status: str,
    evidence_blockers: tuple[str, ...],
    performance_blockers: tuple[str, ...],
    operator_process_status: str,
    operator_process_reasons: tuple[str, ...],
    champion_metrics: JsonDict | None,
    candidate_metrics: JsonDict | None,
    comparison: JsonDict | None,
    champion_evidence_sha256: str | None,
    candidate_evidence_sha256: str | None,
    excluded_dates: JsonDict,
    next_action: str,
    frozen_at: str | None = None,
    registered_at: str | None = None,
    hypothesis: str | None = None,
    logic_artifact_sha256: str | None = None,
) -> JsonDict:
    core = {
        "strategy_id": champion.strategy_id,
        "challenger_id": challenger_id,
        "champion": champion.to_dict(),
        "candidate": candidate.to_dict() if candidate else None,
        "frozen_at": frozen_at,
        "registered_at": registered_at,
        "hypothesis": hypothesis,
        "logic_artifact_sha256": logic_artifact_sha256,
        "evaluation_status": status,
        "champion_metrics": champion_metrics,
        "candidate_metrics": candidate_metrics,
        "comparison": comparison,
        "champion_evidence_sha256": champion_evidence_sha256,
        "candidate_evidence_sha256": candidate_evidence_sha256,
        "evidence_blockers": list(evidence_blockers),
        "performance_blockers": list(performance_blockers),
        "excluded_dates": excluded_dates,
        "operator_process_status": operator_process_status,
        "operator_process_blockers": list(operator_process_reasons),
        "operator_review_eligible": status == "eligible_for_audited_manual_review",
        "promotion_allowed": False,
        "automatic_promotion_enabled": False,
        "recommended_next_action": next_action,
    }
    return {"proposal_id": _stable_digest(core), **core}


def _unregistered_series(
    index: dict[tuple[str, _SeriesKey], list[JsonDict]],
    champions: list[_SeriesKey],
    challengers: list[JsonDict],
) -> list[str]:
    allowed = set(champions)
    for row in challengers:
        allowed.add(
            _SeriesKey(
                str(row.get("strategy_id") or ""),
                str(row.get("candidate_strategy_version") or ""),
                str(row.get("execution_policy_version") or ""),
                str(row.get("candidate_strategy_semantics_fingerprint") or ""),
            )
        )
    strategy_ids = {item.strategy_id for item in champions}
    found = {
        key
        for _session_date, key in index
        if key.strategy_id in strategy_ids and key not in allowed
    }
    return [
        (
            f"{key.strategy_id}:{key.strategy_version}:"
            f"{key.execution_policy_version or 'missing-policy'}:"
            f"{key.strategy_semantics_fingerprint or 'missing-semantics'}"
        )
        for key in sorted(
            found,
            key=lambda item: (
                item.strategy_id,
                item.strategy_version,
                item.execution_policy_version,
                item.strategy_semantics_fingerprint,
            ),
        )
    ]


def _evidence_fingerprint(evidence: _SeriesEvidence) -> str:
    return _stable_digest(
        {
            "key": evidence.key.to_dict(),
            "expected_dates": evidence.expected_dates,
            "eligible": [
                {
                    "date": session.session_date,
                    "calendar": session.row,
                    "closes": session.closes,
                    "fills": session.fills,
                }
                for session in evidence.eligible
            ],
            "exclusions": evidence.exclusions,
        }
    )


def _write_artifacts(reports: Path, payload: JsonDict) -> dict[str, str]:
    json_path = reports / "challenger_evaluation_latest.json"
    csv_path = reports / "challenger_evaluation_latest.csv"
    markdown_path = reports / "challenger_evaluation_latest.md"
    write_json(json_path, payload)
    csv_rows = [_csv_proposal(row) for row in payload["proposals"]]  # type: ignore[index]
    fields = (
        "proposal_id",
        "strategy_id",
        "challenger_id",
        "evaluation_status",
        "champion_strategy_version",
        "candidate_strategy_version",
        "execution_policy_version",
        "candidate_sessions",
        "candidate_closed_trades",
        "candidate_coverage_pct",
        "candidate_net_after_cost_return_pct",
        "candidate_max_drawdown_pct",
        "candidate_win_rate_pct",
        "champion_net_after_cost_return_pct",
        "holdout_candidate_excess_pct",
        "walk_forward_positive_excess_ratio",
        "operator_review_eligible",
        "promotion_allowed",
        "evidence_blockers",
        "performance_blockers",
        "recommended_next_action",
    )
    write_csv(csv_path, csv_rows, fields)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    return {
        "json": str(json_path),
        "csv": str(csv_path),
        "markdown": str(markdown_path),
        "history_jsonl": str(reports / "challenger_evaluation_history.jsonl"),
    }


def _csv_proposal(row: JsonDict) -> JsonDict:
    champion = _dict(row.get("champion"))
    candidate = _dict(row.get("candidate"))
    candidate_metrics = _dict(row.get("candidate_metrics"))
    champion_metrics = _dict(row.get("champion_metrics"))
    comparison = _dict(row.get("comparison"))
    return {
        "proposal_id": row["proposal_id"],
        "strategy_id": row["strategy_id"],
        "challenger_id": row.get("challenger_id"),
        "evaluation_status": row["evaluation_status"],
        "champion_strategy_version": champion.get("strategy_version"),
        "champion_strategy_semantics_fingerprint": champion.get(
            "strategy_semantics_fingerprint"
        ),
        "candidate_strategy_version": candidate.get("strategy_version"),
        "candidate_strategy_semantics_fingerprint": candidate.get(
            "strategy_semantics_fingerprint"
        ),
        "execution_policy_version": champion.get("execution_policy_version"),
        "candidate_sessions": candidate_metrics.get("eligible_session_count"),
        "candidate_closed_trades": candidate_metrics.get("closed_trade_count"),
        "candidate_coverage_pct": candidate_metrics.get("coverage_pct"),
        "candidate_net_after_cost_return_pct": candidate_metrics.get(
            "net_after_cost_cumulative_return_pct"
        ),
        "candidate_max_drawdown_pct": candidate_metrics.get("max_drawdown_pct"),
        "candidate_win_rate_pct": candidate_metrics.get("win_rate_pct"),
        "champion_net_after_cost_return_pct": champion_metrics.get(
            "net_after_cost_cumulative_return_pct"
        ),
        "holdout_candidate_excess_pct": comparison.get("holdout_candidate_excess_pct"),
        "walk_forward_positive_excess_ratio": comparison.get(
            "walk_forward_positive_excess_ratio"
        ),
        "operator_review_eligible": row["operator_review_eligible"],
        "promotion_allowed": row["promotion_allowed"],
        "evidence_blockers": " | ".join(str(item) for item in row["evidence_blockers"]),
        "performance_blockers": " | ".join(
            str(item) for item in row["performance_blockers"]
        ),
        "recommended_next_action": row["recommended_next_action"],
    }


def _markdown(payload: JsonDict) -> str:
    lines = [
        "# PaperOps Challenger Evaluation",
        "",
        f"- Evaluation ID: `{payload['evaluation_id']}`",
        f"- Evidence as of: `{payload['evidence_as_of'] or 'N/A'}`",
        "- Evidence mode: `forward_only`",
        "- Broker execution: `disabled`",
        "- Automatic promotion: `disabled`",
        f"- Operator process: `{payload['operator_process_status']}`",
        "",
        "Replay, demo, missing-source, partial-decision, identity-mismatched, and "
        "unreconciled rows are excluded.",
        "",
        "| Strategy | Challenger | Status | Candidate sessions/trades | After-cost return | "
        "Drawdown | Win rate | Manual review |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for raw in payload["proposals"]:  # type: ignore[index]
        row = raw if isinstance(raw, dict) else {}
        metrics = row.get("candidate_metrics")
        candidate = metrics if isinstance(metrics, dict) else {}
        lines.append(
            f"| {row.get('strategy_id')} | {row.get('challenger_id') or 'N/A'} | "
            f"{row.get('evaluation_status')} | "
            f"{candidate.get('eligible_session_count', 'N/A')}/"
            f"{candidate.get('closed_trade_count', 'N/A')} | "
            f"{_display(candidate.get('net_after_cost_cumulative_return_pct'))} | "
            f"{_display(candidate.get('max_drawdown_pct'))} | "
            f"{_display(candidate.get('win_rate_pct'))} | "
            f"{row.get('operator_review_eligible')} |"
        )
    warnings = payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.extend(
        [
            "",
            "## Governance invariant",
            "",
            "This evaluator emits proposals only. It never changes champion logic, strategy "
            "versions, paper account state, orders, positions, or execution policy.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _read_csv(path: Path) -> list[JsonDict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _return_for_dates(
    rows: dict[str, _SessionEvidence],
    session_dates: tuple[str, ...],
) -> float:
    return _compound_pct([float(rows[item].row["daily_return_pct"]) for item in session_dates])


def _compound_pct(fractional_returns: list[float]) -> float:
    factor = 1.0
    for value in fractional_returns:
        factor *= 1.0 + value
    return round((factor - 1.0) * 100.0, 10)


def _dict(value: Any) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _unsafe_snapshot(value: str) -> bool:
    lowered = value.lower()
    return not value or any(
        token in lowered for token in ("synthetic", "fixture", "ledger_rebuild")
    )


def _stable_digest(payload: object) -> str:
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _safe(value: str) -> str:
    return value.replace(":", "_").replace("/", "_").replace("\\", "_")


def _stable_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)


def _display(value: object) -> str:
    number = _number(value)
    return "N/A" if number is None else f"{number:.4f}%"
