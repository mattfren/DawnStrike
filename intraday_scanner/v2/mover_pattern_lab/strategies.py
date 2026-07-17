"""Frozen, deterministic mover hypotheses for forward paper observation.

These rules are deliberately conservative.  They do not predict a guaranteed
return and they never place an order.  A missing safety fact is distinct from a
verified negative fact and therefore produces a skipped decision.
"""

from __future__ import annotations

from datetime import date, time

from .contracts import (
    MARKET_TZ,
    MoverPaperSignal,
    MoverStrategyDecision,
    MoverStrategySpec,
    ProspectiveMoverSnapshot,
    stable_id,
)


def strategy_catalog() -> tuple[MoverStrategySpec, ...]:
    """Return the immutable initial catalog of forward-only hypotheses."""

    return (
        MoverStrategySpec(
            strategy_id="mover_opening_drive_rvol_v1",
            version="v1.0",
            display_name="Opening Drive + Same-Clock RVOL",
            description=(
                "Observes a completed opening-range drive only when price holds "
                "near the range high and above running VWAP on unusually strong, "
                "time-aligned volume."
            ),
            parameters={
                "start_time_et": "09:45",
                "end_time_et": "11:00",
                "minimum_gap_pct": 2.0,
                "maximum_gap_pct": 60.0,
                "minimum_same_clock_rvol": 2.5,
                "minimum_cumulative_dollar_volume": 5_000_000.0,
                "maximum_spread_pct": 1.5,
                "maximum_distance_from_opening_high_pct": 1.0,
                "minimum_reverse_split_age_days": 91,
                "minimum_offering_age_days": 31,
                "minimum_risk_pct": 0.25,
                "maximum_risk_pct": 8.0,
                "reward_risk_ratio": 2.0,
                "activation_market_date": "2026-07-20",
                "discovery_session_count": 18,
                "validation_session_count": 6,
                "locked_test_session_count": 6,
            },
            required_features=(
                "previous_close",
                "session_open",
                "opening_range_high",
                "opening_range_low",
                "running_vwap",
                "cumulative_dollar_volume",
                "same_clock_rvol",
                "spread_pct",
                "split_adjusted",
                "reverse_split_lookback_clear",
                "offering_lookback_clear",
                "halt_state",
                "source_conflict",
            ),
            entry_logic=(
                "At a completed bar from 09:45 through 11:00 ET: positive 2%-60% "
                "split-adjusted gap, same-clock RVOL >= 2.5, cumulative dollar "
                "volume >= USD 5m, price above VWAP and within 1% of the completed "
                "opening-range high. Simulated entry is the next bar open."
            ),
            stop_logic="Completed 09:30-09:45 opening-range low.",
            target_logic="Two times initial price risk above the signal reference.",
        ),
        MoverStrategySpec(
            strategy_id="mover_verified_catalyst_gap_hold_v1",
            version="v1.0",
            display_name="Verified Catalyst Gap Hold",
            description=(
                "Observes a catalyst-backed gap that holds its completed opening "
                "range and VWAP after the catalyst has verifiable pre-cutoff lineage."
            ),
            parameters={
                "start_time_et": "10:00",
                "end_time_et": "12:00",
                "minimum_gap_pct": 4.0,
                "maximum_gap_pct": 80.0,
                "minimum_same_clock_rvol": 2.0,
                "minimum_cumulative_dollar_volume": 5_000_000.0,
                "maximum_spread_pct": 2.0,
                "maximum_opening_range_break_pct": 0.5,
                "minimum_reverse_split_age_days": 91,
                "minimum_offering_age_days": 31,
                "minimum_risk_pct": 0.25,
                "maximum_risk_pct": 10.0,
                "reward_risk_ratio": 2.0,
                "activation_market_date": "2026-07-20",
                "discovery_session_count": 18,
                "validation_session_count": 6,
                "locked_test_session_count": 6,
            },
            required_features=(
                "previous_close",
                "session_open",
                "opening_range_high",
                "opening_range_low",
                "running_vwap",
                "cumulative_dollar_volume",
                "same_clock_rvol",
                "spread_pct",
                "split_adjusted",
                "reverse_split_lookback_clear",
                "offering_lookback_clear",
                "halt_state",
                "source_conflict",
                "catalyst_verified",
                "catalyst_published_at",
                "catalyst_source_url",
                "catalyst_source_type",
            ),
            entry_logic=(
                "At a completed bar from 10:00 through 12:00 ET: verified catalyst "
                "published no later than the feature cutoff, positive 4%-80% "
                "split-adjusted gap, same-clock RVOL >= 2.0, cumulative dollar "
                "volume >= USD 5m, opening range holds the prior close within "
                "0.5%, and price holds session open and VWAP."
            ),
            stop_logic=(
                "Lower of the completed opening-range low and 0.5% below running VWAP."
            ),
            target_logic="Two times initial price risk above the signal reference.",
        ),
    )


