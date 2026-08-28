"""Selection-only outcome joins for the Morning research radar.

This sidecar deliberately does not model fills, entries, positions, or P&L.  It
binds an immutable frozen selection to sourced post-selection observations so
strategy contributors can be evaluated without changing their decision receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from intraday_scanner.decisioning.contracts import (
    ConditionResult,
    StrategyDecisionReceipt,
    canonical_json,
)
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.market_calendar import market_session
from intraday_scanner.providers.yahoo_chart_provider import (
    yahoo_chart_url,
    yahoo_provider_symbol,
)
from intraday_scanner.services.luna_research_slate_service import (
    AuthenticatedStrategyReceiptResolver,
    validate_ranked_research_slate,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

SCHEMA_VERSION = "dawnstrike.research_episode_outcome_bridge.v1"
RADAR_COHORT = "research_radar"
MISSING = "MISSING"
INELIGIBLE = "INELIGIBLE"
EASTERN = ZoneInfo("America/New_York")


def build_research_episode_outcome_bridges(
    selections: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]] | None = None,
    *,
    market_date: str,
    cutoff: str,
    source_identity: str = "",
    created_at: str | None = None,
    contributor_receipt_verifier: AuthenticatedStrategyReceiptResolver | None = None,
) -> list[dict[str, Any]]:
    """Build deterministic selection/contributor outcome joins.

    Outcomes are matched by exact ``selection_id`` or exact ``signal_id`` only;
    ticker/date matching is intentionally insufficient.  A missing contributor
    receipt therefore remains missing and cannot inherit a neighboring strategy.
    """

    day = str(market_date or "")[:10]
    cutoff_dt = _aware_datetime(cutoff, "cutoff")
    outcome_by_identity: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for outcome in outcomes or ():
        if not isinstance(outcome, Mapping):
            continue
        for key in ("selection_id", "signal_id"):
            identity = str(outcome.get(key) or "").strip()
            if identity:
                outcome_by_identity.setdefault((key, identity), []).append(outcome)

    bridges: list[dict[str, Any]] = []
    for selection in selections:
        if not isinstance(selection, Mapping):
            continue
        if str(selection.get("cohort") or "").strip() != RADAR_COHORT:
            continue
        base = _selection_identity(
            selection,
            day=day,
            cutoff=cutoff_dt,
            contributor_receipt_verifier=contributor_receipt_verifier,
        )
        source_outcome = _exact_outcome(selection, outcome_by_identity)
        contributors = _contributors(
            selection,
            expected_ticker=base["ticker"],
            expected_market_date=base["market_date"],
        )
        for contributor in contributors:
            bridges.append(
                _bridge_row(
                    base,
                    contributor,
                    source_outcome,
                    cutoff=cutoff_dt,
                    source_identity=source_identity,
                    created_at=created_at,
                )
            )
    return bridges


def persist_research_episode_outcome_bridges(
    store: SQLiteScanStore,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Persist a bridge batch idempotently through the immutable SQLite seam."""

    return store.persist_research_episode_outcome_bridges(rows)


