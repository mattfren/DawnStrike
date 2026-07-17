"""Collect and persist post-market daily mover lists."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from intraday_scanner.errors import MarketCalendarCoverageError
from intraday_scanner.market_calendar import MARKET_TIMEZONE, market_session
from intraday_scanner.models import utc_now_iso
from intraday_scanner.providers.daily_movers_base import (
    CURRENT_WEB_KIND,
    CURRENT_WEB_ROLE,
    DAILY_MOVER_COLUMNS,
    DESCRIPTIVE_EOD_ROLE,
    REALIZED_EOD_KIND,
    VERIFIED_CORPORATE_ACTION_STATUSES,
    sha256_file_ref,
    write_daily_mover_csv,
    write_rejected_mover_csv,
)
from intraday_scanner.providers.local_daily_movers_provider import (
    LocalDailyMoversProvider,
    _local_eod_truth_gate,
)
from intraday_scanner.providers.stockanalysis_daily_movers import (
    StockAnalysisDailyMoversProvider,
)
from intraday_scanner.providers.tradingview_daily_movers import TradingViewDailyMoversProvider
from intraday_scanner.providers.web_source_base import (
    WebCollectionConfig,
    WebSourceConfig,
    enabled_sources,
    load_web_sources_config,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

DEFAULT_LOCAL_DAILY_MOVERS_DIR = Path("data/inbox/daily_movers")


def collect_daily_movers(
    *,
    market_date: str,
    config_path: str | Path = "config/web_sources.yaml",
    db_path: str | Path = "data/shadow_real.sqlite",
    out_dir: str | Path = "outputs/daily_movers",
    persist: bool = False,
    print_rows: bool = False,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    public_web_gate = _public_web_collection_gate(market_date, as_of=as_of)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_web_sources_config(config_path)
    attempts: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    selected_provider = ""

    for provider_name, provider in _providers(
        market_date,
        config,
        include_public_web=bool(public_web_gate["eligible"]),
    ):
        attempt_dir = output_dir / provider_name
        result = provider.collect(market_date=market_date, out_dir=attempt_dir)
        attempts.append(_compact_attempt(result))
        rejected_rows.extend(list(result.get("rejected_rows") or []))
        if result.get("status") == "success" and result.get("rows"):
            candidate_rows = list(result["rows"])
            if provider_name == "local_daily_movers" and not _local_rows_are_eligible(
                candidate_rows,
                market_date=market_date,
            ):
                attempts[-1]["status"] = "ineligible_source_truth"
                attempts[-1]["failure_reason"] = (
                    "local rows failed independent EOD label truth validation"
                )
                continue
            rows = candidate_rows
            selected_provider = provider_name
            break

    if not public_web_gate["eligible"] and not rows:
        attempts.append(
            {
                "status": "skipped",
                "source": "public_daily_movers",
                "source_type": "descriptive_eod_movers",
                "failure_reason": str(public_web_gate["reason"]),
                "session_gate": public_web_gate,
            }
        )

    rows = _dedupe_movers(rows)
    _attach_ingestion_truth(
        rows,
        selected_provider=selected_provider,
        public_web_gate=public_web_gate,
    )
    paths = {
        "daily_movers": output_dir / "daily_movers.csv",
        "rejected_movers": output_dir / "rejected_movers.csv",
        "daily_movers_summary": output_dir / "daily_movers_summary.json",
        "source_debug": output_dir / "source_debug.json",
    }
    write_daily_mover_csv(paths["daily_movers"], rows)
    write_rejected_mover_csv(paths["rejected_movers"], rejected_rows)
    status = "success" if rows else "no_data"
    source_status = "success" if rows else _source_failure_reason(attempts)
    summary = {
        "status": status,
        "market_date": market_date,
        "created_at": utc_now_iso(),
        "source_status": source_status,
        "mover_count": len(rows),
        "rejected_count": len(rejected_rows),
        "source_attempt_count": len(attempts),
        "selected_provider": selected_provider or None,
        "public_web_gate": public_web_gate,
        "columns": DAILY_MOVER_COLUMNS,
        "paths": {key: str(value) for key, value in paths.items()},
        "data_quality_note": (
            "Public current-session gainers are descriptive only and never complete "
            "EOD labels. Local EOD labels require a retained SHA-256 artifact, "
            "complete coverage, after-close timestamps, and verified corporate actions."
        ),
    }
    paths["daily_movers_summary"].write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    debug = {"attempts": attempts, "rejected_rows": rejected_rows[:100]}
    paths["source_debug"].write_text(json.dumps(debug, indent=2, sort_keys=True), encoding="utf-8")
    if persist:
        store = SQLiteScanStore(db_path)
        store.initialize()
        if rows:
            if selected_provider != "local_daily_movers" and not public_web_gate["eligible"]:
                raise RuntimeError(
                    "public daily mover persistence requires the current published "
                    "trading session after its official exchange close"
                )
            store.persist_daily_market_movers(rows, market_date=market_date, replace=True)
        store.record_source_health(
            "daily_market_movers",
            "ok" if rows else "failed",
            utc_now_iso(),
            f"movers={len(rows)} status={source_status}",
            summary,
        )
    result = {
        **summary,
        "rows": rows,
        "rejected_rows": rejected_rows,
        "attempts": attempts,
    }
    if print_rows:
        print(json.dumps(_printable_result(result), indent=2, sort_keys=True))
    return result


def _providers(
    market_date: str,
    config: WebCollectionConfig,
    *,
    include_public_web: bool = True,
) -> list[tuple[str, Any]]:
    local_path = DEFAULT_LOCAL_DAILY_MOVERS_DIR / f"daily_movers_{market_date}.csv"
    providers: list[tuple[str, Any]] = [
        ("local_daily_movers", LocalDailyMoversProvider(local_path))
    ]
    if not include_public_web:
        return providers
    stockanalysis = [
        source
        for source in enabled_sources(config)
        if source.type
        in {"stockanalysis_daily_movers", "daily_movers_public_table"}
        and _matches_public_gainers_path(
            source.url,
            host="stockanalysis.com",
            path="/markets/gainers/",
        )
    ]
    tradingview = [
        source
        for source in enabled_sources(config)
        if source.type
        in {"tradingview_daily_movers", "daily_movers_public_table"}
        and _matches_public_gainers_path(
            source.url,
            host="tradingview.com",
            path="/markets/stocks-usa/market-movers-gainers/",
        )
    ]
    if not stockanalysis:
        stockanalysis = [
            WebSourceConfig(
                name="stockanalysis_daily_movers",
                type="stockanalysis_daily_movers",
                url="https://stockanalysis.com/markets/gainers/",
            )
        ]
    if not tradingview:
        tradingview = [
            WebSourceConfig(
                name="tradingview_daily_movers",
                type="tradingview_daily_movers",
                url="https://www.tradingview.com/markets/stocks-usa/market-movers-gainers/",
            )
        ]
    providers.extend(
        (f"stockanalysis_{index}", StockAnalysisDailyMoversProvider(source, config))
        for index, source in enumerate(stockanalysis, start=1)
    )
    providers.extend(
        (f"tradingview_{index}", TradingViewDailyMoversProvider(source, config))
        for index, source in enumerate(tradingview, start=1)
    )
    return providers


def _public_web_collection_gate(
    market_date: str,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Authorize an EOD public-table read without misdating realized movers.

    Public ``/gainers/`` pages describe the site's current session.  They do
    not provide a trustworthy historical-date selector, so a requested date
    must match the current New York date and its scheduled core session must
    have reached the published close.  Local operator CSV imports are handled
    separately and are not restricted by this gate.
    """

    requested_date = date.fromisoformat(market_date)
    current = as_of or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("daily mover collection timestamps must include a timezone")
    current_et = current.astimezone(MARKET_TIMEZONE)
    payload: dict[str, Any] = {
        "eligible": False,
        "reason": "",
        "market_date": market_date,
        "current_market_date": current_et.date().isoformat(),
        "evaluated_at": current.isoformat(),
        "evaluated_at_et": current_et.isoformat(),
        "dataset_role": CURRENT_WEB_ROLE,
        "prospective_signal_eligible": False,
        "source_snapshot_kind": CURRENT_WEB_KIND,
        "source_coverage_complete": False,
        "corporate_action_status": "unverified",
        "calendar": None,
    }
    if requested_date != current_et.date():
        payload["reason"] = "public_gainers_requires_current_market_date"
        return payload
    try:
        decision = market_session(requested_date)
    except MarketCalendarCoverageError as exc:
        payload["reason"] = "public_gainers_requires_published_calendar_coverage"
        payload["calendar_error"] = str(exc)
        return payload
    payload["calendar"] = decision.to_dict()
    if not decision.is_trading_day or decision.close_time_et is None:
        payload["reason"] = "public_gainers_requires_published_trading_session"
        return payload
    scheduled_close = time.fromisoformat(decision.close_time_et)
    if current_et.time().replace(tzinfo=None) < scheduled_close:
        payload["reason"] = "public_gainers_unavailable_before_official_exchange_close"
        return payload
    payload["eligible"] = True
    payload["reason"] = "current_published_session_after_official_exchange_close"
    return payload


