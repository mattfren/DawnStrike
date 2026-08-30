"""Cycle 2 research-only feature and evidence contracts.

This module is intentionally additive.  It does not feed the V5 scorer (or the
existing V6 champion path), and it has no broker or execution imports.  Every
feature is either an observed, point-in-time value or an explicit ``UNKNOWN``;
there is no zero-imputation fallback.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from statistics import pstdev
from typing import Any, TypedDict

from intraday_scanner.alpha.fill_truth import has_authenticated_committed_fill_truth
from intraday_scanner.alpha.v6.contracts import FEATURE_SCHEMA_V2, canonical_hash

UNKNOWN = "UNKNOWN"
OBSERVED = "OBSERVED"
FEATURE_SCHEMA_VERSION_V2 = FEATURE_SCHEMA_V2
FEATURE_NAMES = (
    "benchmark_volatility",
    "market_breadth",
    "sector_breadth",
    "gap_dispersion",
    # Cycle 7 challenger features.  These names are deliberately descriptive
    # and are never consumed by the V5/champion scorer.
    "gap_volatility_normalized",
    "momentum_volatility_normalized",
    "gap_percentile",
    "momentum_percentile",
    "dollar_volume_proxy",
    "turnover_proxy",
    "spread_bps_proxy",
    "round_trip_cost_bps_proxy",
    "residual_vs_market",
    "residual_vs_sector",
)
V2_UNIVERSE_CONTRACT = {
    "price_min_usd": 1.0,
    "price_max_usd": 500.0,
    "gap_min_pct": 1.0,
    "gap_max_pct": 50.0,
    "max_gap_regime_pct": 50.0,
}
CATALYST_ABLATION_MODES = (
    "full",
    "no_catalyst",
    "catalyst_only",
    "shuffled_negative_control",
)

# A frozen, research-only policy.  It intentionally uses observable market
# conditions instead of the unreachable legacy 120% gap regime threshold.
# Keep this mapping JSON-compatible: the policy hash is part of every receipt.
class _Cycle7MinimumSamples(TypedDict):
    benchmark_returns: int
    market_rows: int
    sector_rows: int
    gap_rows: int
    liquidity_rows: int


class _Cycle7Volatility(TypedDict):
    low_max: float
    high_min: float


class _Cycle7Breadth(TypedDict):
    risk_on_min: float
    risk_off_max: float


class _Cycle7Dispersion(TypedDict):
    high_min: float


class _Cycle7Liquidity(TypedDict):
    minimum_dollar_volume: float
    maximum_cost_bps: float


class _Cycle7RegimePolicy(TypedDict):
    schema_version: str
    max_gap_pct: float
    minimum_samples: _Cycle7MinimumSamples
    volatility: _Cycle7Volatility
    breadth: _Cycle7Breadth
    dispersion: _Cycle7Dispersion
    liquidity: _Cycle7Liquidity
    legacy_gap_threshold_used: bool
    ranking_mutation: bool
    promotion_mutation: bool
    policy_mutation: bool
    broker_execution_enabled: bool


CYCLE7_REGIME_POLICY: _Cycle7RegimePolicy = {
    "schema_version": "dawnstrike.alphaops_v6.cycle7_shadow_regime_policy.v1",
    "max_gap_pct": 50.0,
    "minimum_samples": {
        "benchmark_returns": 20,
        "market_rows": 20,
        "sector_rows": 5,
        "gap_rows": 20,
        "liquidity_rows": 20,
    },
    "volatility": {"low_max": 1.0, "high_min": 3.0},
    "breadth": {"risk_on_min": 0.60, "risk_off_max": 0.40},
    "dispersion": {"high_min": 10.0},
    "liquidity": {"minimum_dollar_volume": 100000.0, "maximum_cost_bps": 100.0},
    "legacy_gap_threshold_used": False,
    "ranking_mutation": False,
    "promotion_mutation": False,
    "policy_mutation": False,
    "broker_execution_enabled": False,
}
CYCLE7_REGIME_POLICY_HASH_SHA256 = canonical_hash(CYCLE7_REGIME_POLICY)
CYCLE7_FEATURE_MINIMUMS = {
    "benchmark_volatility": 2,
    "market_breadth": 1,
    "sector_breadth": 1,
    "gap_dispersion": 2,
    "gap_volatility_normalized": 2,
    "momentum_volatility_normalized": 2,
    "gap_percentile": 2,
    "momentum_percentile": 2,
    "dollar_volume_proxy": 1,
    "turnover_proxy": 1,
    "spread_bps_proxy": 1,
    "round_trip_cost_bps_proxy": 1,
    "residual_vs_market": 2,
    "residual_vs_sector": 2,
}


def _unknown(reason: str) -> dict[str, Any]:
    return {"value": None, "status": UNKNOWN, "reason": reason}


def _observed(
    value: float, *, sample_size: int, source_hashes: Sequence[str], **metadata: Any
) -> dict[str, Any]:
    result = {
        "value": round(value, 10),
        "status": OBSERVED,
        "sample_size": sample_size,
        "source_hashes": sorted(set(source_hashes)),
    }
    result.update(metadata)
    return result


def _number(value: object) -> float | None:
    # bool is a distinct JSON type, not a numeric observation.  Rejecting it
    # prevents True/False from becoming 1/0 in a research feature.
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _minimum_sample_size(name: str, minimums: Mapping[str, Any] | None = None) -> int:
    value = (minimums or {}).get(name, 1)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, parsed)


def _row_identity(row: Mapping[str, Any]) -> str | None:
    value = row.get("ticker") or row.get("symbol") or row.get("security_id") or row.get("id")
    text = _text(value).upper()
    return text or None


def _row_momentum(row: Mapping[str, Any]) -> float | None:
    for key in ("momentum_pct", "momentum", "return_pct", "return", "intraday_return_pct"):
        if key in row:
            value = _number(row.get(key))
            if value is not None:
                return value
    return None


def _row_gap(row: Mapping[str, Any]) -> float | None:
    return _number(row.get("gap_pct"))


def _cross_sectional_percentile(values: Sequence[float], target: float) -> float | None:
    """Exact midrank percentile in [0, 1], stable under input permutation."""

    if not values:
        return None
    ordered = sorted(values)
    less = sum(value < target for value in ordered)
    equal = sum(value == target for value in ordered)
    return (less + (equal + 1) / 2.0) / len(ordered)


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def _liquidity_observation(
    row: Mapping[str, Any],
) -> tuple[float | None, float | None, float | None, float | None]:
    price = _number(row.get("premarket_price") or row.get("price") or row.get("close"))
    volume = _number(row.get("volume") or row.get("average_volume") or row.get("adv"))
    dollar_volume = _number(row.get("dollar_volume") or row.get("average_dollar_volume"))
    if (
        dollar_volume is None
        and price is not None
        and volume is not None
        and price > 0
        and volume >= 0
    ):
        dollar_volume = price * volume
    turnover = _number(row.get("turnover") or row.get("turnover_pct"))
    if turnover is None:
        shares_outstanding = _number(row.get("shares_outstanding") or row.get("float_shares"))
        if volume is not None and shares_outstanding is not None and shares_outstanding > 0:
            turnover = volume / shares_outstanding
    spread = _number(row.get("spread_bps") or row.get("quoted_spread_bps"))
    if spread is None:
        bid = _number(row.get("bid"))
        ask = _number(row.get("ask"))
        mid = (
            _safe_ratio((bid or 0) + (ask or 0), 2.0)
            if bid is not None and ask is not None
            else None
        )
        spread = _safe_ratio(
            (ask - bid) * 10000.0 if bid is not None and ask is not None else None, mid
        )
    cost = _number(row.get("round_trip_cost_bps") or row.get("estimated_round_trip_cost_bps"))
    if cost is None and spread is not None:
        cost = max(0.0, spread)
    return dollar_volume, turnover, spread, cost


def _timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp has no defensible exchange/session timezone.  Treat it
    # as unavailable instead of silently assigning UTC and creating lookahead.
    return None if parsed.tzinfo is None else parsed


def _text(value: object) -> str:
    return str(value or "").strip()


def _hashes(values: object) -> list[str]:
    if isinstance(values, str):
        return [values] if values.strip() else []
    if not isinstance(values, (list, tuple, set)):
        return []
    return sorted({_text(item) for item in values if _text(item)})


def _is_sha256(value: object) -> bool:
    text = _text(value)
    return (
        len(text) == 64
        and text == text.lower()
        and all(char in "0123456789abcdef" for char in text)
    )


def _is_code_hash(value: object) -> bool:
    text = _text(value)
    return (
        len(text) in {40, 64}
        and text == text.lower()
        and all(char in "0123456789abcdef" for char in text)
    )


def _canonical_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _window_contains(window: Mapping[str, Any], timestamp: datetime) -> bool:
    start = _timestamp(window.get("start") or window.get("start_at"))
    end = _timestamp(window.get("end") or window.get("end_at"))
    return start is not None and end is not None and start <= timestamp <= end


def _record_hash(record: Mapping[str, Any]) -> str | None:
    value = record.get("source_hash_sha256") or record.get("source_content_hash_sha256")
    return _text(value) if _is_sha256(value) else None


def _lineaged_records(value: object) -> tuple[list[Mapping[str, Any]], list[str]]:
    records = _as_bars(value)
    hashes = [_record_hash(record) for record in records]
    if not records or any(item is None for item in hashes):
        return records, []
    return records, sorted({item for item in hashes if item is not None})


def _pit_lineaged_records(
    value: object, decision_at: datetime
) -> tuple[list[Mapping[str, Any]], list[str], bool]:
    records = _as_bars(value)
    pit_records = [
        record
        for record in records
        if (stamp := _bar_timestamp(record)) is not None and stamp <= decision_at
    ]
    hashes = [_record_hash(record) for record in pit_records]
    return (
        pit_records,
        sorted({item for item in hashes if item is not None}),
        bool(pit_records) and all(item is not None for item in hashes),
    )


def _bar_timestamp(bar: Mapping[str, Any]) -> datetime | None:
    return _timestamp(
        bar.get("observed_at")
        or bar.get("timestamp")
        or bar.get("as_of")
        or bar.get("bar_at")
        or bar.get("date")
    )


def _close(bar: Mapping[str, Any]) -> float | None:
    return _number(bar.get("close") if "close" in bar else bar.get("value"))


def _return_value(bar: Mapping[str, Any]) -> float | None:
    value = _number(bar.get("return_pct") if "return_pct" in bar else bar.get("return"))
    if value is not None:
        return value
    close = _close(bar)
    previous = _number(bar.get("previous_close") or bar.get("prior_close"))
    if close is None or previous is None or previous <= 0:
        return None
    return (close / previous - 1.0) * 100.0


def _as_bars(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        nested = value.get("bars")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            return [item for item in nested if isinstance(item, Mapping)]
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _pit_bars(value: object, decision_at: datetime) -> list[Mapping[str, Any]]:
    return sorted(
        [
            bar
            for bar in _as_bars(value)
            if (stamp := _bar_timestamp(bar)) is not None and stamp <= decision_at
        ],
        key=lambda bar: (
            _bar_timestamp(bar) or datetime.min.replace(tzinfo=timezone.utc),
            canonical_hash(dict(bar)),
        ),
    )


def _time_integrity(value: object, decision_at: datetime) -> bool:
    """Return false for supplied naive, malformed, or future observations."""

    for bar in _as_bars(value):
        raw = (
            bar.get("observed_at")
            or bar.get("timestamp")
            or bar.get("as_of")
            or bar.get("bar_at")
            or bar.get("date")
        )
        stamp = _bar_timestamp(bar)
        if raw in (None, "") or stamp is None or stamp > decision_at:
            return False
    return True


def _lineage_fields_match(
    records: Sequence[Mapping[str, Any]],
    *,
    config_hash: str,
    code_hash: str | None,
    window_hash: str,
    model_hash: str | None,
) -> bool:
    """Reject mixed source/config/code/window/model identities in one vector."""

    for record in records:
        for key, expected, validator in (
            ("config_hash_sha256", config_hash, _is_sha256),
            ("window_hash_sha256", window_hash, _is_sha256),
            ("code_hash_sha256", code_hash, _is_code_hash),
            ("model_hash_sha256", model_hash, _is_sha256),
        ):
            if key not in record:
                continue
            actual = record.get(key)
            if expected is None or not validator(actual) or _text(actual) != _text(expected):
                return False
    return True


def _point_in_time_returns(value: object, decision_at: datetime) -> list[float]:
    bars = _pit_bars(value, decision_at)
    returns: list[float] = []
    previous_close: float | None = None
    for bar in bars:
        close = _close(bar)
        bar_return = _return_value(bar)
        if bar_return is None and close is not None and previous_close and previous_close > 0:
            bar_return = (close / previous_close - 1.0) * 100.0
        if bar_return is not None and math.isfinite(bar_return):
            returns.append(bar_return)
        if close is not None and close > 0:
            previous_close = close
    return returns


def _feature_hash(payload: Mapping[str, Any]) -> str:
    return canonical_hash(payload)


def _observed_with_lineage(
    value: float,
    *,
    sample_size: int,
    source_hashes: Sequence[str],
    reason: str = "",
) -> dict[str, Any]:
    if not source_hashes:
        return _unknown(reason or "source_lineage_missing")
    return _observed(value, sample_size=sample_size, source_hashes=source_hashes)


def _bind_observation(
    observation: dict[str, Any],
    *,
    decision_at: str | None,
    config_hash: str | None,
    code_hash: str | None,
    window_hash: str | None,
    input_hash: str | None,
    minimum_sample_size: int,
) -> dict[str, Any]:
    """Attach the complete immutable identity to each observed feature."""

    if observation.get("status") != OBSERVED:
        return {
            **observation,
            "sample_size": observation.get("sample_size", 0),
            "minimum_sample_size": minimum_sample_size,
        }
    return {
        **observation,
        "decision_at": decision_at,
        "config_hash_sha256": config_hash,
        "code_hash_sha256": code_hash,
        "window_hash_sha256": window_hash,
        "input_hash_sha256": input_hash,
        "minimum_sample_size": minimum_sample_size,
    }


def _unknown_features(
    reason: str, *, minimums: Mapping[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    return {
        name: _unknown(reason)
        | {"sample_size": 0, "minimum_sample_size": _minimum_sample_size(name, minimums)}
        for name in FEATURE_NAMES
    }


def feature_schema_v2() -> dict[str, Any]:
    """Return the stable, additive schema descriptor used by v2 vectors."""

    return {
        "schema_version": FEATURE_SCHEMA_V2,
        "feature_names": list(FEATURE_NAMES),
        "feature_blocks": [
            "point_in_time_regime",
            "cross_sectional_normalization",
            "liquidity_cost_proxies",
            "residual_returns",
            "decision_time_catalyst_evidence",
        ],
        "missing_value_status": UNKNOWN,
        "universe_contract": dict(V2_UNIVERSE_CONTRACT),
        "research_only": True,
        "broker_execution_enabled": False,
        "cycle7_shadow_policy_hash_sha256": CYCLE7_REGIME_POLICY_HASH_SHA256,
    }


def build_cycle2_feature_vector(
    candidate: Mapping[str, Any],
    *,
    decision_id: str | None = None,
    decision_at: str,
    benchmark_bars: object = (),
    sector_bars: Mapping[str, object] | None = None,
    universe_rows: Sequence[Mapping[str, Any]] = (),
    source_hashes: Sequence[str] = (),
    config: Mapping[str, Any] | None = None,
    config_hash_sha256: str | None = None,
    code_hash_sha256: str | None = None,
    model_hash_sha256: str | None = None,
    evaluation_window: Mapping[str, Any] | None = None,
    window_hash_sha256: str | None = None,
    source_manifest_hash_sha256: str | None = None,
    catalyst_events: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build an additive v2 vector from facts visible at ``decision_at``.

    Benchmark and sector bars must carry timestamps.  A missing benchmark,
    sector, or universe observation is represented as UNKNOWN rather than a
    neutral value.  Candidate prices/gaps outside the v2 contract are rejected
    from the v2 universe and never become an invented ``120%`` regime.
    """

    decision = _timestamp(decision_at)
    canonical_decision_at = _canonical_utc(decision) if decision is not None else None
    config_payload = dict(config) if config is not None else {"universe": V2_UNIVERSE_CONTRACT}
    cycle7_minimums = config_payload.get("minimum_sample_sizes")
    if not isinstance(cycle7_minimums, Mapping):
        cycle7_minimums = {}
    effective_minimums = {**CYCLE7_FEATURE_MINIMUMS, **dict(cycle7_minimums)}
    computed_config_hash = canonical_hash(config_payload)
    lineage_ok = bool(
        decision_id
        and decision is not None
        and config_hash_sha256
        and _is_sha256(config_hash_sha256)
        and config_hash_sha256 == computed_config_hash
        and evaluation_window
        and window_hash_sha256
        and _is_sha256(window_hash_sha256)
        and window_hash_sha256 == canonical_hash(dict(evaluation_window))
        and (_window_contains(dict(evaluation_window), decision) if decision is not None else False)
        and _is_code_hash(code_hash_sha256)
        and _record_hash(candidate) is not None
    )
    supplied_hashes = _hashes(source_hashes)
    supplied_hashes_valid = all(_is_sha256(item) for item in supplied_hashes)
    benchmark_hashes: list[str] = []
    market_lineage: list[str] = []
    sector_hashes: list[str] = []
    catalyst_join = join_decision_time_catalyst_evidence(
        {
            "ticker": candidate.get("ticker"),
            "decision_id": decision_id,
            "decision_at": canonical_decision_at,
        },
        catalyst_events,
    )
    if decision is None:
        features = _unknown_features("invalid_decision_timestamp", minimums=effective_minimums)
        status = "BLOCKED_INVALID_DECISION_TIME"
    elif not lineage_ok:
        features = _unknown_features("exact_feature_lineage_required", minimums=effective_minimums)
        status = "BLOCKED_MISSING_EXACT_LINEAGE"
    else:
        price = _number(candidate.get("premarket_price") or candidate.get("price"))
        gap = _number(candidate.get("gap_pct"))
        if price is None or not (1.0 <= price <= 500.0):
            universe_reason = "candidate_price_outside_v2_universe"
        elif gap is None or not (1.0 <= abs(gap) <= 50.0):
            universe_reason = "candidate_gap_outside_v2_universe"
        else:
            universe_reason = ""
        if universe_reason:
            features = _unknown_features(universe_reason, minimums=effective_minimums)
            status = "V2_UNIVERSE_EXCLUDED"
        else:
            # Preserve the established Cycle 2 behavior of excluding a future
            # benchmark bar from volatility (rather than treating it as a
            # market-input corruption).  Future/naive universe and sector
            # rows remain hard integrity failures below.
            input_integrity_ok = all(
                _bar_timestamp(bar) is not None for bar in _as_bars(benchmark_bars)
            )
            status = "PARTIAL_UNKNOWN"
            benchmark_records, benchmark_hashes, benchmark_lineage_ok = _pit_lineaged_records(
                benchmark_bars, decision
            )
            input_integrity_ok = input_integrity_ok and _lineage_fields_match(
                benchmark_records,
                config_hash=computed_config_hash,
                code_hash=code_hash_sha256,
                window_hash=_text(window_hash_sha256),
                model_hash=model_hash_sha256,
            )
            benchmark_returns = _point_in_time_returns(benchmark_records, decision)
            volatility = (
                _observed_with_lineage(
                    pstdev(benchmark_returns),
                    sample_size=len(benchmark_returns),
                    source_hashes=benchmark_hashes,
                    reason="benchmark_source_lineage_missing",
                )
                if len(benchmark_returns) >= 2 and benchmark_lineage_ok
                else _unknown("insufficient_point_in_time_benchmark_returns")
            )
            market_rows: list[Mapping[str, Any]] = []
            invalid_market_input = False
            for row in universe_rows:
                raw_observed_at = row.get("observed_at") or row.get("timestamp") or row.get("as_of")
                observed_at = _timestamp(raw_observed_at)
                # A supplied timestamp that is malformed, naive, or in the
                # future is rejected rather than silently dropped.
                if raw_observed_at not in (None, "") and observed_at is None:
                    invalid_market_input = True
                if observed_at is not None and observed_at > decision:
                    invalid_market_input = True
                if observed_at is not None and observed_at <= decision:
                    market_rows.append(row)
            identities: dict[str, str] = {}
            for row in market_rows:
                identity = _row_identity(row)
                if identity is None:
                    continue
                row_identity_hash = _record_hash(row) or canonical_hash(dict(row))
                prior = identities.get(identity)
                if prior is not None:
                    invalid_market_input = True
                identities[identity] = row_identity_hash
            if invalid_market_input:
                features = _unknown_features(
                    "invalid_or_future_market_observation", minimums=effective_minimums
                )
                status = "BLOCKED_INPUT_INTEGRITY"
                market_rows = []
            input_integrity_ok = input_integrity_ok and _lineage_fields_match(
                market_rows,
                config_hash=computed_config_hash,
                code_hash=code_hash_sha256,
                window_hash=_text(window_hash_sha256),
                model_hash=model_hash_sha256,
            )
            market_hashes = [_record_hash(row) for row in market_rows]
            market_lineage = (
                sorted({item for item in market_hashes if item is not None})
                if market_rows and all(item is not None for item in market_hashes)
                else []
            )
            market_returns = [
                value for row in market_rows if (value := _return_value(row)) is not None
            ]
            breadth = (
                _observed_with_lineage(
                    sum(value > 0 for value in market_returns) / len(market_returns),
                    sample_size=len(market_returns),
                    source_hashes=market_lineage,
                    reason="market_source_lineage_missing",
                )
                if market_returns
                else _unknown("missing_point_in_time_market_breadth")
            )
            sector_values: list[float] = []
            sector_hashes.clear()
            sector_lineage_ok = True
            for bars in (sector_bars or {}).values():
                input_integrity_ok = input_integrity_ok and _time_integrity(bars, decision)
                records, record_hashes, records_lineage_ok = _pit_lineaged_records(bars, decision)
                input_integrity_ok = input_integrity_ok and _lineage_fields_match(
                    records,
                    config_hash=computed_config_hash,
                    code_hash=code_hash_sha256,
                    window_hash=_text(window_hash_sha256),
                    model_hash=model_hash_sha256,
                )
                sector_lineage_ok = sector_lineage_ok and records_lineage_ok
                values = _point_in_time_returns(records, decision)
                if values:
                    sector_values.append(values[-1])
                if records and record_hashes:
                    sector_hashes.extend(record_hashes)
            sector_breadth = (
                _observed_with_lineage(
                    sum(value > 0 for value in sector_values) / len(sector_values),
                    sample_size=len(sector_values),
                    source_hashes=sector_hashes,
                    reason="sector_source_lineage_missing",
                )
                if sector_values and sector_lineage_ok
                else _unknown("missing_point_in_time_sector_breadth")
            )
            gaps = [
                abs(value)
                for row in market_rows
                if (value := _number(row.get("gap_pct"))) is not None and 1.0 <= abs(value) <= 50.0
            ]
            dispersion = (
                _observed_with_lineage(
                    pstdev(gaps),
                    sample_size=len(gaps),
                    source_hashes=market_lineage,
                    reason="gap_source_lineage_missing",
                )
                if len(gaps) >= 2 and market_lineage
                else _unknown("insufficient_v2_gap_observations")
            )

            # Cycle 7 candidate-relative and cross-sectional observations.
            # All inputs are point-in-time rows; no post-decision row survives
            # the integrity gate above.  Percentiles use deterministic
            # midranks, while residuals are simple excess returns against the
            # observed market/sector return at the decision boundary.
            candidate_gap = _row_gap(candidate)
            candidate_momentum = _row_momentum(candidate)
            market_gaps = [
                value
                for row in market_rows
                if (value := _row_gap(row)) is not None and 1.0 <= abs(value) <= 50.0
            ]
            market_momentum = [
                value for row in market_rows if (value := _row_momentum(row)) is not None
            ]
            gap_target = abs(candidate_gap) if candidate_gap is not None else None
            gap_volatility_normalized = (
                _safe_ratio(gap_target, pstdev(market_gaps))
                if gap_target is not None
                and len(market_gaps)
                >= _minimum_sample_size("gap_volatility_normalized", effective_minimums)
                and pstdev(market_gaps) > 0
                else None
            )
            momentum_volatility_normalized = (
                _safe_ratio(candidate_momentum, pstdev(market_momentum))
                if candidate_momentum is not None
                and len(market_momentum)
                >= _minimum_sample_size("momentum_volatility_normalized", effective_minimums)
                and pstdev(market_momentum) > 0
                else None
            )
            gap_percentile = (
                _cross_sectional_percentile(market_gaps + [gap_target], gap_target)
                if gap_target is not None
                and len(market_gaps) + 1
                >= _minimum_sample_size("gap_percentile", effective_minimums)
                else None
            )
            momentum_percentile = (
                _cross_sectional_percentile(
                    market_momentum + [candidate_momentum], candidate_momentum
                )
                if candidate_momentum is not None
                and len(market_momentum) + 1
                >= _minimum_sample_size("momentum_percentile", effective_minimums)
                else None
            )
            dollar_volume, turnover, spread_bps, cost_bps = _liquidity_observation(candidate)
            # If the candidate has no liquidity fields, use its identity row
            # (when present) without manufacturing a zero.
            if all(value is None for value in (dollar_volume, turnover, spread_bps, cost_bps)):
                candidate_identity = _row_identity(candidate)
                for row in market_rows:
                    if candidate_identity and _row_identity(row) == candidate_identity:
                        dollar_volume, turnover, spread_bps, cost_bps = _liquidity_observation(row)
                        break
            # Means are order-independent and avoid making the last input row
            # an accidental (and permutation-sensitive) market proxy.
            market_return = sum(market_returns) / len(market_returns) if market_returns else None
            sector_return = sum(sector_values) / len(sector_values) if sector_values else None
            candidate_return = _row_momentum(candidate)
            residual_market = (
                candidate_return - market_return
                if candidate_return is not None and market_return is not None
                else None
            )
            residual_sector = (
                candidate_return - sector_return
                if candidate_return is not None and sector_return is not None
                else None
            )

            def obs(value: float | None, feature_name: str, reason: str) -> dict[str, Any]:
                minimum = _minimum_sample_size(feature_name, effective_minimums)
                sample = (
                    len(market_rows)
                    if feature_name not in {"residual_vs_sector"}
                    else len(sector_values)
                )
                if value is None:
                    return _unknown(reason) | {
                        "minimum_sample_size": minimum,
                        "sample_size": sample,
                    }
                if sample < minimum:
                    return _unknown("insufficient_sample_size") | {
                        "minimum_sample_size": minimum,
                        "sample_size": sample,
                    }
                return _observed_with_lineage(
                    value,
                    sample_size=sample,
                    source_hashes=market_lineage,
                    reason="feature_source_lineage_missing",
                )

            features = {
                "benchmark_volatility": volatility,
                "market_breadth": breadth,
                "sector_breadth": sector_breadth,
                "gap_dispersion": dispersion,
                "gap_volatility_normalized": obs(
                    gap_volatility_normalized,
                    "gap_volatility_normalized",
                    "missing_gap_or_gap_volatility",
                ),
                "momentum_volatility_normalized": obs(
                    momentum_volatility_normalized,
                    "momentum_volatility_normalized",
                    "missing_momentum_or_momentum_volatility",
                ),
                "gap_percentile": obs(
                    gap_percentile, "gap_percentile", "missing_cross_sectional_gap"
                ),
                "momentum_percentile": obs(
                    momentum_percentile, "momentum_percentile", "missing_cross_sectional_momentum"
                ),
                "dollar_volume_proxy": obs(
                    dollar_volume, "dollar_volume_proxy", "missing_dollar_volume"
                ),
                "turnover_proxy": obs(turnover, "turnover_proxy", "missing_turnover"),
                "spread_bps_proxy": obs(spread_bps, "spread_bps_proxy", "missing_spread"),
                "round_trip_cost_bps_proxy": obs(
                    cost_bps, "round_trip_cost_bps_proxy", "missing_cost"
                ),
                "residual_vs_market": obs(
                    residual_market, "residual_vs_market", "missing_candidate_or_market_return"
                ),
                "residual_vs_sector": obs(
                    residual_sector, "residual_vs_sector", "missing_candidate_or_sector_return"
                ),
            }
            if not input_integrity_ok or invalid_market_input:
                features = _unknown_features(
                    "invalid_or_future_observation", minimums=effective_minimums
                )
                status = "BLOCKED_INPUT_INTEGRITY"
            status = (
                "OBSERVED"
                # Preserve the Cycle 2 envelope status for existing consumers;
                # Cycle 7 partial observations remain explicit per-feature
                # UNKNOWN and are evaluated by its separate receipt.
                if all(features[name]["status"] == OBSERVED for name in FEATURE_NAMES[:4])
                else (status if status == "BLOCKED_INPUT_INTEGRITY" else "PARTIAL_UNKNOWN")
            )
    config_hash = computed_config_hash
    used_hashes = sorted(
        set(benchmark_hashes if decision is not None and lineage_ok else [])
        | set(market_lineage if decision is not None and lineage_ok else [])
        | set(sector_hashes if decision is not None and lineage_ok else [])
    )
    candidate_hash = _record_hash(candidate)
    if candidate_hash is not None and decision is not None and lineage_ok:
        used_hashes = sorted({*used_hashes, candidate_hash})
    if decision is not None and lineage_ok:
        used_hashes = sorted({*used_hashes, *catalyst_join.get("event_identity_hashes", [])})
    manifest_ok = not source_manifest_hash_sha256 or (
        _is_sha256(source_manifest_hash_sha256)
        and source_manifest_hash_sha256 == canonical_hash(used_hashes)
    )
    if source_hashes and (not supplied_hashes_valid or sorted(supplied_hashes) != used_hashes):
        manifest_ok = False
    if not manifest_ok:
        features = _unknown_features("source_manifest_mismatch", minimums=effective_minimums)
        status = "BLOCKED_SOURCE_MANIFEST_MISMATCH"
    source_manifest_hash = canonical_hash(used_hashes)
    input_hash = canonical_hash(
        {
            "decision_at": canonical_decision_at,
            "source_hashes": used_hashes,
            "config_hash_sha256": config_hash,
            "code_hash_sha256": _text(code_hash_sha256) or None,
            "model_hash_sha256": _text(model_hash_sha256) or None,
            "window_hash_sha256": _text(window_hash_sha256) or None,
        }
    )
    features = {
        name: _bind_observation(
            value,
            decision_at=canonical_decision_at,
            config_hash=config_hash,
            code_hash=_text(code_hash_sha256) or None,
            window_hash=_text(window_hash_sha256) or None,
            input_hash=input_hash,
            minimum_sample_size=_minimum_sample_size(name, effective_minimums),
        )
        for name, value in features.items()
    }
    body = {
        "schema_version": FEATURE_SCHEMA_V2,
        "candidate": {
            "ticker": _text(candidate.get("ticker")).upper(),
            "price": _number(candidate.get("premarket_price") or candidate.get("price")),
            "gap_pct": _number(candidate.get("gap_pct")),
        },
        "decision_id": _text(decision_id) or None,
        "decision_at": canonical_decision_at,
        "features": features,
        "status": status,
        "config_hash_sha256": config_hash,
        "config_hash": config_hash,
        "source_hashes": used_hashes,
        "source_lineage_hash_sha256": canonical_hash({"source_hashes": used_hashes}),
        "input_hash_sha256": input_hash,
        "source_manifest_hash_sha256": source_manifest_hash,
        "source_manifest_formula": "sha256(canonical_sorted_used_pit_source_hashes_v1)",
        "source_manifest_supplied": bool(source_hashes or source_manifest_hash_sha256),
        "code_hash_sha256": _text(code_hash_sha256) or None,
        "model_hash_sha256": _text(model_hash_sha256) or None,
        "evaluation_window": dict(evaluation_window or {}),
        "window_hash_sha256": _text(window_hash_sha256) or None,
        "point_in_time": {
            "all_inputs_observed_at_or_before_decision": decision is not None
            and status == "OBSERVED"
        },
        "universe_contract": dict(V2_UNIVERSE_CONTRACT),
        "catalyst_evidence": catalyst_join,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    body["feature_hash_sha256"] = _feature_hash(body)
    return body


def join_decision_time_catalyst_evidence(
    decision: Mapping[str, Any], events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Join only immutable catalyst events available at the exact decision time."""

    decision_at = _timestamp(decision.get("decision_at") or decision.get("timestamp"))
    ticker = _text(decision.get("ticker") or decision.get("symbol")).upper()
    decision_id = _text(decision.get("decision_id"))
    if decision_at is None or not ticker or not decision_id:
        return {
            "status": "BLOCKED_INVALID_DECISION_TIME",
            "event_ids": [],
            "event_hashes": [],
            "research_only": True,
            "broker_execution_enabled": False,
        }
    accepted: list[dict[str, Any]] = []
    for event in events:
        if _text(event.get("symbol") or event.get("ticker")).upper() != ticker:
            continue
        if (
            event.get("research_only") is not True
            or event.get("broker_execution_enabled") is not False
        ):
            continue
        # Availability is when the evidence was actually observed, not when
        # its publisher claims it was published.
        first_seen_at = _timestamp(event.get("first_seen_at"))
        available_at = _timestamp(event.get("available_at"))
        published_raw = event.get("published_at")
        event_decision_raw = event.get("decision_at")
        published_at = _timestamp(published_raw) if published_raw else None
        event_decision_at = _timestamp(event_decision_raw) if event_decision_raw else None
        if (
            first_seen_at is None
            or available_at is None
            or (published_raw and published_at is None)
            or (event_decision_raw and event_decision_at is None)
            or (published_at is not None and published_at > first_seen_at)
            or available_at < first_seen_at
            or first_seen_at > decision_at
            or available_at > decision_at
            or (event_decision_at is not None and event_decision_at != decision_at)
            or event.get("available_at_decision") is False
        ):
            continue
        event_id = _text(event.get("event_id"))
        source_hash_value = event.get("source_content_hash_sha256")
        content_hash_value = event.get("content_hash_sha256")
        if (
            source_hash_value
            and content_hash_value
            and _text(source_hash_value) != _text(content_hash_value)
        ):
            continue
        event_hash = _text(source_hash_value or content_hash_value)
        payload_hash_value = event.get("event_payload_hash_sha256")
        self_hash_value = event.get("event_self_hash_sha256")
        if (
            payload_hash_value
            and self_hash_value
            and _text(payload_hash_value) != _text(self_hash_value)
        ):
            continue
        self_hash = _text(payload_hash_value or self_hash_value)
        source_lineage_hash = _text(event.get("source_lineage_hash_sha256"))
        unsigned_event = {
            key: value
            for key, value in event.items()
            if key
            not in {
                "created_at",
                "event_payload_hash_sha256",
                "event_self_hash_sha256",
            }
        }
        expected_source_lineage = canonical_hash(
            {
                "source_kind": _text(event.get("source_kind")),
                "canonical_url": _text(event.get("canonical_url")),
                "source_content_hash_sha256": event_hash,
            }
        )
        if (
            not event_id
            or not _text(event.get("source_kind"))
            or not _text(event.get("canonical_url"))
            or not _is_sha256(event_hash)
            or not _is_sha256(self_hash)
            or not _is_sha256(source_lineage_hash)
            or canonical_hash(unsigned_event) != self_hash
            or source_lineage_hash != expected_source_lineage
        ):
            continue
        accepted.append(
            {
                "event_id": event_id,
                "event_hash_sha256": event_hash,
                "event_payload_hash_sha256": self_hash,
                "source_lineage_hash_sha256": source_lineage_hash,
                "available_at": _canonical_utc(available_at),
            }
        )
    accepted.sort(key=lambda item: (item["available_at"], item["event_id"]))
    canonical_decision_at = _canonical_utc(decision_at)
    join_body = {
        "decision_id": decision_id,
        "ticker": ticker,
        "decision_at": canonical_decision_at,
        "events": accepted,
    }
    return {
        "status": "EVIDENCE_JOINED" if accepted else "NO_DECISION_TIME_EVIDENCE",
        "decision_id": _text(decision.get("decision_id")) or None,
        "ticker": ticker,
        "decision_at": canonical_decision_at,
        "event_ids": [item["event_id"] for item in accepted],
        "event_hashes": [item["event_hash_sha256"] for item in accepted],
        "event_semantic_hashes": [item["event_payload_hash_sha256"] for item in accepted],
        "event_source_lineage_hashes": [item["source_lineage_hash_sha256"] for item in accepted],
        "event_identity_hashes": sorted(
            {
                digest
                for item in accepted
                for digest in (
                    item["event_hash_sha256"],
                    item["event_payload_hash_sha256"],
                    item["source_lineage_hash_sha256"],
                )
            }
        ),
        "joined_at_decision": True,
        "immutable": True,
        "join_hash_sha256": canonical_hash(join_body),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _authorized_outcome(row: Mapping[str, Any]) -> tuple[float, str] | None:
    """Resolve a return only through a governed authority, never row fields."""

    if not has_authenticated_committed_fill_truth(row):
        return None
    result = row.get("authenticated_outcome_payload") or row.get("outcome_payload")
    if not isinstance(result, Mapping) or result.get("authenticated") is not True:
        return None
    if (
        result.get("research_only") is not True
        or result.get("broker_execution_enabled") is not False
    ):
        return None
    if _text(result.get("decision_id")) != _text(row.get("decision_id")):
        return None
    if _text(result.get("market_date")) != _text(row.get("market_date")):
        return None
    if not _text(row.get("mode")):
        return None
    if result.get("mode") is not None and _text(result.get("mode")) != _text(row.get("mode")):
        return None
    for field in (
        "config_hash_sha256",
        "source_hash_sha256",
        "code_hash_sha256",
        "window_hash_sha256",
    ):
        if not _is_sha256(result.get(field)) or _text(result.get(field)) != _text(row.get(field)):
            return None
    payload_hash = _text(
        row.get("outcome_payload_hash_sha256") or row.get("return_payload_hash_sha256")
    )
    declared_payload_hash = _text(
        result.get("outcome_payload_hash_sha256") or result.get("return_payload_hash_sha256")
    )
    if not _is_sha256(payload_hash) or declared_payload_hash != payload_hash:
        return None
    unsigned_payload = {
        key: value
        for key, value in result.items()
        if key
        not in {
            "authenticated",
            "outcome_payload_hash_sha256",
            "return_payload_hash_sha256",
        }
    }
    if canonical_hash(unsigned_payload) != payload_hash:
        return None
    value = _number(result.get("net_excess_return_pct"))
    if value is None:
        return None
    return value, payload_hash


def _prediction_value(prediction: object) -> float | None:
    if not isinstance(prediction, Mapping):
        return None
    value = prediction.get("expected_net_excess_return_pct")
    if value is None:
        value = prediction.get("predicted_net_excess_return_pct")
    return _number(value)


def build_exact_common_oos_ablation_receipt(
    mode_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    config_hash_sha256: str | None = None,
    source_hash_sha256: str | None = None,
    code_hash_sha256: str | None = None,
    window_hash_sha256: str | None = None,
) -> dict[str, Any]:
    """Compare mode predictions on one exact, authenticated common-OOS cohort.

    The existing governed FillTruth boundary is the only outcome authority.
    ``outcome_status`` or a caller-supplied return/hash is never accepted as
    outcome truth.  Realized outcomes are joined once (from ``full``) and
    must have the exact same authenticated payload in every mode; only the
    prediction payload may differ by mode.
    """

    def blocked(reason: str) -> dict[str, Any]:
        return _blocked_ablation(
            reason,
            config_hash_sha256=config_hash_sha256,
            source_hash_sha256=source_hash_sha256,
            code_hash_sha256=code_hash_sha256,
            window_hash_sha256=window_hash_sha256,
        )

    required = (
        config_hash_sha256,
        source_hash_sha256,
        code_hash_sha256,
        window_hash_sha256,
    )
    if any(not _is_sha256(value) for value in required):
        return blocked("missing_exact_config_source_or_window_hash")
    if set(mode_rows) != set(CATALYST_ABLATION_MODES):
        return blocked("all_preregistered_ablation_modes_required")
    by_mode: dict[str, dict[str, Mapping[str, Any]]] = {}
    for mode in CATALYST_ABLATION_MODES:
        entries: dict[str, Mapping[str, Any]] = {}
        for row in mode_rows.get(mode, ()):
            decision_id = _text(row.get("decision_id"))
            if not decision_id:
                return blocked("missing_decision_id")
            if not _is_oos(row):
                return blocked("non_oos_row_in_ablation_cohort")
            if decision_id in entries:
                return blocked("duplicate_oos_decision_id")
            if _text(row.get("mode")) != mode:
                return blocked("mode_lineage_mismatch")
            if not _text(row.get("market_date")):
                return blocked("missing_market_date")
            if any(
                not _is_sha256(row.get(field)) or _text(row.get(field)) != _text(expected)
                for field, expected in (
                    ("config_hash_sha256", config_hash_sha256),
                    ("source_hash_sha256", source_hash_sha256),
                    ("code_hash_sha256", code_hash_sha256),
                    ("window_hash_sha256", window_hash_sha256),
                )
            ):
                return blocked("row_lineage_hash_mismatch")
            prediction = row.get("prediction_payload") or row.get("prediction")
            row_model_hash = _text(row.get("model_hash_sha256"))
            prediction_hash = _text(row.get("prediction_payload_hash_sha256"))
            receipt_hash = _text(row.get("prediction_receipt_hash_sha256"))
            receipt_payload = row.get("prediction_receipt_payload")
            if (
                not isinstance(prediction, Mapping)
                or not _is_sha256(prediction_hash)
                or canonical_hash(dict(prediction)) != prediction_hash
                or _text(prediction.get("decision_id")) != decision_id
                or _text(prediction.get("mode")) != mode
                or prediction.get("research_only") is not True
                or prediction.get("broker_execution_enabled") is not False
                or not _is_sha256(row_model_hash)
                or _prediction_value(prediction) is None
                or not isinstance(receipt_payload, Mapping)
                or not _is_sha256(receipt_hash)
                or canonical_hash(dict(receipt_payload)) != receipt_hash
                or _text(receipt_payload.get("decision_id")) != decision_id
                or _text(receipt_payload.get("mode")) != mode
                or _text(receipt_payload.get("prediction_payload_hash_sha256")) != prediction_hash
                or _text(receipt_payload.get("model_hash_sha256")) != row_model_hash
                or _text(receipt_payload.get("config_hash_sha256")) != _text(config_hash_sha256)
                or _text(receipt_payload.get("source_hash_sha256")) != _text(source_hash_sha256)
                or _text(receipt_payload.get("code_hash_sha256")) != _text(code_hash_sha256)
                or _text(receipt_payload.get("window_hash_sha256")) != _text(window_hash_sha256)
                or _text(receipt_payload.get("market_date")) != _text(row.get("market_date"))
                or receipt_payload.get("research_only") is not True
                or receipt_payload.get("broker_execution_enabled") is not False
            ):
                return blocked("prediction_receipt_lineage_invalid")
            entries[decision_id] = row
        by_mode[mode] = entries
        mode_model_hashes = {_text(row.get("model_hash_sha256")) for row in entries.values()}
        if len(mode_model_hashes) != 1:
            return blocked("multiple_model_hashes_without_predeclared_run_contract")
    cohorts = [set(rows) for rows in by_mode.values()]
    if not cohorts or any(cohort != cohorts[0] for cohort in cohorts[1:]):
        return blocked("common_oos_cohort_mismatch")
    common_ids = sorted(cohorts[0])
    if not common_ids:
        return blocked("no_exact_common_oos_rows")
    trusted_outcomes: dict[str, tuple[float, str]] = {}
    for decision_id in common_ids:
        full_outcome = _authorized_outcome(by_mode["full"][decision_id])
        if full_outcome is None:
            return blocked("authenticated_outcome_truth_required")
        trusted_outcomes[decision_id] = full_outcome
        for mode in CATALYST_ABLATION_MODES[1:]:
            mode_outcome = _authorized_outcome(by_mode[mode][decision_id])
            if (
                mode_outcome is None
                or mode_outcome[1] != full_outcome[1]
                or mode_outcome[0] != full_outcome[0]
            ):
                return blocked("common_oos_realized_outcome_mismatch")
    realized = [item[0] for item in trusted_outcomes.values()]
    metrics: dict[str, Any] = {}
    prediction_payload_hashes: dict[str, list[str]] = {}
    for mode in CATALYST_ABLATION_MODES:
        predictions = [
            _number(
                _prediction_value(
                    by_mode[mode][decision_id].get("prediction_payload")
                    or by_mode[mode][decision_id].get("prediction")
                )
            )
            for decision_id in common_ids
        ]
        if any(value is None for value in predictions):
            return blocked("prediction_value_missing")
        predicted = [value for value in predictions if value is not None]
        errors = [
            prediction - actual for prediction, actual in zip(predicted, realized, strict=True)
        ]
        mae = sum(abs(error) for error in errors) / len(errors)
        rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
        prediction_payload_hashes[mode] = [
            _text(by_mode[mode][decision_id].get("prediction_payload_hash_sha256"))
            for decision_id in common_ids
        ]
        metrics[mode] = {
            "sample_size": len(errors),
            "mean_absolute_error_pct": round(mae, 10),
            "root_mean_squared_error_pct": round(rmse, 10),
        }
    sessions = {
        str(by_mode["full"][decision_id].get("market_date") or "") for decision_id in common_ids
    }
    low_sample = len(common_ids) < 30 or len(sessions) < 5
    receipt_body = {
        "schema_version": "dawnstrike.alphaops_v6.cycle2_ablation_receipt.v1",
        "modes": list(CATALYST_ABLATION_MODES),
        "common_oos_decision_ids": common_ids,
        "metrics": metrics,
        "prediction_payload_hashes": prediction_payload_hashes,
        "prediction_receipt_hashes": {
            mode: [
                _text(by_mode[mode][decision_id].get("prediction_receipt_hash_sha256"))
                for decision_id in common_ids
            ]
            for mode in CATALYST_ABLATION_MODES
        },
        "common_oos_realized_outcome_payload_hashes": [
            item[1] for item in (trusted_outcomes[decision_id] for decision_id in common_ids)
        ],
        "config_hash_sha256": config_hash_sha256,
        "source_hash_sha256": source_hash_sha256,
        "code_hash_sha256": code_hash_sha256,
        "window_hash_sha256": window_hash_sha256,
        "model_hashes_by_mode": {
            mode: sorted(
                {
                    _text(by_mode[mode][decision_id].get("model_hash_sha256"))
                    for decision_id in common_ids
                }
            )
            for mode in CATALYST_ABLATION_MODES
        },
        "model_hash_by_mode": {
            mode: next(
                iter(
                    {
                        _text(by_mode[mode][decision_id].get("model_hash_sha256"))
                        for decision_id in common_ids
                    }
                )
            )
            for mode in CATALYST_ABLATION_MODES
        },
        "common_oos_session_count": len(sessions),
        "realized_return_diagnostic_pct": round(sum(realized) / len(realized), 10),
        "prediction_error_delta_vs_full": {
            mode: round(
                metrics[mode]["mean_absolute_error_pct"]
                - metrics["full"]["mean_absolute_error_pct"],
                10,
            )
            for mode in CATALYST_ABLATION_MODES
        },
        "exact_common_oos": True,
        "outcome_truth_authenticated": True,
        "dominant_catalyst_claim_allowed": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    receipt_body["status"] = "DIAGNOSTIC_LOW_SAMPLE" if low_sample else "EVALUABLE"
    return {
        **receipt_body,
        "receipt_hash_sha256": canonical_hash(receipt_body),
    }


def _blocked_ablation(
    reason: str,
    *,
    config_hash_sha256: str | None = None,
    source_hash_sha256: str | None = None,
    code_hash_sha256: str | None = None,
    window_hash_sha256: str | None = None,
) -> dict[str, Any]:
    body = {
        "schema_version": "dawnstrike.alphaops_v6.cycle2_ablation_receipt.v1",
        "status": "BLOCKED",
        "reason": reason,
        "exact_common_oos": False,
        "outcome_truth_authenticated": False,
        "dominant_catalyst_claim_allowed": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    for field, value in (
        ("config_hash_sha256", config_hash_sha256),
        ("source_hash_sha256", source_hash_sha256),
        ("code_hash_sha256", code_hash_sha256),
        ("window_hash_sha256", window_hash_sha256),
    ):
        body[field] = value if _is_sha256(value) else None
    body["receipt_hash_sha256"] = canonical_hash(body)
    return body


def _is_oos(row: Mapping[str, Any]) -> bool:
    return (
        row.get("is_oos") is True
        or row.get("oos") is True
        or _text(row.get("split")).upper() in {"OOS", "OUT_OF_SAMPLE", "TEST"}
    )


def cycle7_shadow_regime_policy() -> dict[str, Any]:
    """Return the immutable Cycle 7 policy descriptor and its hash binding."""

    policy: dict[str, Any] = dict(CYCLE7_REGIME_POLICY)
    policy["minimum_samples"] = dict(CYCLE7_REGIME_POLICY["minimum_samples"])
    policy["volatility"] = dict(CYCLE7_REGIME_POLICY["volatility"])
    policy["breadth"] = dict(CYCLE7_REGIME_POLICY["breadth"])
    policy["dispersion"] = dict(CYCLE7_REGIME_POLICY["dispersion"])
    policy["liquidity"] = dict(CYCLE7_REGIME_POLICY["liquidity"])
    policy["policy_hash_sha256"] = CYCLE7_REGIME_POLICY_HASH_SHA256
    return policy


def build_shadow_regime_interaction_receipt(
    feature_vector: Mapping[str, Any],
    *,
    decision_at: str | None = None,
    model_hash_sha256: str | None = None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a hash-bound, non-ranking Cycle 7 regime interaction receipt.

    This function only classifies research evidence.  It cannot alter scores,
    ranking, promotion, policy, or broker execution, and remains
    ``NOT_EVALUABLE`` until all exact lineage and sample minima are present.
    """

    policy_payload = cycle7_shadow_regime_policy() if policy is None else dict(policy)
    supplied_policy_hash = _text(policy_payload.pop("policy_hash_sha256", ""))
    policy_hash = canonical_hash(policy_payload)
    if supplied_policy_hash and supplied_policy_hash != policy_hash:
        reasons = ["policy_hash_mismatch"]
    else:
        reasons = []
    feature_input = feature_vector if isinstance(feature_vector, Mapping) else {}
    expected_decision = (
        _timestamp(decision_at) if decision_at else _timestamp(feature_input.get("decision_at"))
    )
    canonical_decision = _canonical_utc(expected_decision) if expected_decision else None
    if not isinstance(feature_vector, Mapping):
        reasons.append("feature_vector_missing")
        feature_vector = {}
    stored_feature_hash = _text(feature_vector.get("feature_hash_sha256"))
    feature_without_hash = {
        key: value for key, value in feature_vector.items() if key != "feature_hash_sha256"
    }
    if (
        not _is_sha256(stored_feature_hash)
        or canonical_hash(feature_without_hash) != stored_feature_hash
    ):
        reasons.append("feature_hash_invalid")
    if canonical_decision is None or _text(feature_vector.get("decision_at")) != canonical_decision:
        reasons.append("decision_time_invalid_or_mismatched")
    source_hashes = feature_vector.get("source_hashes")
    if (
        not isinstance(source_hashes, list)
        or source_hashes != sorted(set(source_hashes))
        or not source_hashes
        or not all(_is_sha256(item) for item in source_hashes)
    ):
        reasons.append("source_lineage_invalid")
    for key, validator in (
        ("config_hash_sha256", _is_sha256),
        ("window_hash_sha256", _is_sha256),
        ("input_hash_sha256", _is_sha256),
        ("code_hash_sha256", _is_code_hash),
        ("model_hash_sha256", _is_sha256),
    ):
        value = (
            model_hash_sha256
            if key == "model_hash_sha256" and model_hash_sha256 is not None
            else feature_vector.get(key)
        )
        if not validator(value):
            reasons.append(f"{key}_missing_or_invalid")
    if model_hash_sha256 is not None and _text(feature_vector.get("model_hash_sha256")) != _text(
        model_hash_sha256
    ):
        reasons.append("model_lineage_mismatch")
    if (
        feature_vector.get("research_only") is not True
        or feature_vector.get("broker_execution_enabled") is not False
    ):
        reasons.append("research_only_safety_contract_invalid")
    if feature_vector.get("schema_version") != FEATURE_SCHEMA_V2:
        reasons.append("feature_schema_invalid")

    required = {
        "benchmark_volatility": "benchmark_returns",
        "market_breadth": "market_rows",
        "sector_breadth": "sector_rows",
        "gap_dispersion": "gap_rows",
        "gap_volatility_normalized": "gap_rows",
        "momentum_volatility_normalized": "market_rows",
        "gap_percentile": "market_rows",
        "momentum_percentile": "market_rows",
        "dollar_volume_proxy": "liquidity_rows",
        "turnover_proxy": "liquidity_rows",
        "spread_bps_proxy": "liquidity_rows",
        "round_trip_cost_bps_proxy": "liquidity_rows",
        "residual_vs_market": "market_rows",
        "residual_vs_sector": "sector_rows",
    }
    features = feature_vector.get("features")
    if not isinstance(features, Mapping):
        reasons.append("feature_block_missing")
        features = {}
    for feature_name, minimum_key in required.items():
        observation = features.get(feature_name)
        observed_sample_size = 0
        minimum_values = policy_payload.get("minimum_samples", {})
        if not isinstance(minimum_values, Mapping):
            minimum_values = {}
        try:
            minimum = max(1, int(minimum_values.get(minimum_key, 1)))
        except (TypeError, ValueError):
            minimum = 1
        if not isinstance(observation, Mapping) or observation.get("status") != OBSERVED:
            reasons.append(f"{feature_name}_unknown")
        elif (
            isinstance(observation.get("value"), bool) or _number(observation.get("value")) is None
        ):
            reasons.append(f"{feature_name}_invalid_value")
        else:
            try:
                observed_sample_size = int(observation.get("sample_size", 0) or 0)
            except (TypeError, ValueError):
                observed_sample_size = 0
        if (
            isinstance(observation, Mapping)
            and observation.get("status") == OBSERVED
            and observed_sample_size < minimum
        ):
            reasons.append(f"{feature_name}_below_minimum")
    status = "EVALUABLE" if not reasons else "NOT_EVALUABLE"
    regime_values = {
        name: (
            _number(features.get(name, {}).get("value"))
            if isinstance(features.get(name), Mapping)
            else None
        )
        for name in (
            "benchmark_volatility",
            "market_breadth",
            "sector_breadth",
            "gap_dispersion",
            "dollar_volume_proxy",
            "round_trip_cost_bps_proxy",
        )
    }
    interaction = "UNKNOWN"
    if status == "EVALUABLE":
        vol = regime_values["benchmark_volatility"]
        breadth = regime_values["market_breadth"]
        dispersion = regime_values["gap_dispersion"]
        cost = regime_values["round_trip_cost_bps_proxy"]
        if (
            vol >= policy_payload["volatility"]["high_min"]
            or breadth <= policy_payload["breadth"]["risk_off_max"]
        ):
            interaction = "DEFENSIVE"
        elif (
            vol <= policy_payload["volatility"]["low_max"]
            and breadth >= policy_payload["breadth"]["risk_on_min"]
        ):
            interaction = "SUPPORTIVE"
        else:
            interaction = "SELECTIVE"
        if (
            dispersion >= policy_payload["dispersion"]["high_min"]
            or cost > policy_payload["liquidity"]["maximum_cost_bps"]
        ):
            interaction = "DEFENSIVE"
    body = {
        "schema_version": "dawnstrike.alphaops_v6.cycle7_regime_interaction_receipt.v1",
        "status": status,
        "reason_codes": sorted(set(reasons)),
        "decision_id": _text(feature_vector.get("decision_id")) or None,
        "decision_at": canonical_decision,
        "feature_hash_sha256": stored_feature_hash if _is_sha256(stored_feature_hash) else None,
        "model_hash_sha256": _text(model_hash_sha256 or feature_vector.get("model_hash_sha256"))
        or None,
        "policy_hash_sha256": policy_hash,
        "policy": policy_payload,
        "regime_interaction": interaction,
        "regime_inputs": regime_values,
        "ranking_mutated": False,
        "promotion_mutated": False,
        "policy_mutated": False,
        "broker_execution_enabled": False,
        "research_only": True,
    }
    body["receipt_hash_sha256"] = canonical_hash(body)
    return body


# Cycle 7 descriptive aliases for callers that use the tranche nomenclature.
build_cycle7_regime_receipt = build_shadow_regime_interaction_receipt
build_cycle7_shadow_regime_interaction_receipt = build_shadow_regime_interaction_receipt


# Descriptive aliases keep the contract discoverable to research callers while
# preserving one implementation and one receipt shape.
build_feature_vector_v2 = build_cycle2_feature_vector
build_pit_regime_features = build_cycle2_feature_vector
join_catalyst_evidence_at_decision = join_decision_time_catalyst_evidence
build_ablation_receipt = build_exact_common_oos_ablation_receipt


__all__ = [
    "CATALYST_ABLATION_MODES",
    "CYCLE7_FEATURE_MINIMUMS",
    "CYCLE7_REGIME_POLICY",
    "CYCLE7_REGIME_POLICY_HASH_SHA256",
    "FEATURE_NAMES",
    "FEATURE_SCHEMA_V2",
    "OBSERVED",
    "UNKNOWN",
    "V2_UNIVERSE_CONTRACT",
    "build_cycle2_feature_vector",
    "build_feature_vector_v2",
    "build_pit_regime_features",
    "build_ablation_receipt",
    "build_exact_common_oos_ablation_receipt",
    "build_shadow_regime_interaction_receipt",
    "build_cycle7_regime_receipt",
    "build_cycle7_shadow_regime_interaction_receipt",
    "cycle7_shadow_regime_policy",
    "feature_schema_v2",
    "join_decision_time_catalyst_evidence",
    "join_catalyst_evidence_at_decision",
]