def build_and_persist_research_episode_outcome_bridges(
    store: SQLiteScanStore,
    selections: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]] | None = None,
    *,
    market_date: str,
    cutoff: str,
    source_identity: str = "",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build and persist one governed radar bridge batch."""

    contributor_receipt_verifier = AuthenticatedStrategyReceiptResolver.from_store(
        store,
        market_date=str(market_date)[:10],
        strategy_id=None,
    )
    rows = build_research_episode_outcome_bridges(
        selections,
        outcomes,
        market_date=market_date,
        cutoff=cutoff,
        source_identity=source_identity,
        created_at=created_at,
        contributor_receipt_verifier=contributor_receipt_verifier,
    )
    stats = persist_research_episode_outcome_bridges(store, rows)
    persisted_rows = [
        dict(row)
        for row in stats.get("persisted_rows") or ()
        if isinstance(row, Mapping)
    ]
    eligible_count = sum(row.get("learning_eligible") is True for row in persisted_rows)
    missing_count = sum(
        str(row.get("outcome_status") or "").upper() == MISSING
        for row in persisted_rows
    )
    ineligible_count = sum(
        row.get("learning_eligible") is not True
        and str(row.get("outcome_status") or "").upper() != MISSING
        for row in persisted_rows
    )
    selection_ids = {
        str(selection.get("selection_id") or "")
        for selection in selections
        if isinstance(selection, Mapping)
        and str(selection.get("cohort") or "") == RADAR_COHORT
        and str(selection.get("selection_id") or "")
    }
    matched_outcome_ids = {
        (key, str(selection.get(key) or ""))
        for selection in selections
        if isinstance(selection, Mapping)
        and str(selection.get("cohort") or "") == RADAR_COHORT
        for key in ("selection_id", "signal_id")
        if str(selection.get(key) or "")
    }
    unmatched_count = sum(
        1
        for outcome in outcomes or ()
        if isinstance(outcome, Mapping)
        and not any(
            (key, str(outcome.get(key) or "")) in matched_outcome_ids
            for key in ("selection_id", "signal_id")
            if str(outcome.get(key) or "")
        )
    )
    expected_contributor_count = len(rows)
    actual_count = len(persisted_rows)
    ambiguous_count = sum(
        str(row.get("outcome_match_status") or "").upper() == "AMBIGUOUS"
        for row in persisted_rows
    )
    if (
        actual_count == expected_contributor_count
        and eligible_count == expected_contributor_count
        and not unmatched_count
        and not ambiguous_count
    ):
        status = "COMPLETE"
    elif eligible_count:
        status = "PARTIAL"
    else:
        status = "INELIGIBLE"
    return {
        **stats,
        "schema_version": SCHEMA_VERSION,
        "market_date": str(market_date)[:10],
        "bridges": persisted_rows,
        "status": status,
        "expected_selection_count": len(selection_ids),
        "expected_contributor_count": expected_contributor_count,
        "eligible_count": eligible_count,
        "missing_count": missing_count,
        "ineligible_count": ineligible_count,
        "unmatched_count": unmatched_count,
        "ambiguous_count": ambiguous_count,
        "research_only": True,
        "broker_execution_enabled": False,
    }
def load_research_episode_outcome_bridges(
    store: SQLiteScanStore,
    *,
    market_date: str | None = None,
    selection_id: str | None = None,
    receipt_id: str | None = None,
    limit: int = 50_000,
) -> list[dict[str, Any]]:
    return store.load_research_episode_outcome_bridges(
        market_date=market_date,
        selection_id=selection_id,
        receipt_id=receipt_id,
        limit=limit,
    )


# Naming aliases keep the sidecar easy to call from scheduled adapters that
# use either "join" or "bridge" terminology.
build_research_episode_outcome_join = build_research_episode_outcome_bridges
persist_research_episode_outcome_join = persist_research_episode_outcome_bridges
run_research_episode_outcome_bridge = build_and_persist_research_episode_outcome_bridges


def _selection_identity(
    selection: Mapping[str, Any],
    *,
    day: str,
    cutoff: datetime,
    contributor_receipt_verifier: AuthenticatedStrategyReceiptResolver | None,
) -> dict[str, Any]:
    selection_id = str(selection.get("selection_id") or "").strip()
    ticker = str(selection.get("ticker") or "").strip().upper()
    selected_at = str(selection.get("selected_at") or "").strip()
    if not selection_id or not ticker or not selected_at:
        raise SnapshotValidationError("research radar selection identity is incomplete")
    if str(selection.get("market_date") or day)[:10] != day:
        raise SnapshotValidationError("research radar selection crosses market date")
    selected_dt = _aware_datetime(selected_at, "selected_at")
    if selected_dt > cutoff:
        raise SnapshotValidationError("research radar selection is after outcome cutoff")
    payload_value = selection.get("payload_json")
    if isinstance(payload_value, str):
        payload_value = _decode_json(payload_value)
        if not isinstance(payload_value, Mapping):
            raise SnapshotValidationError("research radar selection payload is invalid")
    payload = payload_value if isinstance(payload_value, Mapping) else {}
    slate = selection.get("frozen_ranked_research_slate") or payload.get(
        "frozen_ranked_research_slate"
    )
    slate = _decode_mapping(slate)
    if not slate:
        raise SnapshotValidationError("research radar selection lacks frozen slate")
    slate = dict(slate)
    try:
        validate_ranked_research_slate(
            slate,
            market_date=day,
            production=True,
            contributor_receipt_verifier=contributor_receipt_verifier,
        )
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError("research radar frozen slate is not valid") from exc
    slate_id = str(slate.get("slate_id") or "").strip()
    slate_hash = str(slate.get("content_hash_sha256") or "").strip().lower()
    frozen_signal = _decode_mapping(payload.get("signal"))
    if not frozen_signal:
        raise SnapshotValidationError("research radar selection lacks frozen signal")
    for field in ("selection_id", "signal_id", "ticker", "market_date"):
        payload_value = payload.get(field)
        if payload_value in (None, ""):
            continue
        if field == "market_date":
            expected_value = str(selection.get(field) or day)[:10]
            actual_value = str(payload_value)[:10]
        elif field == "ticker":
            expected_value = str(selection.get(field) or "").upper()
            actual_value = str(payload_value).upper()
        else:
            expected_value = str(selection.get(field) or "")
            actual_value = str(payload_value)
        if actual_value != expected_value:
            raise SnapshotValidationError(
                f"research radar selection {field} conflicts with payload"
            )
    if (
        frozen_signal.get("market_date") not in (None, "")
        and str(frozen_signal.get("market_date"))[:10] != day
    ):
        raise SnapshotValidationError("research radar frozen signal crosses market date")
    selected_at_candidates = [
        payload.get("selected_at"),
        frozen_signal.get("selected_at"),
    ]
    frozen_selection_id = str(frozen_signal.get("research_selection_id") or "").strip()
    frozen_rows = [
        row
        for row in slate.get("rows") or ()
        if isinstance(row, Mapping)
        and str(row.get("research_selection_id") or "").strip()
        == frozen_selection_id
    ]
    if len(frozen_rows) != 1:
        raise SnapshotValidationError("research radar selection lacks one exact frozen row")
    frozen_row = frozen_rows[0]
    for field in ("signal_id", "ticker", "market_date", "episode_id"):
        asserted = frozen_signal.get(field)
        frozen = frozen_row.get(field)
        if asserted not in (None, "") and frozen not in (None, ""):
            left = (
                str(asserted).upper()
                if field == "ticker"
                else str(asserted)[:10]
                if field == "market_date"
                else str(asserted)
            )
            right = (
                str(frozen).upper()
                if field == "ticker"
                else str(frozen)[:10]
                if field == "market_date"
                else str(frozen)
            )
            if left != right:
                raise SnapshotValidationError(
                    f"research radar frozen row {field} conflicts with signal"
                )
        elif asserted not in (None, "") and frozen in (None, ""):
            raise SnapshotValidationError(f"research radar frozen row lacks {field}")
    row_selected_at = frozen_row.get("selected_at")
    if row_selected_at not in (None, ""):
        selected_at_candidates.append(row_selected_at)
    if _contributor_projection(
        frozen_signal.get("strategy_contributors")
    ) != _contributor_projection(frozen_row.get("strategy_contributors")):
        raise SnapshotValidationError(
            "research radar frozen row contributor receipts conflict with signal"
        )
    for row in slate.get("rows") or ():
        if (
            isinstance(row, Mapping)
            and str(row.get("research_selection_id") or "").strip()
            == frozen_selection_id
        ):
            selected_at_candidates.append(row.get("selected_at"))
            break
    for candidate in selected_at_candidates:
        if candidate in (None, ""):
            continue
        if _aware_datetime(str(candidate), "frozen selected_at") != selected_dt:
            raise SnapshotValidationError(
                "research radar selection timestamp conflicts with frozen lineage"
            )
    if not frozen_selection_id or frozen_selection_id not in {
        str(item).strip() for item in slate.get("selection_ids") or ()
    }:
        raise SnapshotValidationError("research radar selection is not a frozen slate member")
    if str(frozen_signal.get("signal_id") or "") != str(selection.get("signal_id") or ""):
        raise SnapshotValidationError(
            "research radar selection signal identity conflicts with slate"
        )
    if str(frozen_signal.get("ticker") or "").upper() != ticker:
        raise SnapshotValidationError("research radar selection ticker conflicts with slate")
    episode_values = [
        str(value).strip()
        for value in (
            selection.get("episode_id"),
            payload.get("episode_id"),
            frozen_signal.get("episode_id"),
        )
        if value not in (None, "")
    ]
    if len(set(episode_values)) > 1:
        raise SnapshotValidationError("research radar episode identity conflicts with lineage")
    episode_id = episode_values[0] if episode_values else ""
    if not episode_id:
        frozen_id = str(
            frozen_signal.get("research_selection_id")
            or selection.get("research_selection_id")
            or ""
        )
        for row in slate.get("rows") or ():
            if (
                isinstance(row, Mapping)
                and str(row.get("research_selection_id") or "") == frozen_id
            ):
                episode_id = str(row.get("episode_id") or "").strip()
                if episode_id:
                    break
    if not slate_id or not _sha256(slate_hash) or not episode_id:
        raise SnapshotValidationError("research radar frozen identity is incomplete")
    return {
        "selection_id": selection_id,
        "signal_id": str(selection.get("signal_id") or "").strip(),
        "slate_id": slate_id,
        "slate_content_hash_sha256": slate_hash,
        "episode_id": episode_id,
        "ticker": ticker,
        "market_date": day,
        "selected_at": selected_at,
        "selection_source_observation_id": _first(
            frozen_signal,
            "source_observation_id",
            "observation_id",
        ),
        "selection_source_observation_hash_sha256": _first(
            frozen_signal,
            "source_observation_hash_sha256",
            "enrichment_observation_sha256",
        ),
        "selection_source_path_id": _first(
            frozen_signal,
            "source_path_id",
            "path_replay_id",
            "path_id",
        ),
        "selection_source_path_hash_sha256": _first(
            frozen_signal,
            "source_path_hash_sha256",
            "replay_receipt_hash_sha256",
            "path_hash_sha256",
        ),
    }


def _contributors(
    selection: Mapping[str, Any],
    *,
    expected_ticker: str,
    expected_market_date: str,
) -> list[dict[str, Any]]:
    # Production radar rows persist the merged strategy rows under
    # payload_json.signal.strategy_contributors.  The store may return either
    # decoded JSON or a serialized envelope, so decode each envelope without
    # changing explicit-key precedence.  In particular, an explicitly empty
    # contributor list must not fall through to a lower-level alias.
    payload = _decode_mapping(selection.get("payload_json"))
    signal = _decode_mapping(payload.get("signal"))
    raw: Any = None
    raw_present = False
    for container in (selection, payload, signal):
        if "strategy_contributors" in container:
            raw = container.get("strategy_contributors")
            raw_present = True
            break
    rows: list[dict[str, Any]] = []
    if raw_present:
        decoded_raw = _decode_json(raw)
        if isinstance(decoded_raw, Sequence) and not isinstance(decoded_raw, (str, bytes)):
            rows = [
                dict(item)
                if isinstance(item, Mapping)
                else {"_invalid_contributor_index": index}
                for index, item in enumerate(decoded_raw)
            ]
        else:
            rows = [{"_invalid_contributor_index": 0}]
    elif signal:
        # Some older producers put the primary receipt directly on signal.
        # It is safe to retain it only when it is an authenticated receipt
        # identity; otherwise the identity-active path below stays ineligible.
        if any(
            signal.get(key) not in (None, "")
            for key in (
                "receipt_id",
                "receipt_hash_sha256",
                "strategy_decision_receipt",
                "decision_receipt",
            )
        ):
            rows = [dict(signal)]

    # A signal with frozen identity is an authenticated strategy join point.
    # Do not invent a receiptless cohort-level contributor when its contributor
    # list was stripped or malformed.  Retain one explicit ineligible row so
    # the gap is durable and cannot accidentally inherit another strategy.
    identity_active = bool(signal) and any(
        signal.get(key) not in (None, "")
        for key in (
            "signal_id",
            "research_selection_id",
            "episode_id",
            "strategy_id",
            "strategy_version",
        )
    )
    if not rows and identity_active:
        rows = [{
            "strategy_id": (
                signal.get("strategy_id") or selection.get("strategy_id") or RADAR_COHORT
            ),
            "strategy_version": (
                signal.get("strategy_version") or selection.get("strategy_version") or ""
            ),
            "_missing_authenticated_contributor": True,
        }]
    elif not rows and not identity_active:
        # Truly legacy, identity-absent rows remain readable for compatibility;
        # _selection_identity rejects these from the governed bridge path.
        rows = [dict(selection)]
    unique: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    receipt_bindings: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    for index, item in enumerate(rows):
        strategy_id = str(item.get("strategy_id") or item.get("decision_strategy_id") or "").strip()
        strategy_version = str(item.get("strategy_version") or "").strip()
        receipt = _decode_mapping(
            item.get("strategy_decision_receipt") or item.get("decision_receipt")
        )
        receipt_id = str(item.get("receipt_id") or "").strip()
        receipt_hash = str(item.get("receipt_hash_sha256") or "").strip().lower()
        receipt_status = str(
            item.get("receipt_status")
            or item.get("strategy_receipt_construction_status")
            or ""
        ).strip().upper()
        contributor_source_signal = str(
            item.get("source_signal_id")
            or item.get("prior_session_signal_id")
            or item.get("signal_id")
            or ""
        ).strip()
        outer_direction = _normalize_direction(
            item.get("direction") or item.get("trade_direction")
        )
        effective_receipt_status = "INVALID"
        canonical_receipt: dict[str, Any] | None = None
        input_direction = ""
        input_signal = ""
        try:
            if not strategy_id or not strategy_version:
                raise ValueError("outer strategy identity is incomplete")
            if receipt_status != "COMPLETE":
                raise ValueError("outer receipt status is not COMPLETE")
            receipt_payload = dict(receipt)
            if receipt_payload.get("schema_version") != "dawnstrike.strategy_decision_receipt.v2":
                raise ValueError("only exact v2 strategy decision receipts are accepted")
            raw_conditions = receipt_payload.get("condition_results")
            if not isinstance(raw_conditions, list) or any(
                not isinstance(condition, Mapping) for condition in raw_conditions
            ):
                raise ValueError("receipt conditions are not typed objects")
            typed_receipt = StrategyDecisionReceipt(
                **{
                    **receipt_payload,
                    "condition_results": tuple(
                        ConditionResult(**dict(condition)) for condition in raw_conditions
                    ),
                }
            )
            canonical_receipt = typed_receipt.to_dict()
            if canonical_json(canonical_receipt) != canonical_json(receipt_payload):
                raise ValueError("receipt is not the exact typed canonical payload")
            if (
                receipt_id != typed_receipt.receipt_id
                or receipt_hash != typed_receipt.receipt_hash_sha256
            ):
                raise ValueError("outer receipt identity conflicts with embedded receipt")
            if (
                strategy_id != typed_receipt.strategy_id
                or strategy_version != typed_receipt.strategy_version
            ):
                raise ValueError("outer strategy identity conflicts with embedded receipt")
            if typed_receipt.symbol != str(expected_ticker or "").strip().upper():
                raise ValueError("receipt symbol conflicts with frozen selection")
            if typed_receipt.market_date != str(expected_market_date or "")[:10]:
                raise ValueError("receipt market date conflicts with frozen selection")
            input_payload = json.loads(typed_receipt.input_payload_json)
            if not isinstance(input_payload, Mapping):
                raise ValueError("receipt input payload is not an object")
            input_signal = _first(
                input_payload,
                "source_signal_id",
                "prior_session_signal_id",
                "signal_id",
                "signal_key",
            )
            input_direction = _receipt_input_direction(input_payload)
            if not input_signal or input_signal != contributor_source_signal:
                raise ValueError("source signal is not bound to canonical receipt input")
            if not input_direction or input_direction != outer_direction:
                raise ValueError("direction is not bound to canonical receipt input")
            effective_receipt_status = "COMPLETE"
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            canonical_receipt = dict(receipt) if receipt else None
        if not strategy_id:
            strategy_id = f"__invalid_contributor_{index}__"
        key = (
            strategy_id,
            strategy_version,
            receipt_id,
            receipt_hash,
            contributor_source_signal,
            outer_direction,
        )
        receipt_key = (receipt_id, receipt_hash)
        binding = (strategy_id, strategy_version, contributor_source_signal, outer_direction)
        if receipt_id or receipt_hash:
            prior = receipt_bindings.get(receipt_key)
            if prior is not None and prior != binding:
                raise SnapshotValidationError(
                    "one strategy receipt is bound to conflicting contributor identities"
                )
            receipt_bindings[receipt_key] = binding
        unique.setdefault(
            key,
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "receipt_id": receipt_id,
                "receipt_hash_sha256": receipt_hash,
                "source_signal_id": contributor_source_signal,
                "direction": outer_direction or input_direction,
                "strategy_decision_receipt": canonical_receipt,
                "_receipt_status": effective_receipt_status,
                "_missing_authenticated_contributor": bool(
                    item.get("_missing_authenticated_contributor")
                ),
            },
        )
    return list(unique.values())


def _contributor_projection(value: Any) -> tuple[tuple[str, ...], ...]:
    decoded = _decode_json(value)
    if not isinstance(decoded, Sequence) or isinstance(decoded, (str, bytes)):
        return ()
    return tuple(
        sorted(
            (
                str(item.get("strategy_id") or item.get("decision_strategy_id") or "").strip(),
                str(item.get("strategy_version") or "").strip(),
                str(item.get("receipt_id") or "").strip(),
                str(
                    item.get("receipt_hash_sha256")
                    or item.get("receipt_hash")
                    or ""
                ).strip().lower(),
                str(item.get("source_signal_id") or item.get("signal_id") or "").strip(),
                str(
                    item.get("receipt_status")
                    or item.get("strategy_receipt_construction_status")
                    or ""
                ).strip().upper(),
                _normalize_direction(item.get("direction") or item.get("trade_direction")),
                canonical_json(
                    dict(
                        _decode_mapping(
                            item.get("strategy_decision_receipt")
                            or item.get("decision_receipt")
                        )
                    )
                ),
            )
            for item in decoded
            if isinstance(item, Mapping)
        )
    )


def _normalize_direction(value: Any) -> str:
    direction = str(value or "").strip().lower()
    return direction if direction in {"long", "short"} else ""


def _receipt_input_direction(value: Mapping[str, Any]) -> str:
    direction = _normalize_direction(value.get("direction") or value.get("trade_direction"))
    if direction:
        return direction
    for key in ("alphaops_market_structure_plan", "market_structure_plan", "plan"):
        plan = _decode_mapping(value.get(key))
        direction = _normalize_direction(plan.get("direction") or plan.get("trade_direction"))
        if direction:
            return direction
    return ""


def _decode_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _decode_mapping(value: Any) -> Mapping[str, Any]:
    decoded = _decode_json(value)
    return decoded if isinstance(decoded, Mapping) else {}


def _exact_outcome(
    selection: Mapping[str, Any],
    by_identity: Mapping[tuple[str, str], list[Mapping[str, Any]]],
) -> Mapping[str, Any] | None:
    matches: list[Mapping[str, Any]] = []
    seen_ids: set[int] = set()
    for key in ("selection_id", "signal_id"):
        identity = str(selection.get(key) or "").strip()
        if not identity:
            payload = _decode_mapping(selection.get("payload_json"))
            identity = str(payload.get(key) or "").strip()
        identity_matches = by_identity.get((key, identity), [])
        for match in identity_matches:
            if id(match) not in seen_ids:
                matches.append(match)
                seen_ids.add(id(match))
    if len(matches) > 1:
        return {
            "_outcome_match_status": "AMBIGUOUS",
            "_outcome_match_count": len(matches),
        }
    if not matches:
        return None
    return {
        **dict(matches[0]),
        "_outcome_match_status": "MATCHED",
        "_outcome_match_count": 1,
    }


def _bridge_row(
    base: Mapping[str, Any],
    contributor: Mapping[str, Any],
    outcome: Mapping[str, Any] | None,
    *,
    cutoff: datetime,
    source_identity: str,
    created_at: str | None,
) -> dict[str, Any]:
    outcome_status, eligible, reason = _outcome_state(outcome, base, cutoff=cutoff)
    if (
        not str(contributor.get("receipt_id") or "").strip()
        or not _sha256(str(contributor.get("receipt_hash_sha256") or "").strip().lower())
        or contributor.get("_receipt_status") != "COMPLETE"
        or contributor.get("_missing_authenticated_contributor") is True
    ):
        outcome_status, eligible, reason = (
            INELIGIBLE,
            False,
            "authenticated contributor receipt is absent or incomplete",
        )
    source_observation_id = _first(outcome, "source_observation_id", "observation_id") or str(
        base.get("selection_source_observation_id") or ""
    )
    source_observation_hash = _first(
        outcome, "source_observation_hash_sha256", "enrichment_observation_sha256"
    ) or str(base.get("selection_source_observation_hash_sha256") or "")
    source_path_id = _first(outcome, "source_path_id", "path_replay_id", "path_id") or str(
        base.get("selection_source_path_id") or ""
    )
    source_path_hash = _first(
        outcome, "source_path_hash_sha256", "replay_receipt_hash_sha256", "path_hash_sha256"
    ) or str(base.get("selection_source_path_hash_sha256") or "")
    source_cutoff = _first(outcome, "source_cutoff", "cutoff", "requested_at") or cutoff.isoformat()
    artifact_id = _first(outcome, "outcome_artifact_id", "outcome_id", "signal_id")
    artifact_hash = _first(outcome, "outcome_artifact_hash_sha256", "source_bar_hash_sha256")
    source_provider = _first(outcome, "source", "source_provider") or base.get("source")
    source_url = _first(outcome, "source_url") or base.get("source_url")
    source_artifact_identity = (
        _first(outcome, "source_artifact_identity")
        or base.get("source_artifact_identity")
    )
    source_lineage = (
        outcome.get("source_lineage")
        if isinstance(outcome, Mapping) and outcome.get("source_lineage") not in (None, "")
        else base.get("source_lineage")
    )
    source_binding = (
        outcome.get("source_binding")
        if isinstance(outcome, Mapping) and outcome.get("source_binding") not in (None, "")
        else base.get("source_binding")
    )
    source_bar_hash = _first(
        outcome, "source_bar_hash_sha256"
    ) or str(base.get("source_bar_hash_sha256") or "")
    observation_payload = (
        outcome.get("source_observation_payload")
        if isinstance(outcome, Mapping)
        else None
    ) or base.get("source_observation_payload")
    path_payload = (
        outcome.get("source_path_payload") if isinstance(outcome, Mapping) else None
    ) or base.get("source_path_payload")
    metrics = outcome.get("selection_outcome_metrics") if isinstance(outcome, Mapping) else None
    if eligible and isinstance(metrics, Mapping):
        try:
            metrics = _directional_metrics(
                metrics,
                direction=str(contributor.get("direction") or ""),
            )
            source_path_id = (
                f"selection-path:{base.get('selection_id')}:{contributor.get('receipt_id')}:"
                f"{source_bar_hash[:16]}"
            )
            path_payload = {
                "path_id": source_path_id,
                "strategy_id": str(contributor.get("strategy_id") or ""),
                "strategy_version": str(contributor.get("strategy_version") or ""),
                "receipt_id": str(contributor.get("receipt_id") or ""),
                "direction": str(contributor.get("direction") or ""),
                "metrics": dict(metrics),
                "source_bar_hash_sha256": source_bar_hash,
            }
            source_path_hash = _digest(path_payload)
            metric_body = {
                "selection_id": str(base.get("selection_id") or ""),
                "signal_id": str(base.get("signal_id") or ""),
                "ticker": str(base.get("ticker") or "").upper(),
                "market_date": str(base.get("market_date") or "")[:10],
                "strategy_id": str(contributor.get("strategy_id") or ""),
                "strategy_version": str(contributor.get("strategy_version") or ""),
                "receipt_id": str(contributor.get("receipt_id") or ""),
                "direction": str(contributor.get("direction") or ""),
                "source_bar_hash_sha256": source_bar_hash,
                "source_binding": dict(source_binding)
                if isinstance(source_binding, Mapping)
                else source_binding,
                "metrics": dict(metrics),
            }
            artifact_hash = _digest(metric_body)
            artifact_id = (
                f"selection-outcome:{base.get('selection_id')}:"
                f"{contributor.get('receipt_id')}:{artifact_hash[:16]}"
            )
        except (TypeError, ValueError, SnapshotValidationError) as exc:
            outcome_status, eligible, reason = (
                INELIGIBLE,
                False,
                f"direction-bound outcome metrics are invalid: {exc}",
            )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        **dict(base),
        "strategy_id": str(contributor.get("strategy_id") or ""),
        "strategy_version": str(contributor.get("strategy_version") or ""),
        "receipt_id": str(contributor.get("receipt_id") or ""),
        "receipt_hash_sha256": str(contributor.get("receipt_hash_sha256") or ""),
        "source_signal_id": str(contributor.get("source_signal_id") or ""),
        "direction": str(contributor.get("direction") or ""),
        "strategy_decision_receipt": contributor.get("strategy_decision_receipt"),
        "receipt_status": str(contributor.get("_receipt_status") or ""),
        "outcome_status": outcome_status,
        "outcome_reason": reason,
        "learning_eligible": eligible,
        "source_observation_id": source_observation_id,
        "source_observation_hash_sha256": source_observation_hash,
        "source_observation_payload": observation_payload,
        "source_path_id": source_path_id,
        "source_path_hash_sha256": source_path_hash,
        "source_path_payload": path_payload,
        "source_cutoff": source_cutoff,
        "source_bar_hash_sha256": source_bar_hash,
        "source_provider": source_provider,
        "source_url": source_url,
        "source_artifact_identity": source_artifact_identity,
        "source_lineage": source_lineage,
        "source_binding": source_binding,
        "source_authenticated": bool(
            outcome.get("source_authenticated")
            if isinstance(outcome, Mapping) and "source_authenticated" in outcome
            else base.get("source_authenticated")
        ),
        "outcome_artifact_id": artifact_id,
        "outcome_artifact_hash_sha256": artifact_hash,
        "source_outcome_status": str(
            (outcome or {}).get("outcome_status")
            or (outcome or {}).get("outcome_state")
            or ""
        ).strip().upper() if isinstance(outcome, Mapping) else "",
        "outcome_match_status": str(
            (outcome or {}).get("_outcome_match_status")
            or ("MATCHED" if outcome is not None else "MISSING")
        ).upper(),
        "outcome_match_count": int(
            (outcome or {}).get("_outcome_match_count")
            or (1 if outcome is not None else 0)
        ),
        "source_identity": source_identity,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    # Carry the producer's complete proof envelope into the immutable join;
    # the SQLite validator must be able to recheck it without consulting the
    # transient capture result.
    if isinstance(outcome, Mapping):
        for field in (
            "source_bar_interval",
            "source_bar_count",
            "source_bar_payload",
            "source_first_bar_at",
            "source_last_bar_at",
            "source_coverage_complete",
            "coverage_status",
            "coverage_detail",
            "coverage_expected_start_at",
            "coverage_expected_end_at",
            "coverage_expected_minute_count",
            "coverage_observed_minute_count",
            "coverage_maximum_gap_seconds",
            "coverage_allowed_gap_seconds",
            "capture_model_version",
            "capture_mode",
            "automatic_sourced_data",
            "source_authenticated",
            "no_lookahead",
            "independent_reconciliation",
            "independent_reconciliation_status",
            "source_conflict",
        ):
            if field in outcome:
                payload[field] = outcome[field]
    # Acquisition timestamps are observational metadata and must not create a
    # new logical bridge on a same-session retry.
    payload["requested_at"] = source_cutoff
    payload["captured_at"] = source_cutoff
    payload["source_fetched_at"] = None
    payload["logical_key"] = _logical_key(
        market_date=str(base.get("market_date") or "")[:10],
        selection_id=str(base.get("selection_id") or ""),
        strategy_id=str(contributor.get("strategy_id") or ""),
        strategy_version=str(contributor.get("strategy_version") or ""),
        receipt_id=str(contributor.get("receipt_id") or ""),
    )
    if isinstance(metrics, Mapping):
        payload["selection_outcome_metrics"] = dict(metrics)
        payload["selection_outcome"] = str(metrics.get("path_status") or "").strip().upper()
    payload["created_at"] = str(created_at or source_cutoff)
    body = {
        key: value
        for key, value in payload.items()
        if key not in {"bridge_id", "bridge_hash_sha256", "created_at"}
    }
    digest = _digest(body)
    payload["bridge_hash_sha256"] = digest
    payload["bridge_id"] = "rep-" + digest[:24]
    if eligible:
        validate_research_episode_outcome_bridge(payload)
    return payload


def _directional_metrics(
    value: Mapping[str, Any], *, direction: str
) -> dict[str, Any]:
    normalized_direction = _normalize_direction(direction)
    if not normalized_direction:
        raise SnapshotValidationError("contributor direction is absent")
    try:
        reference = float(value["reference_price"])
        close = float(value["close_price"])
        high = float(value["high_after_reference"])
        low = float(value["low_after_reference"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotValidationError("raw price path is incomplete") from exc
    if not all(
        number > 0 and number == number and abs(number) != float("inf")
        for number in (reference, close, high, low)
    ):
        raise SnapshotValidationError("raw price path is non-finite or non-positive")
    raw_close = (close - reference) / reference * 100.0
    raw_upside = (high - reference) / reference * 100.0
    raw_downside = (low - reference) / reference * 100.0
    direction_multiplier = 1.0 if normalized_direction == "long" else -1.0
    directional_close = raw_close * direction_multiplier
    if normalized_direction == "long":
        mfe = max(0.0, raw_upside)
        mae = min(0.0, raw_downside)
    else:
        mfe = max(0.0, -raw_downside)
        mae = min(0.0, -raw_upside)
    output = dict(value)
    output.update(
        {
            "direction": normalized_direction,
            "raw_close_change_pct": round(raw_close, 6),
            "raw_upside_excursion_pct": round(raw_upside, 6),
            "raw_downside_excursion_pct": round(raw_downside, 6),
            "raw_path_status": (
                "POSITIVE_CLOSE"
                if raw_close > 0
                else "NEGATIVE_CLOSE"
                if raw_close < 0
                else "FLAT_CLOSE"
            ),
            "direction_adjusted_close_change_pct": round(directional_close, 6),
            "mfe_pct": round(mfe, 6),
            "mae_pct": round(mae, 6),
            "path_status": (
                "POSITIVE_CLOSE"
                if directional_close > 0
                else "NEGATIVE_CLOSE"
                if directional_close < 0
                else "FLAT_CLOSE"
            ),
        }
    )
    return output


def _logical_key(
    *,
    market_date: str,
    selection_id: str,
    strategy_id: str,
    strategy_version: str,
    receipt_id: str,
) -> str:
    identity = {
        "market_date": str(market_date)[:10],
        "selection_id": selection_id,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "receipt_id": receipt_id,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "research-bridge-logical-v1-" + digest


def _outcome_state(
    outcome: Mapping[str, Any] | None,
    base: Mapping[str, Any],
    *,
    cutoff: datetime,
) -> tuple[str, bool, str]:
    if outcome is None:
        return MISSING, False, "exact sourced outcome is absent"
    if str(outcome.get("_outcome_match_status") or "").upper() == "AMBIGUOUS":
        return INELIGIBLE, False, "research radar outcome identity is ambiguous"
    if outcome.get("source_conflict") is True or str(
        outcome.get("independent_reconciliation_status") or ""
    ).upper() == "DISAGREEMENT":
        return INELIGIBLE, False, "independent source reconciliation disagrees"
    for field in ("selection_id", "signal_id", "episode_id", "slate_id"):
        value = outcome.get(field)
        expected = base.get(field)
        if value not in (None, "") and str(value) != str(expected or ""):
            return INELIGIBLE, False, f"outcome {field} conflicts with frozen selection"
    outcome_ticker = str(outcome.get("ticker") or "").strip().upper()
    if outcome_ticker and outcome_ticker != str(base.get("ticker") or "").upper():
        return INELIGIBLE, False, "outcome ticker conflicts with frozen selection"
    outcome_slate_hash = str(outcome.get("slate_content_hash_sha256") or "").strip().lower()
    if outcome_slate_hash and outcome_slate_hash != str(
        base.get("slate_content_hash_sha256") or ""
    ).lower():
        return INELIGIBLE, False, "outcome slate hash conflicts with frozen selection"
    outcome_selected_at = outcome.get("selected_at")
    if outcome_selected_at not in (None, ""):
        try:
            selected_at_matches = _aware_datetime(
                str(outcome_selected_at), "outcome selected_at"
            ) == _aware_datetime(str(base["selected_at"]), "selected_at")
        except SnapshotValidationError:
            selected_at_matches = False
        if not selected_at_matches:
            return INELIGIBLE, False, "outcome selected_at conflicts with frozen selection"
    outcome_day = str(outcome.get("market_date") or outcome.get("date") or "")[:10]
    if outcome_day != str(base["market_date"]):
        return INELIGIBLE, False, "outcome crosses market date"
    source_cutoff = _parse_optional(
        outcome.get("source_cutoff")
        or outcome.get("cutoff")
        or outcome.get("requested_at")
    )
    selected_at = _aware_datetime(str(base["selected_at"]), "selected_at")
    if source_cutoff is None or source_cutoff < selected_at:
        return INELIGIBLE, False, "source cutoff is absent or precedes selection"
    if source_cutoff > cutoff:
        return INELIGIBLE, False, "source cutoff exceeds bridge cutoff"
    # ``automatic_sourced_data`` is provenance metadata, not authentication.
    # Only the producer's validated source binding may authorize learning.
    if outcome.get("source_authenticated") is not True:
        return INELIGIBLE, False, "outcome source is not authenticated"
    metrics = outcome.get("selection_outcome_metrics")
    path_status = metrics.get("path_status") if isinstance(metrics, Mapping) else None
    status = str(
        outcome.get("selection_outcome")
        or path_status
        or outcome.get("outcome_state")
        or outcome.get("outcome_status")
        or ""
    ).strip().upper()
    if status in {"", "MISSING", "UNKNOWN", "OPEN", "PENDING", "UNRESOLVED"}:
        return MISSING, False, "sourced outcome is unresolved"
    if status in {
        "STALE",
        "STALE_OBSERVATION",
        "INCOMPLETE",
        "PARTIAL",
        "TERMINAL_MISSING",
        "INELIGIBLE",
    }:
        return INELIGIBLE, False, "sourced outcome is stale or incomplete"
    if status in {"WIN", "LOSS", "PROFIT", "RETURN"}:
        return INELIGIBLE, False, "trade outcome labels are not valid for radar observations"
    if status not in {"POSITIVE_CLOSE", "NEGATIVE_CLOSE", "FLAT_CLOSE"}:
        return INELIGIBLE, False, "selection path outcome is absent or invalid"
    if (
        outcome.get("source_coverage_complete") is not True
        or outcome.get("coverage_complete") is False
    ):
        return INELIGIBLE, False, "sourced outcome lacks complete bar coverage proof"
    maximum_gap = outcome.get("coverage_maximum_gap_seconds")
    allowed_gap = outcome.get("coverage_allowed_gap_seconds")
    try:
        if maximum_gap is None or allowed_gap is None:
            return INELIGIBLE, False, "sourced outcome lacks bar gap proof"
        maximum_gap = float(maximum_gap)
        allowed_gap = float(allowed_gap)
        if maximum_gap < 0 or allowed_gap < 0 or maximum_gap > allowed_gap:
            return INELIGIBLE, False, "sourced outcome has a disallowed bar gap"
    except (TypeError, ValueError):
        return INELIGIBLE, False, "sourced outcome coverage metadata is invalid"
    observation_hash = _first(
        outcome, "source_observation_hash_sha256", "enrichment_observation_sha256"
    )
    artifact_hash = _first(outcome, "outcome_artifact_hash_sha256", "source_bar_hash_sha256")
    path_hash = _first(
        outcome,
        "source_path_hash_sha256",
        "replay_receipt_hash_sha256",
        "path_hash_sha256",
    )
    if (
        not _sha256(observation_hash)
        or not _sha256(path_hash)
        or not _sha256(artifact_hash)
    ):
        return INELIGIBLE, False, "source observation/artifact hash is absent"
    last_bar = _parse_optional(outcome.get("source_last_bar_at") or outcome.get("last_bar_at"))
    if last_bar is not None and last_bar > cutoff:
        return INELIGIBLE, False, "source bars include data after cutoff"
    return status, bool(outcome.get("learning_eligible", True)), ""


def validate_research_episode_outcome_bridge(row: Mapping[str, Any]) -> None:
    """Validate a newly built bridge; callers may never mint a revision."""

    _validate_research_episode_outcome_bridge(row, allow_persisted_revision=False)


def _validate_persisted_research_episode_outcome_bridge(
    row: Mapping[str, Any],
) -> None:
    """Validate a bridge loaded or minted inside the governed SQLite boundary."""

    _validate_research_episode_outcome_bridge(row, allow_persisted_revision=True)


def _validate_research_episode_outcome_bridge(
    row: Mapping[str, Any], *, allow_persisted_revision: bool
) -> None:
    """Validate the carried automatic-capture proof before learning/persisting."""

    _validate_bridge_envelope(
        row,
        allow_persisted_revision=allow_persisted_revision,
    )
    status = str(row.get("outcome_status") or "").strip().upper()
    if row.get("learning_eligible") is not True and status != "COMPLETE_SOURCED":
        return
    if row.get("learning_eligible") is True and str(
        row.get("source_outcome_status") or ""
    ).upper() != "COMPLETE_SOURCED":
        raise SnapshotValidationError("research outcome producer status is not complete sourced")
    binding = row.get("source_binding")
    if not isinstance(binding, Mapping):
        raise SnapshotValidationError("research outcome source binding is absent")
    provider = str(binding.get("provider") or "").strip()
    source_url = str(binding.get("source_url") or "").strip()
    artifact = str(binding.get("source_artifact_identity") or "").strip()
    bar_hash = str(binding.get("source_bar_hash_sha256") or "").strip().lower()
    lineage = binding.get("source_lineage")
    if not provider or not source_url or not artifact or not _sha256(bar_hash):
        raise SnapshotValidationError("research outcome source binding is incomplete")
    if bar_hash not in artifact.lower():
        raise SnapshotValidationError("research outcome source binding is not authenticated")
    canonical_lineage = _canonical_source_lineage(lineage)
    if not canonical_lineage or provider not in {
        str(item.get("source") or "").strip() for item in canonical_lineage
    }:
        raise SnapshotValidationError("research outcome source lineage is invalid")
    matching = [
        item for item in canonical_lineage
        if str(item.get("source") or "").strip() == provider
        and str(item.get("source_url") or "").strip() == source_url
    ]
    if not matching:
        raise SnapshotValidationError("research outcome provider/request binding is invalid")
    ticker = str(row.get("ticker") or "").upper()
    market_day = str(row.get("market_date") or "")[:10]
    session_open, session_close = _session_bounds(market_day)
    for item in canonical_lineage:
        if str(item.get("ticker") or "").strip().upper() != ticker:
            raise SnapshotValidationError("research outcome lineage ticker binding mismatch")
        item_provider = str(item.get("source") or "").strip()
        item_url = str(item.get("source_url") or "").strip()
        item_bar_hash = str(item.get("source_bar_hash_sha256") or "").lower()
        if not _sha256(item_bar_hash):
            raise SnapshotValidationError("research outcome lineage bar hash is invalid")
        expected_item_artifact = (
            f"market-bars:{item_provider}:{ticker}:{market_day}:1m:{item_bar_hash}"
        )
        if str(item.get("source_artifact_identity") or "") != expected_item_artifact:
            raise SnapshotValidationError("research outcome lineage artifact binding mismatch")
        expected_item_request = _expected_provider_request_contract(
            provider=item_provider,
            ticker=ticker,
            source_url=item_url,
            session_open=session_open,
            session_close=session_close,
        )
        if item.get("request_contract") != expected_item_request:
            raise SnapshotValidationError("research outcome lineage request contract mismatch")
        if item_provider == provider and item_url == source_url and (
            item_bar_hash != bar_hash
            or str(item.get("source_artifact_identity") or "") != artifact
        ):
            raise SnapshotValidationError("research outcome selected lineage binding mismatch")
    expected_request_hash = _digest_list(canonical_lineage)
    if str(binding.get("source_request_hash_sha256") or "").lower() != expected_request_hash:
        raise SnapshotValidationError("research outcome request hash mismatch")
    if row.get("source_bar_hash_sha256") != bar_hash:
        raise SnapshotValidationError("research outcome bar hash binding mismatch")
    if str(row.get("source_provider") or "").strip() != provider:
        raise SnapshotValidationError("research outcome provider binding mismatch")
    if str(row.get("source_url") or "").strip() != source_url:
        raise SnapshotValidationError("research outcome URL binding mismatch")
    if str(row.get("source_artifact_identity") or "").strip() != artifact:
        raise SnapshotValidationError("research outcome artifact binding mismatch")
    row_cutoff = str(row.get("source_cutoff") or "").strip()
    binding_cutoff = str(binding.get("source_cutoff") or "").strip()
    if not row_cutoff or row_cutoff != binding_cutoff:
        raise SnapshotValidationError("research outcome cutoff binding mismatch")
    cutoff_dt = _parse_optional(row_cutoff)
    if cutoff_dt is None:
        raise SnapshotValidationError("research outcome cutoff is invalid")
    if row.get("source_coverage_complete") is not True:
        raise SnapshotValidationError("research outcome coverage proof is absent")
    if row.get("source_authenticated") is not True:
        raise SnapshotValidationError("research outcome source authentication is absent")
    if row.get("automatic_sourced_data") is not True:
        raise SnapshotValidationError("research outcome automatic producer proof is absent")
    if row.get("research_only") is not True or row.get("broker_execution_enabled") is not False:
        raise SnapshotValidationError("research outcome execution scope is invalid")
    if row.get("no_lookahead") is not True:
        raise SnapshotValidationError("research outcome no-lookahead proof is absent")
    if str(row.get("capture_mode") or "") != "automatic_sourced_selection_observation":
        raise SnapshotValidationError("research outcome capture producer is invalid")
    if not str(row.get("capture_model_version") or "").strip():
        raise SnapshotValidationError("research outcome capture model proof is absent")
    expected_request_contract = _expected_provider_request_contract(
        provider=provider,
        ticker=ticker,
        source_url=source_url,
        session_open=session_open,
        session_close=session_close,
    )
    if binding.get("request_contract") != expected_request_contract:
        raise SnapshotValidationError("research outcome selected request contract mismatch")
    if any(item.get("request_contract") != expected_request_contract for item in matching):
        raise SnapshotValidationError("research outcome lineage request contract mismatch")
    expected_full_count = int((session_close - session_open).total_seconds() // 60)
    expected_full_start = _utc_iso(session_open)
    expected_full_end = _utc_iso(session_close - timedelta(minutes=1))
    for item in canonical_lineage:
        if (
            str(item.get("status") or "").lower() != "ok"
            or item.get("source_coverage_complete") is not True
            or str(item.get("coverage_status") or "").lower() != "complete"
            or str(item.get("coverage_expected_start_at") or "") != expected_full_start
            or str(item.get("coverage_expected_end_at") or "") != expected_full_end
            or item.get("coverage_expected_minute_count") != expected_full_count
            or item.get("coverage_observed_minute_count") != expected_full_count
            or item.get("bar_count") != expected_full_count
            or item.get("first_bar_at") != expected_full_start
            or item.get("last_bar_at") != expected_full_end
            or item.get("coverage_maximum_gap_seconds") != 60
            or item.get("coverage_allowed_gap_seconds") != 60
        ):
            raise SnapshotValidationError("research outcome lineage session coverage mismatch")
    reconciliation = binding.get("independent_reconciliation")
    if not isinstance(reconciliation, Mapping):
        raise SnapshotValidationError("research outcome reconciliation proof is absent")
    if str(binding.get("independent_reconciliation_status") or "") != str(
        reconciliation.get("status") or ""
    ):
        raise SnapshotValidationError("research outcome reconciliation status mismatch")
    reconciliation_status = str(reconciliation.get("status") or "")
    if reconciliation_status == "DISAGREEMENT" or row.get(
        "source_conflict"
    ) is True:
        raise SnapshotValidationError("research outcome independent sources disagree")
    if row.get("independent_reconciliation") != dict(reconciliation) or str(
        row.get("independent_reconciliation_status") or ""
    ) != str(reconciliation.get("status") or ""):
        raise SnapshotValidationError("research outcome reconciliation binding mismatch")
    if str(binding.get("reconciliation_hash_sha256") or "").lower() != _digest(
        dict(reconciliation)
    ):
        raise SnapshotValidationError("research outcome reconciliation hash mismatch")
    lineage_sources = sorted(
        {str(item.get("source") or "") for item in canonical_lineage}
    )
    lineage_hashes = sorted(
        {str(item.get("source_bar_hash_sha256") or "").lower() for item in canonical_lineage}
    )
    if reconciliation.get("independent_source_count") != len(lineage_sources):
        raise SnapshotValidationError("research outcome reconciliation source count mismatch")
    if len(lineage_sources) < 2:
        if reconciliation_status != "NOT_AVAILABLE" or reconciliation.get(
            "agreement"
        ) is not None:
            raise SnapshotValidationError("research outcome reconciliation availability mismatch")
    elif (
        reconciliation_status != "PASSED"
        or reconciliation.get("agreement") is not True
        or reconciliation.get("source_names") != lineage_sources
        or reconciliation.get("source_bar_hashes") != lineage_hashes
        or reconciliation.get("thresholds")
        != {"minimum_overlap_pct": 98.0, "maximum_close_difference_pct": 0.5}
    ):
        raise SnapshotValidationError("research outcome reconciliation lineage mismatch")
    if str(row.get("capture_model_version") or "") != "alphaops-sourced-outcome-v3":
        raise SnapshotValidationError("research outcome capture model is not governed")
    expected_artifact = (
        f"market-bars:{provider}:{ticker}:{str(row.get('market_date') or '')[:10]}:"
        f"1m:{bar_hash}"
    )
    if artifact != expected_artifact:
        raise SnapshotValidationError("research outcome artifact identity is not producer-bound")
    if str(row.get("source_bar_interval") or "") != "1m":
        raise SnapshotValidationError("research outcome bar interval is not governed")
    canonical_bars = _validate_source_bar_payload(row, bar_hash=bar_hash)
    try:
        maximum_gap = float(row.get("coverage_maximum_gap_seconds"))
        allowed_gap = float(row.get("coverage_allowed_gap_seconds"))
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError("research outcome gap proof is invalid") from exc
    if maximum_gap < 0 or allowed_gap < 0 or maximum_gap > allowed_gap:
        raise SnapshotValidationError("research outcome gap proof is invalid")
    metrics = row.get("selection_outcome_metrics")
    if not isinstance(metrics, Mapping):
        raise SnapshotValidationError("research outcome metrics are absent")
    path_status = str(metrics.get("path_status") or "").strip().upper()
    if path_status not in {"POSITIVE_CLOSE", "NEGATIVE_CLOSE", "FLAT_CLOSE"}:
        raise SnapshotValidationError("research outcome path status is invalid")
    _validate_directional_metrics(row, metrics, canonical_bars)
    observation_payload = _decode_mapping(row.get("source_observation_payload"))
    if not observation_payload:
        raise SnapshotValidationError("research outcome observation payload is absent")
    expected_observation_hash = _digest(observation_payload)
    if str(row.get("source_observation_hash_sha256") or "").lower() != expected_observation_hash:
        raise SnapshotValidationError("research outcome observation hash mismatch")
    if str(observation_payload.get("ticker") or "").upper() != str(
        row.get("ticker") or ""
    ).upper():
        raise SnapshotValidationError("research outcome observation ticker binding mismatch")
    if str(observation_payload.get("market_date") or "")[:10] != str(
        row.get("market_date") or ""
    )[:10]:
        raise SnapshotValidationError("research outcome observation date binding mismatch")
    if str(observation_payload.get("observed_at") or "") != str(metrics.get("reference_at") or ""):
        raise SnapshotValidationError("research outcome reference timestamp binding mismatch")
    observation_at = _parse_optional(observation_payload.get("observed_at"))
    if observation_at is None or observation_at > cutoff_dt:
        raise SnapshotValidationError("research outcome reference exceeds cutoff")
    close_at = _parse_optional(metrics.get("close_at"))
    if close_at is not None and close_at > cutoff_dt:
        raise SnapshotValidationError("research outcome path exceeds cutoff")
    try:
        if float(observation_payload.get("close")) != float(metrics.get("reference_price")):
            raise SnapshotValidationError("research outcome reference price binding mismatch")
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError("research outcome reference price is invalid") from exc
    path_payload = _decode_mapping(row.get("source_path_payload"))
    if not path_payload:
        raise SnapshotValidationError("research outcome path payload is absent")
    expected_path_hash = _digest(path_payload)
    if str(row.get("source_path_hash_sha256") or "").lower() != expected_path_hash:
        raise SnapshotValidationError("research outcome path hash mismatch")
    if str(path_payload.get("path_id") or "") != str(row.get("source_path_id") or ""):
        raise SnapshotValidationError("research outcome path identity mismatch")
    if path_payload.get("metrics") != dict(metrics):
        raise SnapshotValidationError("research outcome path metrics binding mismatch")
    for field in ("strategy_id", "strategy_version", "receipt_id", "direction"):
        if str(path_payload.get(field) or "") != str(row.get(field) or ""):
            raise SnapshotValidationError(
                f"research outcome path contributor {field} binding mismatch"
            )
    if str(path_payload.get("source_bar_hash_sha256") or "").lower() != bar_hash:
        raise SnapshotValidationError("research outcome path bar binding mismatch")
    expected_observation_id = (
        f"selection-observation:{row.get('selection_id')}:{observation_payload.get('observed_at')}"
    )
    if str(row.get("source_observation_id") or "") != expected_observation_id:
        raise SnapshotValidationError("research outcome observation identity mismatch")
    expected_path_id = (
        f"selection-path:{row.get('selection_id')}:{row.get('receipt_id')}:"
        f"{bar_hash[:16]}"
    )
    if str(row.get("source_path_id") or "") != expected_path_id:
        raise SnapshotValidationError("research outcome path identity is not producer-bound")
    metric_body = {
        "selection_id": str(row.get("selection_id") or ""),
        "signal_id": str(row.get("signal_id") or ""),
        "ticker": str(row.get("ticker") or "").upper(),
        "market_date": str(row.get("market_date") or "")[:10],
        "strategy_id": str(row.get("strategy_id") or ""),
        "strategy_version": str(row.get("strategy_version") or ""),
        "receipt_id": str(row.get("receipt_id") or ""),
        "direction": str(row.get("direction") or ""),
        "source_bar_hash_sha256": bar_hash,
        "source_binding": dict(binding),
        "metrics": dict(metrics),
    }
    expected_metric_hash = _digest(metric_body)
    if str(row.get("outcome_artifact_hash_sha256") or "").lower() != expected_metric_hash:
        raise SnapshotValidationError("research outcome artifact hash mismatch")
    if str(row.get("outcome_artifact_id") or "") != (
        f"selection-outcome:{row.get('selection_id')}:{row.get('receipt_id')}:"
        f"{expected_metric_hash[:16]}"
    ):
        raise SnapshotValidationError("research outcome metric artifact identity mismatch")
    if not _sha256(str(row.get("source_observation_hash_sha256") or "")):
        raise SnapshotValidationError("research outcome observation hash is absent")
    if not _sha256(str(row.get("source_path_hash_sha256") or "")):
        raise SnapshotValidationError("research outcome path hash is absent")


def _validate_bridge_envelope(
    row: Mapping[str, Any], *, allow_persisted_revision: bool
) -> None:
    if row.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotValidationError("research outcome bridge schema is invalid")
    if row.get("research_only") is not True or row.get("broker_execution_enabled") is not False:
        raise SnapshotValidationError("research outcome bridge execution scope is invalid")
    if str(row.get("broker_execution") or "disabled").strip().lower() != "disabled":
        raise SnapshotValidationError("research outcome bridge broker execution is not disabled")
    body = {
        key: value
        for key, value in row.items()
        if key not in {"bridge_id", "bridge_hash_sha256", "created_at"}
    }
    digest = _digest(body)
    if str(row.get("bridge_hash_sha256") or "").lower() != digest:
        raise SnapshotValidationError("research outcome bridge hash mismatch")
    if str(row.get("bridge_id") or "") != "rep-" + digest[:24]:
        raise SnapshotValidationError("research outcome bridge id mismatch")
    expected_logical = _logical_key(
        market_date=str(row.get("market_date") or "")[:10],
        selection_id=str(row.get("selection_id") or ""),
        strategy_id=str(row.get("strategy_id") or ""),
        strategy_version=str(row.get("strategy_version") or ""),
        receipt_id=str(row.get("receipt_id") or ""),
    )
    actual_logical = str(row.get("logical_key") or "")
    if actual_logical == expected_logical + "-r2" and not allow_persisted_revision:
        raise SnapshotValidationError(
            "research outcome bridge revision must be minted by the persistence boundary"
        )
    if actual_logical not in {expected_logical, expected_logical + "-r2"}:
        raise SnapshotValidationError("research outcome bridge logical identity mismatch")
    if row.get("learning_eligible") is not True:
        return
    if str(row.get("receipt_status") or "").strip().upper() != "COMPLETE":
        raise SnapshotValidationError("research outcome contributor receipt is not complete")
    receipt = _decode_mapping(row.get("strategy_decision_receipt"))
    if receipt.get("schema_version") != "dawnstrike.strategy_decision_receipt.v2":
        raise SnapshotValidationError("research outcome contributor receipt is not exact v2")
    try:
        conditions = receipt.get("condition_results")
        if not isinstance(conditions, list) or any(
            not isinstance(item, Mapping) for item in conditions
        ):
            raise ValueError("condition results are not objects")
        typed = StrategyDecisionReceipt(
            **{
                **dict(receipt),
                "condition_results": tuple(
                    ConditionResult(**dict(item)) for item in conditions
                ),
            }
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise SnapshotValidationError("research outcome contributor receipt is invalid") from exc
    if canonical_json(typed.to_dict()) != canonical_json(dict(receipt)):
        raise SnapshotValidationError("research outcome contributor receipt is not canonical")
    for outer, embedded in (
        (str(row.get("receipt_id") or ""), typed.receipt_id),
        (str(row.get("receipt_hash_sha256") or "").lower(), typed.receipt_hash_sha256),
        (str(row.get("strategy_id") or ""), typed.strategy_id),
        (str(row.get("strategy_version") or ""), typed.strategy_version),
        (str(row.get("ticker") or "").upper(), typed.symbol),
        (str(row.get("market_date") or "")[:10], typed.market_date),
    ):
        if outer != embedded:
            raise SnapshotValidationError("research outcome contributor outer binding mismatch")
    try:
        input_payload = json.loads(typed.input_payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError("research outcome contributor input is invalid") from exc
    if not isinstance(input_payload, Mapping):
        raise SnapshotValidationError("research outcome contributor input is not an object")
    if _first(
        input_payload,
        "source_signal_id",
        "prior_session_signal_id",
        "signal_id",
        "signal_key",
    ) != str(row.get("source_signal_id") or ""):
        raise SnapshotValidationError("research outcome contributor signal binding mismatch")
    if _receipt_input_direction(input_payload) != str(row.get("direction") or ""):
        raise SnapshotValidationError("research outcome contributor direction binding mismatch")


def _session_bounds(market_date: str) -> tuple[datetime, datetime]:
    try:
        market_day = date.fromisoformat(market_date)
        decision = market_session(market_day)
        if not decision.is_trading_day or not decision.open_time_et or not decision.close_time_et:
            raise ValueError("not a trading session")
        opened = datetime.combine(
            market_day, time.fromisoformat(decision.open_time_et), tzinfo=EASTERN
        )
        closed = datetime.combine(
            market_day, time.fromisoformat(decision.close_time_et), tzinfo=EASTERN
        )
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError("research outcome market session is invalid") from exc
    return opened, closed


def _expected_provider_request_contract(
    *,
    provider: str,
    ticker: str,
    source_url: str,
    session_open: datetime,
    session_close: datetime,
) -> dict[str, Any]:
    parsed_url = urlparse(source_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise SnapshotValidationError("research outcome source URL is not governed")
    if provider.lower() in {
        "yahoo",
        "yahoo_finance",
        "yahoo finance",
        "yahoo_finance_chart",
    }:
        provider_symbol = yahoo_provider_symbol(ticker)
        expected_url = yahoo_chart_url(
            ticker,
            range_name="5d",
            interval="1m",
            include_pre_post=False,
        )
        if source_url != expected_url or (
            parsed_url.hostname != "query1.finance.yahoo.com"
            or parse_qs(parsed_url.query, keep_blank_values=True)
            != {"range": ["5d"], "interval": ["1m"], "includePrePost": ["false"]}
        ):
            raise SnapshotValidationError("research outcome Yahoo source URL is not governed")
        return {
            "provider": provider,
            "ticker": ticker,
            "provider_symbol": provider_symbol,
            "endpoint": source_url,
            "range": "5d",
            "interval": "1m",
            "include_pre_post": False,
        }
    if provider.lower().startswith("alpaca_market_data_"):
        if (
            parsed_url.hostname != "data.alpaca.markets"
            or parsed_url.path != "/v2/stocks/bars"
            or parsed_url.query
        ):
            raise SnapshotValidationError("research outcome Alpaca source URL is not governed")
        feed = provider[len("alpaca_market_data_") :]
        if not feed:
            raise SnapshotValidationError("research outcome Alpaca feed binding is absent")
        return {
            "provider": provider,
            "ticker": ticker,
            "symbols": [ticker],
            "endpoint": source_url,
            "start": _utc_iso(session_open),
            "end": _utc_iso(session_close),
            "timeframe": "1Min",
            "feed": feed,
        }
    raise SnapshotValidationError("research outcome provider is not allowlisted")


def _validate_source_bar_payload(
    row: Mapping[str, Any], *, bar_hash: str
) -> list[dict[str, Any]]:
    raw = _decode_json(row.get("source_bar_payload"))
    if not isinstance(raw, list) or not raw or len(raw) > 1_000:
        raise SnapshotValidationError(
            "research outcome canonical bar payload is absent or unbounded"
        )
    ticker = str(row.get("ticker") or "").upper()
    opened, closed = _session_bounds(str(row.get("market_date") or "")[:10])
    expected_last = closed - timedelta(minutes=1)
    canonical: list[dict[str, Any]] = []
    timestamps: list[datetime] = []
    expected_keys = {"observed_at", "open", "high", "low", "close", "volume"}
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != expected_keys:
            raise SnapshotValidationError("research outcome canonical bar schema is invalid")
        observed_at = _parse_optional(item.get("observed_at"))
        if observed_at is None:
            raise SnapshotValidationError("research outcome canonical bar timestamp is invalid")
        try:
            values = [float(item[key]) for key in ("open", "high", "low", "close")]
            volume = None if item.get("volume") is None else float(item["volume"])
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(
                "research outcome canonical bar value is invalid"
            ) from exc
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise SnapshotValidationError("research outcome canonical bar OHLC is invalid")
        open_price, high, low, close_price = values
        if not low <= min(open_price, close_price) <= max(open_price, close_price) <= high:
            raise SnapshotValidationError("research outcome canonical bar OHLC ordering is invalid")
        if volume is not None and (not math.isfinite(volume) or volume < 0):
            raise SnapshotValidationError("research outcome canonical bar volume is invalid")
        canonical.append(dict(item))
        timestamps.append(observed_at)
    if timestamps != sorted(set(timestamps)):
        raise SnapshotValidationError("research outcome canonical bars are not ordered and unique")
    if timestamps[0] != opened.astimezone(UTC) or timestamps[-1] != expected_last.astimezone(UTC):
        raise SnapshotValidationError(
            "research outcome canonical bars do not span the exact session"
        )
    expected_count = int((closed - opened).total_seconds() // 60)
    if len(timestamps) != expected_count or any(
        right - left != timedelta(minutes=1)
        for left, right in zip(timestamps, timestamps[1:], strict=False)
    ):
        raise SnapshotValidationError("research outcome canonical bar coverage is incomplete")
    computed_hash = hashlib.sha256(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if computed_hash != bar_hash:
        raise SnapshotValidationError("research outcome canonical bar payload hash mismatch")
    if row.get("source_bar_count") != len(canonical):
        raise SnapshotValidationError("research outcome source bar count mismatch")
    if str(row.get("source_first_bar_at") or "") != str(canonical[0]["observed_at"]):
        raise SnapshotValidationError("research outcome first bar binding mismatch")
    if str(row.get("source_last_bar_at") or "") != str(canonical[-1]["observed_at"]):
        raise SnapshotValidationError("research outcome last bar binding mismatch")
    selected_at = _parse_optional(row.get("selected_at"))
    if selected_at is None:
        raise SnapshotValidationError("research outcome frozen selection timestamp is invalid")
    first_eligible = max(opened, selected_at.astimezone(EASTERN))
    if first_eligible.second or first_eligible.microsecond:
        first_eligible = first_eligible.replace(second=0, microsecond=0) + timedelta(minutes=1)
    else:
        first_eligible = first_eligible.replace(second=0, microsecond=0)
    eligible = [timestamp for timestamp in timestamps if first_eligible <= timestamp < closed]
    expected_eligible = int((closed - first_eligible).total_seconds() // 60)
    if (
        str(row.get("coverage_expected_start_at") or "")
        != _utc_iso(first_eligible)
        or str(row.get("coverage_expected_end_at") or "")
        != _utc_iso(expected_last)
        or row.get("coverage_expected_minute_count") != expected_eligible
        or row.get("coverage_observed_minute_count") != len(eligible)
        or len(eligible) != expected_eligible
        or row.get("source_coverage_complete") is not True
    ):
        raise SnapshotValidationError("research outcome selected-session coverage binding mismatch")
    del ticker
    return canonical


def _validate_directional_metrics(
    row: Mapping[str, Any],
    metrics: Mapping[str, Any],
    canonical_bars: Sequence[Mapping[str, Any]],
) -> None:
    selected_at = _parse_optional(row.get("selected_at"))
    opened, closed = _session_bounds(str(row.get("market_date") or "")[:10])
    if selected_at is None:
        raise SnapshotValidationError("research outcome selection timestamp is invalid")
    first_eligible = max(opened, selected_at.astimezone(EASTERN))
    if first_eligible.second or first_eligible.microsecond:
        first_eligible = first_eligible.replace(second=0, microsecond=0) + timedelta(minutes=1)
    else:
        first_eligible = first_eligible.replace(second=0, microsecond=0)
    eligible = [
        item
        for item in canonical_bars
        if first_eligible
        <= _aware_datetime(str(item["observed_at"]), "bar observed_at").astimezone(EASTERN)
        < closed
    ]
    if len(eligible) < 2:
        raise SnapshotValidationError("research outcome direction path is incomplete")
    reference = float(eligible[0]["close"])
    subsequent = eligible[1:]
    raw = {
        "reference_at": str(eligible[0]["observed_at"]),
        "reference_price": reference,
        "close_at": str(subsequent[-1]["observed_at"]),
        "close_price": float(subsequent[-1]["close"]),
        "high_after_reference": max(float(item["high"]) for item in subsequent),
        "low_after_reference": min(float(item["low"]) for item in subsequent),
        "bar_count": len(subsequent),
    }
    for key, expected in raw.items():
        if metrics.get(key) != expected:
            raise SnapshotValidationError(f"research outcome metric {key} is not bar-derived")
    expected_metrics = _directional_metrics(
        raw,
        direction=str(row.get("direction") or ""),
    )
    for key in (
        "direction",
        "raw_close_change_pct",
        "raw_upside_excursion_pct",
        "raw_downside_excursion_pct",
        "raw_path_status",
        "direction_adjusted_close_change_pct",
        "mfe_pct",
        "mae_pct",
        "path_status",
    ):
        if metrics.get(key) != expected_metrics.get(key):
            raise SnapshotValidationError(f"research outcome directional metric {key} mismatch")


def _canonical_source_lineage(value: Any) -> list[dict[str, Any]]:
    decoded = _decode_json(value)
    if not isinstance(decoded, Sequence) or isinstance(decoded, (str, bytes)):
        return []
    candidates = [
        item
        for item in decoded
        if isinstance(item, Mapping)
        and (
            str(item.get("status") or "").lower() == "ok"
            or item.get("source_coverage_complete") is True
        )
    ]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates or decoded:
        if not isinstance(item, Mapping):
            return []
        row = dict(item)
        for key in ("fetched_at", "attempt", "attempt_limit", "error"):
            row.pop(key, None)
        identity = json.dumps(row, sort_keys=True, separators=(",", ":"))
        if identity not in seen:
            rows.append(row)
            seen.add(identity)
    return sorted(
        rows,
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )


def _digest_list(value: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _first(value: Mapping[str, Any] | None, *keys: str) -> str:
    if not isinstance(value, Mapping):
        return ""
    for key in keys:
        item = value.get(key)
        if item not in (None, ""):
            return str(item).strip()
    return ""


def _aware_datetime(value: str, field: str) -> datetime:
    parsed = _parse_optional(value)
    if parsed is None:
        raise SnapshotValidationError(f"{field} must be an aware ISO timestamp")
    return parsed


def _parse_optional(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sha256(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


__all__ = [
    "INELIGIBLE",
    "MISSING",
    "RADAR_COHORT",
    "SCHEMA_VERSION",
    "build_and_persist_research_episode_outcome_bridges",
    "build_research_episode_outcome_bridges",
    "build_research_episode_outcome_join",
    "load_research_episode_outcome_bridges",
    "persist_research_episode_outcome_bridges",
    "persist_research_episode_outcome_join",
    "run_research_episode_outcome_bridge",
]