def _attach_ingestion_truth(
    rows: list[dict[str, Any]],
    *,
    selected_provider: str,
    public_web_gate: dict[str, Any],
) -> None:
    channel = (
        "local_operator_csv"
        if selected_provider == "local_daily_movers"
        else "public_web_current_session_gainers"
    )
    for row in rows:
        payload = dict(row.get("payload_json") or {})
        if channel == "local_operator_csv":
            truth = {
                "dataset_role": DESCRIPTIVE_EOD_ROLE,
                "prospective_signal_eligible": False,
                "source_snapshot_kind": REALIZED_EOD_KIND,
                "ingestion_channel": channel,
                "source_coverage_complete": True,
                "source_complete": True,
                "list_coverage_complete": True,
                "eod_label_eligible": True,
            }
        else:
            # A public page is a current, provider-defined view. Even after the
            # exchange close it does not prove universe coverage, adjustment,
            # or a durable official EOD list.
            truth = {
                "dataset_role": CURRENT_WEB_ROLE,
                "prospective_signal_eligible": False,
                "source_snapshot_kind": CURRENT_WEB_KIND,
                "ingestion_channel": channel,
                "source_coverage_complete": False,
                "source_complete": False,
                "list_coverage_complete": False,
                "expected_row_count": None,
                "corporate_action_status": "unverified",
                "eod_label_eligible": False,
            }
            payload["public_web_session_gate"] = dict(public_web_gate)
        payload.update(truth)
        row.update(truth)
        row["payload_json"] = payload