def evaluate_snapshot(
    spec: MoverStrategySpec,
    snapshot: ProspectiveMoverSnapshot,
) -> MoverStrategyDecision:
    """Evaluate one frozen strategy without mutating strategy state."""

    decision_id = stable_id(
        "mover_strategy_decision",
        spec.strategy_id,
        spec.version,
        snapshot.snapshot_id,
    )
    activation_date = date.fromisoformat(
        str(spec.parameters["activation_market_date"])
    )
    if (
        snapshot.evidence_mode == "forward_observation"
        and date.fromisoformat(snapshot.market_date) < activation_date
    ):
        return _decision(
            decision_id,
            spec,
            snapshot,
            "skipped",
            "strategy_not_active_for_forward_observation",
        )

    missing, vetoes = _common_safety_gate(spec, snapshot)
    if missing:
        return _decision(
            decision_id,
            spec,
            snapshot,
            "skipped",
            "required_point_in_time_truth_missing",
            missing_features=missing,
            vetoes=vetoes,
        )
    if vetoes:
        return _decision(
            decision_id,
            spec,
            snapshot,
            "rejected",
            "hard_risk_or_quality_veto",
            vetoes=vetoes,
        )

    if spec.strategy_id == "mover_opening_drive_rvol_v1":
        return _evaluate_opening_drive(decision_id, spec, snapshot)
    if spec.strategy_id == "mover_verified_catalyst_gap_hold_v1":
        return _evaluate_verified_catalyst(decision_id, spec, snapshot)
    raise ValueError(f"unsupported mover strategy: {spec.strategy_id}")


