"""Selection-only outcome joins for the Morning research radar.

This sidecar deliberately does not model fills, entries, positions, or P&L.  It
binds an immutable frozen selection to sourced post-selection observations so
strategy contributors can be evaluated without changing their decision receipt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from intraday_scanner.decisioning.contracts import canonical_json
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.services.luna_research_slate_service import (
    validate_ranked_research_slate,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

SCHEMA_VERSION = "dawnstrike.research_episode_outcome_bridge.v1"
RADAR_COHORT = "research_radar"
MISSING = "MISSING"
INELIGIBLE = "INELIGIBLE"


def build_research_episode_outcome_bridges(
    selections: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]] | None = None,
    *,
    market_date: str,
    cutoff: str,
    source_identity: str = "",
    created_at: str | None = None,
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
        base = _selection_identity(selection, day=day, cutoff=cutoff_dt)
        source_outcome = _exact_outcome(selection, outcome_by_identity)
        contributors = _contributors(selection)
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
) -> dict[str, int]:
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

    rows = build_research_episode_outcome_bridges(
        selections,
        outcomes,
        market_date=market_date,
        cutoff=cutoff,
        source_identity=source_identity,
        created_at=created_at,
    )
    stats = persist_research_episode_outcome_bridges(store, rows)
    return {
        **stats,
        "schema_version": SCHEMA_VERSION,
        "market_date": str(market_date)[:10],
        "bridges": rows,
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
    selection: Mapping[str, Any], *, day: str, cutoff: datetime
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
        validate_ranked_research_slate(slate, market_date=day, production=True)
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


def _contributors(selection: Mapping[str, Any]) -> list[dict[str, Any]]:
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
            rows = [dict(item) for item in decoded_raw if isinstance(item, Mapping)]
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
    unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for item in rows:
        strategy_id = str(item.get("strategy_id") or item.get("decision_strategy_id") or "").strip()
        strategy_version = str(item.get("strategy_version") or "").strip()
        receipt = _decode_mapping(
            item.get("strategy_decision_receipt") or item.get("decision_receipt")
        )
        receipt_id = str(item.get("receipt_id") or receipt.get("receipt_id") or "").strip()
        receipt_hash = str(
            item.get("receipt_hash_sha256")
            or receipt.get("receipt_hash_sha256")
            or receipt.get("hash_sha256")
            or ""
        ).strip().lower()
        receipt_status = str(
            item.get("receipt_status")
            or item.get("strategy_receipt_construction_status")
            or receipt.get("receipt_status")
            or ""
        ).strip().upper()
        effective_receipt_status = receipt_status
        if receipt:
            contributor_source_signal = str(
                item.get("source_signal_id")
                or item.get("prior_session_signal_id")
                or item.get("signal_id")
                or signal.get("signal_id")
                or ""
            ).strip()
            receipt_payload = dict(receipt)
            supplied_hash = str(
                receipt_payload.get("receipt_hash_sha256") or ""
            ).lower()
            supplied_id = str(receipt_payload.get("receipt_id") or "")
            hash_body = {
                key: value
                for key, value in receipt_payload.items()
                if key not in {"receipt_hash_sha256", "receipt_id"}
            }
            expected_hash = hashlib.sha256(
                canonical_json(hash_body).encode("utf-8")
            ).hexdigest()
            if (
                supplied_hash != expected_hash
                or supplied_id != "sdr-" + expected_hash[:24]
                or str(receipt_payload.get("strategy_id") or "") != strategy_id
                or str(receipt_payload.get("strategy_version") or "") != strategy_version
                or str(receipt_payload.get("symbol") or "").upper()
                != str(selection.get("ticker") or "").upper()
                or str(receipt_payload.get("market_date") or "")[:10]
                != str(selection.get("market_date") or "")[:10]
                or (
                    not contributor_source_signal
                    or not str(receipt_payload.get("input_payload_json") or "").strip()
                )
                or receipt_status != "COMPLETE"
            ):
                item["_receipt_status"] = "INVALID"
            else:
                try:
                    input_payload_text = str(receipt_payload["input_payload_json"])
                    input_payload = json.loads(input_payload_text)
                    input_signal = next(
                        (
                            str(input_payload.get(key) or "").strip()
                            for key in (
                                "source_signal_id",
                                "prior_session_signal_id",
                                "signal_id",
                                "signal_key",
                            )
                            if str(input_payload.get(key) or "").strip()
                        ),
                        "",
                    )
                    if (
                        canonical_json(input_payload) != input_payload_text
                        or input_signal != contributor_source_signal
                    ):
                        item["_receipt_status"] = "INVALID"
                except (TypeError, ValueError, json.JSONDecodeError):
                    item["_receipt_status"] = "INVALID"
        elif receipt_status == "COMPLETE" and (receipt_id or receipt_hash):
            item["_receipt_status"] = "INVALID"
        effective_receipt_status = str(
            item.get("_receipt_status") or receipt_status
        ).strip().upper()
        if not strategy_id:
            continue
        key = (strategy_id, strategy_version, receipt_id, receipt_hash)
        unique.setdefault(
            key,
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "receipt_id": receipt_id,
                "receipt_hash_sha256": receipt_hash,
                "source_signal_id": str(
                    item.get("source_signal_id")
                    or item.get("prior_session_signal_id")
                    or item.get("signal_id")
                    or signal.get("signal_id")
                    or ""
                ).strip(),
                "_receipt_status": effective_receipt_status,
                "_missing_authenticated_contributor": bool(
                    item.get("_missing_authenticated_contributor")
                ),
            },
        )
    return list(unique.values())


def _contributor_projection(value: Any) -> tuple[tuple[str, str, str, str, str, str], ...]:
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
            )
            for item in decoded
            if isinstance(item, Mapping)
        )
    )


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
        raise SnapshotValidationError("research radar outcome identity is ambiguous")
    return matches[0] if matches else None


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
        or (
            contributor.get("_receipt_status")
            and contributor.get("_receipt_status") != "COMPLETE"
        )
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
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        **dict(base),
        "strategy_id": str(contributor.get("strategy_id") or ""),
        "strategy_version": str(contributor.get("strategy_version") or ""),
        "receipt_id": str(contributor.get("receipt_id") or ""),
        "receipt_hash_sha256": str(contributor.get("receipt_hash_sha256") or ""),
        "source_signal_id": str(contributor.get("source_signal_id") or ""),
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
    metrics = outcome.get("selection_outcome_metrics") if isinstance(outcome, Mapping) else None
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
    """Validate the carried automatic-capture proof before learning/persisting."""

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
    for item in canonical_lineage:
        if str(item.get("ticker") or ticker).upper() != ticker:
            raise SnapshotValidationError("research outcome lineage ticker binding mismatch")
        if item.get("source_bar_hash_sha256") not in (None, "", bar_hash):
            raise SnapshotValidationError("research outcome lineage bar binding mismatch")
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
    parsed_url = urlparse(source_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise SnapshotValidationError("research outcome source URL is not governed")
    if provider.lower() in {"yahoo", "yahoo_finance", "yahoo finance", "yahoo_finance_chart"}:
        if (
            parsed_url.hostname != "query1.finance.yahoo.com"
            or parsed_url.path != f"/v8/finance/chart/{ticker}"
            or parse_qs(parsed_url.query, keep_blank_values=True)
            != {"range": ["5d"], "interval": ["1m"], "includePrePost": ["false"]}
        ):
            raise SnapshotValidationError("research outcome Yahoo source URL is not governed")
    elif provider.lower().startswith("alpaca_market_data_"):
        if (
            parsed_url.hostname != "data.alpaca.markets"
            or parsed_url.path != "/v2/stocks/bars"
            or parsed_url.query
        ):
            raise SnapshotValidationError("research outcome Alpaca source URL is not governed")
    else:
        raise SnapshotValidationError("research outcome provider is not allowlisted")
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
    if str(path_payload.get("source_bar_hash_sha256") or "").lower() != bar_hash:
        raise SnapshotValidationError("research outcome path bar binding mismatch")
    expected_observation_id = (
        f"selection-observation:{row.get('selection_id')}:{observation_payload.get('observed_at')}"
    )
    if str(row.get("source_observation_id") or "") != expected_observation_id:
        raise SnapshotValidationError("research outcome observation identity mismatch")
    expected_path_id = f"selection-path:{row.get('selection_id')}:{bar_hash[:24]}"
    if str(row.get("source_path_id") or "") != expected_path_id:
        raise SnapshotValidationError("research outcome path identity is not producer-bound")
    metric_body = {
        "selection_id": str(row.get("selection_id") or ""),
        "signal_id": str(row.get("signal_id") or ""),
        "ticker": str(row.get("ticker") or "").upper(),
        "market_date": str(row.get("market_date") or "")[:10],
        "source_bar_hash_sha256": bar_hash,
        "source_binding": dict(binding),
        "metrics": dict(metrics),
    }
    expected_metric_hash = _digest(metric_body)
    if str(row.get("outcome_artifact_hash_sha256") or "").lower() != expected_metric_hash:
        raise SnapshotValidationError("research outcome artifact hash mismatch")
    if str(row.get("outcome_artifact_id") or "") != (
        f"selection-outcome:{row.get('selection_id')}:{expected_metric_hash[:24]}"
    ):
        raise SnapshotValidationError("research outcome metric artifact identity mismatch")
    if not _sha256(str(row.get("source_observation_hash_sha256") or "")):
        raise SnapshotValidationError("research outcome observation hash is absent")
    if not _sha256(str(row.get("source_path_hash_sha256") or "")):
        raise SnapshotValidationError("research outcome path hash is absent")


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
