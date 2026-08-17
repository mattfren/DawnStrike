"""Pure point-in-time universe policy and snapshot builders."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from intraday_scanner.v2.data import MarketDataset
from intraday_scanner.v2.opportunity.capabilities import (
    CapabilityState,
    ProviderCapabilityReceipt,
)
from intraday_scanner.v2.opportunity.models import (
    EvidenceKind,
    OpportunityContract,
    stable_identity,
)


class SecurityType(str, Enum):
    COMMON_STOCK = "common_stock"
    ETF = "etf"
    ADR = "adr"
    OTC = "otc"
    WARRANT = "warrant"
    RIGHT = "right"
    UNIT = "unit"
    PREFERRED = "preferred"
    UNKNOWN = "unknown"


class UniverseMembershipStatus(str, Enum):
    INCLUDED = "included"
    EXCLUDED = "excluded"


class UniverseEligibility(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


class SafetyStatus(str, Enum):
    CLEAR = "clear"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UniversePolicy(OpportunityContract):
    policy_id: str
    version: str
    allowed_security_types: tuple[SecurityType, ...] = (SecurityType.COMMON_STOCK,)
    include_etfs: bool = False
    include_adrs: bool = False
    minimum_price: Decimal | None = None
    minimum_average_daily_dollar_volume: Decimal | None = None
    require_available_data: bool = True
    require_clear_halt_status: bool = True
    require_clear_corporate_action_status: bool = True
    admission_evidence_kind: EvidenceKind = EvidenceKind.HEURISTIC
    admission_rule_source: str = "bounded_unvalidated_universe_policy"
    schema_version: str = "v2.opportunity.universe_policy.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.policy_id, "policy_id")
        _require_text(self.version, "version")
        _require_text(self.admission_rule_source, "admission_rule_source")
        if len(self.allowed_security_types) != len(set(self.allowed_security_types)):
            raise ValueError("duplicate allowed security type")
        if SecurityType.UNKNOWN in self.allowed_security_types:
            raise ValueError("UNKNOWN cannot be an allowed security type")
        if SecurityType.COMMON_STOCK not in self.allowed_security_types:
            raise ValueError("common stock must remain the baseline allowed security type")
        if (SecurityType.ETF in self.allowed_security_types) is not self.include_etfs:
            raise ValueError("ETF policy membership requires explicit include_etfs opt-in")
        if (SecurityType.ADR in self.allowed_security_types) is not self.include_adrs:
            raise ValueError("ADR policy membership requires explicit include_adrs opt-in")
        if self.admission_evidence_kind is not EvidenceKind.HEURISTIC:
            raise ValueError(
                "universe admission rules must remain heuristic without typed validation evidence"
            )
        for value, name in (
            (self.minimum_price, "minimum_price"),
            (
                self.minimum_average_daily_dollar_volume,
                "minimum_average_daily_dollar_volume",
            ),
        ):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True)
class UniverseMemberFact(OpportunityContract):
    symbol: str
    security_type: SecurityType
    venue: str | None
    first_seen_at: datetime | None
    observed_at: datetime
    data_availability: CapabilityState
    halt_status: SafetyStatus
    corporate_action_status: SafetyStatus
    observed_price: Decimal | None
    average_daily_dollar_volume: Decimal | None
    provider_receipt_ids: tuple[str, ...]
    informational_reason_codes: tuple[str, ...] = ()
    declared_exclusion_reason_codes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.universe_member_fact.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_symbol(self.symbol)
        if self.venue is not None:
            _require_text(self.venue, "venue")
        if self.first_seen_at is not None and self.first_seen_at > self.observed_at:
            raise ValueError("first_seen_at cannot be after observed_at")
        for value, name in (
            (self.observed_price, "observed_price"),
            (self.average_daily_dollar_volume, "average_daily_dollar_volume"),
        ):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError(f"{name} must be finite and positive")
        _require_sorted_unique(self.provider_receipt_ids, "provider receipt ID")
        _require_unique(self.informational_reason_codes, "informational reason code")
        _require_unique(self.declared_exclusion_reason_codes, "declared exclusion reason code")
        _require_unique(self.limitations, "member fact limitation")
        _require_entries(self.informational_reason_codes, "informational reason code")
        _require_entries(
            self.declared_exclusion_reason_codes,
            "declared exclusion reason code",
        )
        _require_entries(self.limitations, "member fact limitation")


@dataclass(frozen=True)
class UniverseMember(OpportunityContract):
    member_id: str
    symbol: str
    membership_status: UniverseMembershipStatus
    security_type: SecurityType
    venue: str | None
    first_seen_at: datetime | None
    as_of: datetime
    admission_reason_codes: tuple[str, ...]
    exclusion_reason_codes: tuple[str, ...]
    informational_reason_codes: tuple[str, ...]
    eligibility: UniverseEligibility
    data_availability: CapabilityState
    halt_status: SafetyStatus
    corporate_action_status: SafetyStatus
    observed_price: Decimal | None
    average_daily_dollar_volume: Decimal | None
    provider_receipt_ids: tuple[str, ...]
    benchmark_only: bool
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.universe_member.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.member_id, "member_id")
        _require_symbol(self.symbol)
        if self.venue is not None:
            _require_text(self.venue, "venue")
        if self.first_seen_at is not None and self.first_seen_at > self.as_of:
            raise ValueError("first_seen_at cannot be after as_of")
        _require_unique(self.admission_reason_codes, "admission reason code")
        _require_unique(self.exclusion_reason_codes, "exclusion reason code")
        _require_unique(self.informational_reason_codes, "informational reason code")
        _require_sorted_unique(self.provider_receipt_ids, "provider receipt ID")
        _require_unique(self.limitations, "universe member limitation")
        _require_entries(self.admission_reason_codes, "admission reason code")
        _require_entries(self.exclusion_reason_codes, "exclusion reason code")
        _require_entries(self.informational_reason_codes, "informational reason code")
        _require_entries(self.limitations, "universe member limitation")
        for value, name in (
            (self.observed_price, "observed_price"),
            (self.average_daily_dollar_volume, "average_daily_dollar_volume"),
        ):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError(f"{name} must be finite and positive")
        if self.membership_status is UniverseMembershipStatus.INCLUDED:
            if self.eligibility is not UniverseEligibility.ELIGIBLE:
                raise ValueError("included universe member must be eligible")
            if self.exclusion_reason_codes:
                raise ValueError("included universe member cannot carry exclusion reasons")
            if not self.admission_reason_codes:
                raise ValueError("included universe member requires admission reasons")
        else:
            if self.eligibility is UniverseEligibility.ELIGIBLE:
                raise ValueError("excluded universe member cannot be eligible")
            if not self.exclusion_reason_codes:
                raise ValueError("excluded universe member requires exclusion reasons")
        expected = stable_identity("universe-member", _member_identity_payload(self))
        if self.member_id != expected:
            raise ValueError("universe member identity does not match content")


@dataclass(frozen=True)
class UniverseSnapshot(OpportunityContract):
    universe_snapshot_id: str
    decision_at: datetime
    as_of: datetime
    policy_id: str
    policy_hash: str
    dataset_id: str
    dataset_content_id: str
    source_identity: str
    provider_receipt_ids: tuple[str, ...]
    requested_symbols: tuple[str, ...]
    included_members: tuple[UniverseMember, ...]
    excluded_members: tuple[UniverseMember, ...]
    benchmark_member: UniverseMember | None
    requested_count: int
    included_count: int
    excluded_count: int
    unknown_metadata_count: int
    limitations: tuple[str, ...]
    bounded_coverage: bool = True
    schema_version: str = "v2.opportunity.universe_snapshot.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for value, name in (
            (self.universe_snapshot_id, "universe_snapshot_id"),
            (self.policy_id, "policy_id"),
            (self.policy_hash, "policy_hash"),
            (self.dataset_id, "dataset_id"),
            (self.dataset_content_id, "dataset_content_id"),
            (self.source_identity, "source_identity"),
        ):
            _require_text(value, name)
        if self.as_of > self.decision_at:
            raise ValueError("universe as_of cannot be after decision_at")
        _require_unique(self.requested_symbols, "requested symbol")
        _require_sorted_unique(self.provider_receipt_ids, "provider receipt ID")
        _require_unique(self.limitations, "universe snapshot limitation")
        _require_entries(self.limitations, "universe snapshot limitation")
        included_symbols = tuple(item.symbol for item in self.included_members)
        excluded_symbols = tuple(item.symbol for item in self.excluded_members)
        _require_unique((*included_symbols, *excluded_symbols), "universe member symbol")
        if tuple(sorted((*included_symbols, *excluded_symbols))) != self.requested_symbols:
            raise ValueError("included and excluded members must reconcile requested symbols")
        if any(
            item.membership_status is not UniverseMembershipStatus.INCLUDED
            or item.benchmark_only
            for item in self.included_members
        ):
            raise ValueError("included_members contains an invalid member disposition")
        if any(
            item.membership_status is not UniverseMembershipStatus.EXCLUDED
            or item.benchmark_only
            for item in self.excluded_members
        ):
            raise ValueError("excluded_members contains an invalid member disposition")
        if self.benchmark_member is not None and not self.benchmark_member.benchmark_only:
            raise ValueError("benchmark member must be marked benchmark_only")
        if self.benchmark_member is not None and self.benchmark_member.symbol in set(
            self.requested_symbols
        ):
            raise ValueError("benchmark member cannot also be a trade-universe member")
        all_members = (*self.included_members, *self.excluded_members)
        if self.benchmark_member is not None:
            all_members = (*all_members, self.benchmark_member)
        if any(item.as_of > self.as_of for item in all_members):
            raise ValueError("universe member cannot be observed after snapshot as_of")
        provider_ids = set(self.provider_receipt_ids)
        if any(set(item.provider_receipt_ids) - provider_ids for item in all_members):
            raise ValueError("universe member references receipt absent from snapshot")
        if self.requested_count != len(self.requested_symbols):
            raise ValueError("requested_count does not reconcile requested_symbols")
        if self.included_count != len(self.included_members):
            raise ValueError("included_count does not reconcile included_members")
        if self.excluded_count != len(self.excluded_members):
            raise ValueError("excluded_count does not reconcile excluded_members")
        if self.requested_count != self.included_count + self.excluded_count:
            raise ValueError("universe member counts do not reconcile")
        actual_unknown = sum(
            item.eligibility is UniverseEligibility.UNKNOWN
            for item in (*self.included_members, *self.excluded_members)
        )
        if self.unknown_metadata_count != actual_unknown:
            raise ValueError("unknown_metadata_count does not reconcile member states")
        if not self.bounded_coverage:
            raise ValueError("package-002 universe snapshots must remain bounded")
        expected = stable_identity("universe", _snapshot_identity_payload(self))
        if self.universe_snapshot_id != expected:
            raise ValueError("universe snapshot identity does not match content")

    @property
    def eligible_symbols(self) -> tuple[str, ...]:
        return tuple(item.symbol for item in self.included_members)


def build_universe_snapshot(
    dataset: MarketDataset,
    *,
    decision_at: datetime,
    as_of: datetime,
    policy: UniversePolicy,
    member_facts: tuple[UniverseMemberFact, ...],
    capability_receipts: tuple[ProviderCapabilityReceipt, ...],
    requested_symbols: tuple[str, ...] | None = None,
    benchmark_symbol: str | None = None,
    source_identity: str,
    limitations: tuple[str, ...] = (),
) -> UniverseSnapshot:
    """Materialize a bounded universe without performing current-data lookup."""

    if as_of > decision_at:
        raise ValueError("universe as_of cannot be after decision_at")
    _require_text(source_identity, "source_identity")
    requested_source = dataset.symbols if requested_symbols is None else requested_symbols
    _require_unique(requested_source, "requested symbol")
    normalized_values = tuple(_normalized_symbol(symbol) for symbol in requested_source)
    _require_unique(normalized_values, "normalized requested symbol")
    normalized_requested = tuple(sorted(normalized_values))
    benchmark = _normalized_symbol(benchmark_symbol) if benchmark_symbol is not None else None
    trade_symbols = tuple(symbol for symbol in normalized_requested if symbol != benchmark)

    facts_by_symbol = _facts_by_symbol(member_facts)
    allowed_fact_symbols = set(trade_symbols)
    if benchmark is not None:
        allowed_fact_symbols.add(benchmark)
    extras = set(facts_by_symbol) - allowed_fact_symbols
    if extras:
        raise ValueError(f"membership facts contain unrequested symbols: {sorted(extras)}")

    receipts_by_id = {item.capability_receipt_id: item for item in capability_receipts}
    if len(receipts_by_id) != len(capability_receipts):
        raise ValueError("duplicate provider capability receipt identity")
    if any(item.decision_at != decision_at for item in capability_receipts):
        raise ValueError("provider capability decision time differs from universe decision")
    if any(item.observed_at > as_of for item in capability_receipts):
        raise ValueError("provider capability cannot be observed after universe as_of")
    for symbol, bars in dataset.bars_by_symbol.items():
        previous_at: datetime | None = None
        for bar in bars:
            if bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None:
                raise ValueError("dataset bar timestamp must be timezone-aware")
            if bar.timestamp > as_of:
                raise ValueError("dataset bar cannot be observed after universe as_of")
            if previous_at is not None and bar.timestamp <= previous_at:
                raise ValueError("dataset bars must be strictly chronological and unique")
            previous_at = bar.timestamp
            if bar.symbol != symbol:
                raise ValueError("dataset bar symbol conflicts with dataset mapping")
    for fact in member_facts:
        if fact.observed_at > as_of:
            raise ValueError("membership fact cannot be observed after universe as_of")
        unknown_receipts = set(fact.provider_receipt_ids) - set(receipts_by_id)
        if unknown_receipts:
            raise ValueError(
                "membership fact references unknown capability receipts: "
                f"{sorted(unknown_receipts)}"
            )
        referenced = tuple(receipts_by_id[item] for item in fact.provider_receipt_ids)
        if any(item.observed_at > fact.observed_at for item in referenced):
            raise ValueError("member fact cannot predate its provider capability receipt")
        bars = dataset.bars_by_symbol.get(fact.symbol, ())
        bar_start = min((bar.timestamp for bar in bars), default=None)
        bar_end = max((bar.timestamp for bar in bars), default=None)
        if fact.data_availability is CapabilityState.AVAILABLE and not any(
            item.bars is CapabilityState.AVAILABLE
            and item.historical_coverage is CapabilityState.AVAILABLE
            and item.coverage_start is not None
            and item.coverage_end is not None
            and bar_start is not None
            and bar_end is not None
            and item.coverage_start <= bar_start
            and item.coverage_end >= bar_end
            for item in referenced
        ):
            raise ValueError(
                "available member data requires a referenced receipt covering the "
                "dataset bar interval"
            )

    members = tuple(
        _build_member(
            symbol,
            facts_by_symbol.get(symbol),
            policy=policy,
            dataset=dataset,
            as_of=as_of,
            benchmark_only=False,
        )
        for symbol in trade_symbols
    )
    benchmark_member = (
        _build_member(
            benchmark,
            facts_by_symbol.get(benchmark),
            policy=policy,
            dataset=dataset,
            as_of=as_of,
            benchmark_only=True,
        )
        if benchmark is not None
        else None
    )
    included = tuple(
        item for item in members if item.membership_status is UniverseMembershipStatus.INCLUDED
    )
    excluded = tuple(
        item for item in members if item.membership_status is UniverseMembershipStatus.EXCLUDED
    )
    dataset_content_id = market_dataset_content_id(dataset)
    snapshot_limitations = tuple(
        dict.fromkeys(
            (
                "bounded_caller_supplied_universe_not_full_market",
                *limitations,
                *(
                    ("point_in_time_membership_metadata_incomplete",)
                    if any(item.eligibility is UniverseEligibility.UNKNOWN for item in members)
                    else ()
                ),
            )
        )
    )
    values = {
        "decision_at": decision_at,
        "as_of": as_of,
        "policy_id": policy.policy_id,
        "policy_hash": policy.content_hash(),
        "dataset_id": dataset.dataset_id,
        "dataset_content_id": dataset_content_id,
        "source_identity": source_identity,
        "provider_receipt_ids": tuple(sorted(receipts_by_id)),
        "requested_symbols": trade_symbols,
        "included_members": included,
        "excluded_members": excluded,
        "benchmark_member": benchmark_member,
        "requested_count": len(trade_symbols),
        "included_count": len(included),
        "excluded_count": len(excluded),
        "unknown_metadata_count": sum(
            item.eligibility is UniverseEligibility.UNKNOWN for item in members
        ),
        "limitations": snapshot_limitations,
        "bounded_coverage": True,
        "schema_version": "v2.opportunity.universe_snapshot.v1",
    }
    return UniverseSnapshot(
        universe_snapshot_id=stable_identity("universe", values),
        decision_at=decision_at,
        as_of=as_of,
        policy_id=policy.policy_id,
        policy_hash=policy.content_hash(),
        dataset_id=dataset.dataset_id,
        dataset_content_id=dataset_content_id,
        source_identity=source_identity,
        provider_receipt_ids=tuple(sorted(receipts_by_id)),
        requested_symbols=trade_symbols,
        included_members=included,
        excluded_members=excluded,
        benchmark_member=benchmark_member,
        requested_count=len(trade_symbols),
        included_count=len(included),
        excluded_count=len(excluded),
        unknown_metadata_count=sum(
            item.eligibility is UniverseEligibility.UNKNOWN for item in members
        ),
        limitations=snapshot_limitations,
    )


def market_dataset_content_id(dataset: MarketDataset) -> str:
    """Bind a run to exact in-memory market bars and declared dataset lineage."""

    payload = {
        "dataset_id": dataset.dataset_id,
        "source_kind": dataset.source_kind,
        "timeframe": dataset.timeframe,
        "bars": tuple(
            (
                symbol,
                tuple(
                    (
                        bar.symbol,
                        bar.timestamp,
                        Decimal(str(bar.open)),
                        Decimal(str(bar.high)),
                        Decimal(str(bar.low)),
                        Decimal(str(bar.close)),
                        bar.volume,
                        Decimal(str(bar.vwap)) if bar.vwap is not None else None,
                        bar.exchange_session_id,
                        bar.price_adjustment_basis,
                    )
                    for bar in dataset.bars_by_symbol[symbol]
                ),
            )
            for symbol in dataset.symbols
        ),
        "warnings": dataset.warnings,
        "source_refs": dataset.source_refs,
    }
    return stable_identity("market-dataset", payload)


def _facts_by_symbol(
    facts: Iterable[UniverseMemberFact],
) -> dict[str, UniverseMemberFact]:
    result: dict[str, UniverseMemberFact] = {}
    for fact in facts:
        if fact.symbol in result:
            raise ValueError(f"duplicate or conflicting membership facts for {fact.symbol}")
        result[fact.symbol] = fact
    return result


def _build_member(
    symbol: str,
    fact: UniverseMemberFact | None,
    *,
    policy: UniversePolicy,
    dataset: MarketDataset,
    as_of: datetime,
    benchmark_only: bool,
) -> UniverseMember:
    if fact is None:
        fact = UniverseMemberFact(
            symbol=symbol,
            security_type=SecurityType.UNKNOWN,
            venue=None,
            first_seen_at=None,
            observed_at=as_of,
            data_availability=CapabilityState.UNKNOWN,
            halt_status=SafetyStatus.UNKNOWN,
            corporate_action_status=SafetyStatus.UNKNOWN,
            observed_price=None,
            average_daily_dollar_volume=None,
            provider_receipt_ids=(),
            declared_exclusion_reason_codes=("membership_metadata_absent",),
            limitations=("no_point_in_time_security_metadata",),
        )
    exclusions: list[str] = list(fact.declared_exclusion_reason_codes)
    if symbol not in dataset.bars_by_symbol:
        exclusions.append("dataset_symbol_absent")
    elif not dataset.bars_by_symbol[symbol]:
        exclusions.append("dataset_bars_unavailable")
    if fact.security_type is SecurityType.UNKNOWN:
        exclusions.append("security_type_unknown")
    elif fact.security_type not in policy.allowed_security_types:
        exclusions.append(f"security_type_not_allowed:{fact.security_type.value}")
    if policy.require_available_data and fact.data_availability is not CapabilityState.AVAILABLE:
        exclusions.append(f"data_availability_not_available:{fact.data_availability.value}")
    if policy.require_clear_halt_status and fact.halt_status is not SafetyStatus.CLEAR:
        exclusions.append(f"halt_status_not_clear:{fact.halt_status.value}")
    if (
        policy.require_clear_corporate_action_status
        and fact.corporate_action_status is not SafetyStatus.CLEAR
    ):
        exclusions.append(
            f"corporate_action_status_not_clear:{fact.corporate_action_status.value}"
        )
    if policy.minimum_price is not None:
        if fact.observed_price is None:
            exclusions.append("price_unavailable")
        elif fact.observed_price < policy.minimum_price:
            exclusions.append("price_below_policy_minimum")
    if policy.minimum_average_daily_dollar_volume is not None:
        if fact.average_daily_dollar_volume is None:
            exclusions.append("liquidity_unavailable")
        elif (
            fact.average_daily_dollar_volume
            < policy.minimum_average_daily_dollar_volume
        ):
            exclusions.append("liquidity_below_policy_minimum")
    exclusions = list(dict.fromkeys(exclusions))
    unknown = any(
        (
            fact.security_type is SecurityType.UNKNOWN,
            fact.data_availability is CapabilityState.UNKNOWN,
            fact.halt_status is SafetyStatus.UNKNOWN,
            fact.corporate_action_status is SafetyStatus.UNKNOWN,
            "price_unavailable" in exclusions,
            "liquidity_unavailable" in exclusions,
        )
    )
    eligible = not exclusions
    eligibility = (
        UniverseEligibility.ELIGIBLE
        if eligible
        else UniverseEligibility.UNKNOWN
        if unknown
        else UniverseEligibility.INELIGIBLE
    )
    admission = (
        (
            "security_type_allowed_by_versioned_policy",
            "point_in_time_safety_and_data_checks_satisfied",
        )
        if eligible
        else ()
    )
    values = {
        "symbol": symbol,
        "membership_status": (
            UniverseMembershipStatus.INCLUDED if eligible else UniverseMembershipStatus.EXCLUDED
        ),
        "security_type": fact.security_type,
        "venue": fact.venue,
        "first_seen_at": fact.first_seen_at,
        "as_of": fact.observed_at,
        "admission_reason_codes": admission,
        "exclusion_reason_codes": tuple(exclusions),
        "informational_reason_codes": fact.informational_reason_codes,
        "eligibility": eligibility,
        "data_availability": fact.data_availability,
        "halt_status": fact.halt_status,
        "corporate_action_status": fact.corporate_action_status,
        "observed_price": fact.observed_price,
        "average_daily_dollar_volume": fact.average_daily_dollar_volume,
        "provider_receipt_ids": fact.provider_receipt_ids,
        "benchmark_only": benchmark_only,
        "limitations": fact.limitations,
        "schema_version": "v2.opportunity.universe_member.v1",
    }
    return UniverseMember(
        member_id=stable_identity("universe-member", values),
        symbol=symbol,
        membership_status=(
            UniverseMembershipStatus.INCLUDED if eligible else UniverseMembershipStatus.EXCLUDED
        ),
        security_type=fact.security_type,
        venue=fact.venue,
        first_seen_at=fact.first_seen_at,
        as_of=fact.observed_at,
        admission_reason_codes=admission,
        exclusion_reason_codes=tuple(exclusions),
        informational_reason_codes=fact.informational_reason_codes,
        eligibility=eligibility,
        data_availability=fact.data_availability,
        halt_status=fact.halt_status,
        corporate_action_status=fact.corporate_action_status,
        observed_price=fact.observed_price,
        average_daily_dollar_volume=fact.average_daily_dollar_volume,
        provider_receipt_ids=fact.provider_receipt_ids,
        benchmark_only=benchmark_only,
        limitations=fact.limitations,
    )


def _member_identity_payload(member: UniverseMember) -> dict[str, object]:
    return {name: value for name, value in member.__dict__.items() if name != "member_id"}


def _snapshot_identity_payload(snapshot: UniverseSnapshot) -> dict[str, object]:
    return {
        name: value
        for name, value in snapshot.__dict__.items()
        if name != "universe_snapshot_id"
    }


def _normalized_symbol(value: str) -> str:
    normalized = value.strip().upper()
    _require_symbol(normalized)
    return normalized


def _require_symbol(value: str) -> None:
    if not value or value != value.strip().upper():
        raise ValueError("symbol must be nonblank canonical uppercase text")


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be blank")


def _require_unique(values: Iterable[str], label: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"duplicate {label}")


def _require_sorted_unique(values: tuple[str, ...], label: str) -> None:
    _require_unique(values, label)
    _require_entries(values, label)
    if values != tuple(sorted(values)):
        raise ValueError(f"{label} values must use canonical sorted order")


def _require_entries(values: tuple[str, ...], label: str) -> None:
    for value in values:
        _require_text(value, label)


__all__ = [
    "SafetyStatus",
    "SecurityType",
    "UniverseEligibility",
    "UniverseMember",
    "UniverseMemberFact",
    "UniverseMembershipStatus",
    "UniversePolicy",
    "UniverseSnapshot",
    "build_universe_snapshot",
    "market_dataset_content_id",
]