def _local_rows_are_eligible(
    rows: list[dict[str, Any]],
    *,
    market_date: str,
) -> bool:
    if not rows:
        return False
    artifact_refs = {str(row.get("source_artifact_ref") or "") for row in rows}
    artifact_paths = {str(row.get("source_artifact_path") or "") for row in rows}
    if len(artifact_refs) != 1 or len(artifact_paths) != 1:
        return False
    artifact_ref = next(iter(artifact_refs))
    artifact_path = Path(next(iter(artifact_paths)))
    if not artifact_path.is_file():
        return False
    try:
        if sha256_file_ref(artifact_path) != artifact_ref:
            return False
    except OSError:
        return False
    gate = _local_eod_truth_gate(
        rows,
        market_date=market_date,
        artifact_ref=artifact_ref,
    )
    expected_count = len(rows)
    return bool(
        gate["eligible"]
        and all(
            row.get("dataset_role") == DESCRIPTIVE_EOD_ROLE
            and row.get("source_snapshot_kind") == REALIZED_EOD_KIND
            and row.get("prospective_signal_eligible") is False
            and row.get("ingestion_channel") == "local_operator_csv"
            and row.get("source_coverage_complete") is True
            and row.get("source_complete") is True
            and row.get("list_coverage_complete") is True
            and row.get("eod_label_eligible") is True
            and row.get("expected_row_count") == expected_count
            and row.get("source_ref") == artifact_ref
            and str(row.get("corporate_action_status") or "").lower()
            in VERIFIED_CORPORATE_ACTION_STATUSES
            for row in rows
        )
    )


def _matches_public_gainers_path(url: str, *, host: str, path: str) -> bool:
    parsed = urlparse(url)
    normalized_host = (parsed.hostname or "").lower()
    normalized_path = f"/{parsed.path.strip('/')}/".lower()
    return (
        normalized_host in {host, f"www.{host}"}
        and normalized_path == path.lower()
    )


def _dedupe_movers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: int(item.get("rank") or 999999)):
        ticker = str(row.get("ticker") or "").upper()
        if ticker in seen:
            continue
        seen.add(ticker)
        output.append(row)
    for index, row in enumerate(output, start=1):
        row["rank"] = index
        row["mover_id"] = (
            f"mover:{row.get('market_date')}:{row.get('source')}:{index}:{row['ticker']}"
        )
    return output


def _compact_attempt(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"rows", "rejected_rows"}
    }


def _source_failure_reason(attempts: list[dict[str, Any]]) -> str:
    reasons = [
        str(item.get("failure_reason") or item.get("status") or "")
        for item in attempts
        if str(item.get("status") or "") not in {"success", ""}
    ]
    return "; ".join(reason for reason in reasons if reason) or "no mover rows collected"


def _printable_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key not in {"rows", "rejected_rows"}
    }