def _common_safety_gate(
    spec: MoverStrategySpec,
    snapshot: ProspectiveMoverSnapshot,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    missing: list[str] = []
    vetoes: list[str] = []

    if not snapshot.opening_range_complete:
        missing.append("completed_opening_range")
    for field in (
        "previous_close",
        "session_open",
        "opening_range_high",
        "opening_range_low",
        "running_vwap",
        "cumulative_dollar_volume",
        "same_clock_rvol",
        "spread_pct",
        "split_adjusted",
        "reverse_split_lookback_clear",
        "offering_lookback_clear",
        "source_conflict",
    ):
        if getattr(snapshot, field) is None:
            missing.append(field)
    if snapshot.halt_state in {"", "unknown", "unverified"}:
        missing.append("halt_state")

    if snapshot.split_adjusted is False:
        vetoes.append("prices_not_split_adjusted")
    if snapshot.reverse_split_lookback_clear is False:
        vetoes.append("recent_reverse_split")
    if snapshot.offering_lookback_clear is False:
        vetoes.append("recent_offering_or_dilution")
    if snapshot.source_conflict is True:
        vetoes.append("source_conflict")
    if snapshot.halt_state not in {"", "unknown", "unverified", "clear"}:
        vetoes.append(f"halt_state:{snapshot.halt_state}")
    if (
        snapshot.spread_pct is not None
        and snapshot.spread_pct > float(spec.parameters["maximum_spread_pct"])
    ):
        vetoes.append("spread_above_limit")

    return tuple(sorted(set(missing))), tuple(sorted(set(vetoes)))


def _evaluate_opening_drive(
    decision_id: str,
    spec: MoverStrategySpec,
    snapshot: ProspectiveMoverSnapshot,
) -> MoverStrategyDecision:
    params = spec.parameters
    reasons = _base_setup_rejections(spec, snapshot)
    opening_high = _present(snapshot.opening_range_high)
    running_vwap = _present(snapshot.running_vwap)
    distance_from_high = (opening_high - snapshot.price) / opening_high * 100.0
    if snapshot.price < running_vwap:
        reasons.append("price_below_running_vwap")
    if distance_from_high > float(params["maximum_distance_from_opening_high_pct"]):
        reasons.append("price_too_far_below_opening_range_high")
    if snapshot.price > opening_high * 1.03:
        reasons.append("price_overextended_above_opening_range")
    if reasons:
        return _decision(
            decision_id,
            spec,
            snapshot,
            "rejected",
            "setup_conditions_not_met",
            vetoes=tuple(reasons),
        )

    stop = _present(snapshot.opening_range_low)
    evidence = (
        f"gap_pct={_present(snapshot.gap_pct):.4f}",
        f"same_clock_rvol={_present(snapshot.same_clock_rvol):.4f}",
        (
            "cumulative_dollar_volume="
            f"{_present(snapshot.cumulative_dollar_volume):.2f}"
        ),
        f"distance_from_opening_high_pct={distance_from_high:.4f}",
        "price_at_or_above_running_vwap",
    )
    return _accepted_decision(decision_id, spec, snapshot, stop, evidence)


def _evaluate_verified_catalyst(
    decision_id: str,
    spec: MoverStrategySpec,
    snapshot: ProspectiveMoverSnapshot,
) -> MoverStrategyDecision:
    if snapshot.catalyst_verified is None:
        return _decision(
            decision_id,
            spec,
            snapshot,
            "skipped",
            "required_point_in_time_truth_missing",
            missing_features=("catalyst_verified",),
        )
    if snapshot.catalyst_verified is False:
        return _decision(
            decision_id,
            spec,
            snapshot,
            "rejected",
            "verified_pre_cutoff_catalyst_required",
            vetoes=("catalyst_not_verified",),
        )

    catalyst_missing = tuple(
        field
        for field, value in (
            ("catalyst_published_at", snapshot.catalyst_published_at),
            ("catalyst_source_url", snapshot.catalyst_source_url),
            ("catalyst_source_type", snapshot.catalyst_source_type),
        )
        if value in {None, ""}
    )
    if catalyst_missing:
        return _decision(
            decision_id,
            spec,
            snapshot,
            "skipped",
            "catalyst_lineage_missing",
            missing_features=catalyst_missing,
        )
    catalyst_published_at = snapshot.catalyst_published_at
    if catalyst_published_at is None:  # Type narrowing; guarded above.
        raise AssertionError("catalyst_published_at unexpectedly missing")

    params = spec.parameters
    reasons = _base_setup_rejections(spec, snapshot)
    previous_close = _present(snapshot.previous_close)
    opening_low = _present(snapshot.opening_range_low)
    running_vwap = _present(snapshot.running_vwap)
    allowed_break = float(params["maximum_opening_range_break_pct"])
    if opening_low < previous_close * (1.0 - allowed_break / 100.0):
        reasons.append("opening_range_failed_prior_close_hold")
    if snapshot.price < _present(snapshot.session_open):
        reasons.append("price_below_session_open")
    if snapshot.price < running_vwap:
        reasons.append("price_below_running_vwap")
    if reasons:
        return _decision(
            decision_id,
            spec,
            snapshot,
            "rejected",
            "setup_conditions_not_met",
            vetoes=tuple(reasons),
        )

    stop = min(opening_low, running_vwap * 0.995)
    evidence = (
        f"gap_pct={_present(snapshot.gap_pct):.4f}",
        f"same_clock_rvol={_present(snapshot.same_clock_rvol):.4f}",
        (
            "cumulative_dollar_volume="
            f"{_present(snapshot.cumulative_dollar_volume):.2f}"
        ),
        f"catalyst_published_at={catalyst_published_at.isoformat()}",
        f"catalyst_source_type={snapshot.catalyst_source_type}",
        "opening_range_and_vwap_hold",
    )
    return _accepted_decision(decision_id, spec, snapshot, stop, evidence)


def _base_setup_rejections(
    spec: MoverStrategySpec,
    snapshot: ProspectiveMoverSnapshot,
) -> list[str]:
    params = spec.parameters
    reasons: list[str] = []
    cutoff_clock = snapshot.feature_cutoff_at.astimezone(MARKET_TZ).time()
    start = _clock(str(params["start_time_et"]))
    end = _clock(str(params["end_time_et"]))
    if not start <= cutoff_clock <= end:
        reasons.append("outside_strategy_observation_window")
    gap = _present(snapshot.gap_pct)
    if gap < float(params["minimum_gap_pct"]):
        reasons.append("gap_below_minimum")
    if gap > float(params["maximum_gap_pct"]):
        reasons.append("gap_above_maximum")
    if _present(snapshot.same_clock_rvol) < float(
        params["minimum_same_clock_rvol"]
    ):
        reasons.append("same_clock_rvol_below_minimum")
    if _present(snapshot.cumulative_dollar_volume) < float(
        params["minimum_cumulative_dollar_volume"]
    ):
        reasons.append("cumulative_dollar_volume_below_minimum")
    return reasons


def _accepted_decision(
    decision_id: str,
    spec: MoverStrategySpec,
    snapshot: ProspectiveMoverSnapshot,
    stop: float,
    evidence: tuple[str, ...],
) -> MoverStrategyDecision:
    risk_pct = (snapshot.price - stop) / snapshot.price * 100.0
    minimum_risk = float(spec.parameters["minimum_risk_pct"])
    maximum_risk = float(spec.parameters["maximum_risk_pct"])
    if risk_pct < minimum_risk or risk_pct > maximum_risk:
        return _decision(
            decision_id,
            spec,
            snapshot,
            "rejected",
            "risk_distance_outside_frozen_bounds",
            vetoes=("invalid_initial_risk_distance",),
        )
    reward_risk = float(spec.parameters["reward_risk_ratio"])
    target = snapshot.price + (snapshot.price - stop) * reward_risk
    signal_id = stable_id(
        "mover_paper_signal",
        spec.strategy_id,
        spec.version,
        snapshot.snapshot_id,
    )
    features: dict[str, float | int | str | bool | None] = {
        "gap_pct": snapshot.gap_pct,
        "price": snapshot.price,
        "opening_range_high": snapshot.opening_range_high,
        "opening_range_low": snapshot.opening_range_low,
        "running_vwap": snapshot.running_vwap,
        "cumulative_volume": snapshot.cumulative_volume,
        "cumulative_dollar_volume": snapshot.cumulative_dollar_volume,
        "same_clock_rvol": snapshot.same_clock_rvol,
        "spread_pct": snapshot.spread_pct,
        "reverse_split_days": snapshot.reverse_split_days,
        "reverse_split_lookback_clear": snapshot.reverse_split_lookback_clear,
        "recent_offering_days": snapshot.recent_offering_days,
        "offering_lookback_clear": snapshot.offering_lookback_clear,
        "catalyst_verified": snapshot.catalyst_verified,
        "initial_risk_pct": risk_pct,
        "bar_interval_minutes": snapshot.raw_payload.get(
            "bar_interval_minutes"
        ),
    }
    score = _bounded_score(spec, snapshot, risk_pct)
    signal = MoverPaperSignal(
        signal_id=signal_id,
        strategy_id=spec.strategy_id,
        strategy_version=spec.version,
        strategy_semantics_fingerprint=str(
            spec.to_dict()["semantics_fingerprint"]
        ),
        market_date=snapshot.market_date,
        symbol=snapshot.symbol,
        signal_at=snapshot.feature_cutoff_at,
        snapshot_id=snapshot.snapshot_id,
        evidence_mode=snapshot.evidence_mode,
        source_captured_at=snapshot.source_captured_at,
        system_received_at=snapshot.system_received_at,
        forward_receipt_ref=snapshot.forward_receipt_ref,
        entry_reference=snapshot.price,
        stop=round(stop, 8),
        target=round(target, 8),
        score=score,
        evidence=evidence,
        warnings=(
            "unvalidated_forward_observation",
            "next_bar_fill_and_costs_required",
        ),
        source_refs=snapshot.source_refs,
        features=features,
    )
    return MoverStrategyDecision(
        decision_id=decision_id,
        strategy_id=spec.strategy_id,
        strategy_version=spec.version,
        market_date=snapshot.market_date,
        symbol=snapshot.symbol,
        snapshot_id=snapshot.snapshot_id,
        feature_cutoff_at=snapshot.feature_cutoff_at,
        evidence_mode=snapshot.evidence_mode,
        decision="paper_signal",
        reason="frozen_forward_observation_conditions_met",
        evidence=evidence,
        signal=signal,
    )


def _bounded_score(
    spec: MoverStrategySpec,
    snapshot: ProspectiveMoverSnapshot,
    risk_pct: float,
) -> float:
    """Rank observations; this is not a calibrated probability."""

    rvol_floor = float(spec.parameters["minimum_same_clock_rvol"])
    rvol_component = min(_present(snapshot.same_clock_rvol) / rvol_floor, 2.0) / 2.0
    liquidity_floor = float(spec.parameters["minimum_cumulative_dollar_volume"])
    liquidity_component = min(
        _present(snapshot.cumulative_dollar_volume) / liquidity_floor,
        3.0,
    ) / 3.0
    spread_limit = float(spec.parameters["maximum_spread_pct"])
    spread_component = max(0.0, 1.0 - _present(snapshot.spread_pct) / spread_limit)
    risk_midpoint = (
        float(spec.parameters["minimum_risk_pct"])
        + float(spec.parameters["maximum_risk_pct"])
    ) / 2.0
    risk_component = max(0.0, 1.0 - abs(risk_pct - risk_midpoint) / risk_midpoint)
    value = (
        0.35 * rvol_component
        + 0.30 * liquidity_component
        + 0.20 * spread_component
        + 0.15 * risk_component
    )
    return round(min(max(value, 0.0), 1.0), 6)


def _decision(
    decision_id: str,
    spec: MoverStrategySpec,
    snapshot: ProspectiveMoverSnapshot,
    decision: str,
    reason: str,
    *,
    evidence: tuple[str, ...] = (),
    missing_features: tuple[str, ...] = (),
    vetoes: tuple[str, ...] = (),
) -> MoverStrategyDecision:
    return MoverStrategyDecision(
        decision_id=decision_id,
        strategy_id=spec.strategy_id,
        strategy_version=spec.version,
        market_date=snapshot.market_date,
        symbol=snapshot.symbol,
        snapshot_id=snapshot.snapshot_id,
        feature_cutoff_at=snapshot.feature_cutoff_at,
        evidence_mode=snapshot.evidence_mode,
        decision=decision,
        reason=reason,
        evidence=evidence,
        missing_features=missing_features,
        vetoes=vetoes,
    )


def _clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def _present(value: float | None) -> float:
    if value is None:
        raise AssertionError("strategy evaluated a missing gated feature")
    return float(value)
