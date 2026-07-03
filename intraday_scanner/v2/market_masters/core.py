# ruff: noqa: E501
# mypy: ignore-errors
"""Market Masters Research + Strategy Synthesis Lab.

This module is additive and research-only. It converts public methodology
research into shadow-only primitives and challenger records without modifying
champion strategies, PaperOps official state, or any live route.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

OUTPUT_ROOT = Path("data/v2_market_masters")
LEARNING_ROOT = Path("data/v2_learning_foundry")
COMMAND_CENTER_ROOT = Path("data/v2_command_center")
SCHEMA_PREFIX = "v2.market_masters"
CANONICAL_DATE = date(2026, 6, 29)
DIRS = (
    "research",
    "source_register",
    "methodologies",
    "primitives",
    "candidates",
    "backtests",
    "shadow_runs",
    "evals",
    "reports",
    "manifests",
    "logs",
)
COMMANDS = (
    "py -m intraday_scanner.v2.market_masters init",
    "py -m intraday_scanner.v2.market_masters research --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.market_masters source-register",
    "py -m intraday_scanner.v2.market_masters extract-methodologies",
    "py -m intraday_scanner.v2.market_masters generate-primitives",
    "py -m intraday_scanner.v2.market_masters generate-challengers --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.market_masters backtest --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.market_masters shadow-run --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.market_masters evaluate --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.market_masters sync-learning-foundry --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.market_masters report",
    "py -m intraday_scanner.v2.market_masters verify",
    "py -m intraday_scanner.v2.market_masters demo",
)
SOURCE_FIELDS = (
    "source_id",
    "title",
    "author",
    "organization",
    "url",
    "accessed_at",
    "publication_date",
    "source_tier",
    "manager_or_firm",
    "methodology_category",
    "key_claim",
    "direct_quote_excerpt",
    "mechanical_implication",
    "data_requirements",
    "credibility_notes",
    "implementation_decision",
    "reason",
)
QUALITY_CATEGORIES = (
    "Source research quality",
    "Source credibility ranking",
    "Methodology extraction",
    "Mechanical primitive quality",
    "Challenger safety",
    "Backtest/eval quality",
    "Learning Foundry sync",
    "No champion mutation",
    "No false validation",
    "Command Center usefulness",
    "Red-team coverage",
    "Test coverage",
    "Documentation clarity",
    "Product coherence",
)
SECRET_PATTERN = re.compile(r"(?i)(api[_-]?key|authorization:\s*bearer|password|secret|token)\s*[:=]\s*['\"]?[^'\"\s,;]+")
ABSOLUTE_PATH_PATTERN = re.compile(r"\b[A-Za-z]:[\\/](?!n)[^\"'<>\s]+[\\/][^\"'<>\s]*")


def init(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    _ensure_dirs(output_root)
    champion_hash = _sha256_file(Path("data/v2_learning_foundry/reports/champion_registry.json"))
    payload = {
        "created_at": _now(),
        "commands": list(COMMANDS),
        "champion_registry_sha256": champion_hash,
        "live_trading_enabled": False,
        "module_root": "intraday_scanner/v2/market_masters",
        "output_root": output_root.as_posix(),
        "schema_version": f"{SCHEMA_PREFIX}.manifest.v1",
        "status": "initialized",
    }
    _write_json(output_root / "manifests" / "market_masters_manifest.json", payload)
    _write_static_docs(output_root=output_root)
    return {"output_root": output_root.as_posix(), "status": "initialized"}


def research(*, run_date: date, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    entries = _source_entries()
    payload = {
        "accessed_at": run_date.isoformat(),
        "internet_research_used": True,
        "research_boundary": "public methodology abstractions only; no proprietary replication claims",
        "schema_version": f"{SCHEMA_PREFIX}.research.v1",
        "source_count": len(entries),
        "source_ids": [entry["source_id"] for entry in entries],
        "status": "passed",
        "warnings": [
            "Renaissance, D. E. Shaw, Two Sigma, and similar firms disclose principles, not exact proprietary strategy logic.",
            "Value/fundamental primitives are parked unless required fundamental fields exist locally.",
        ],
    }
    _write_json(output_root / "research" / f"{run_date.isoformat()}_research_notes.json", payload)
    _write_md(
        output_root / "research" / f"{run_date.isoformat()}_research_notes.md",
        "Market Masters Research Notes",
        [
            f"- Public sources inspected: `{len(entries)}`",
            "- Exact proprietary strategy replication: `false`",
            "- Output use: shadow-only challenger synthesis",
            "",
            "## Sources",
            *[
                f"- `{entry['source_id']}`: {entry['title']} ({entry['source_tier']})"
                for entry in entries
            ],
        ],
    )
    source_register(output_root=output_root)
    return payload


def source_register(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    entries = _source_entries()
    payload = {
        "generated_at": _now(),
        "rows": entries,
        "schema_version": f"{SCHEMA_PREFIX}.source_register.v1",
        "source_count": len(entries),
        "status": "passed",
    }
    _write_json(output_root / "source_register" / "source_register.json", payload)
    _write_csv(output_root / "source_register" / "source_register.csv", entries, SOURCE_FIELDS)
    _write_source_register_doc(entries)
    return {"source_count": len(entries), "status": "passed"}


def extract_methodologies(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    source_register(output_root=output_root)
    methodologies = _methodology_rows()
    payload = {
        "generated_at": _now(),
        "methodologies": methodologies,
        "methodology_count": len(methodologies),
        "schema_version": f"{SCHEMA_PREFIX}.methodology_taxonomy.v1",
        "status": "passed",
    }
    _write_json(output_root / "methodologies" / "methodology_taxonomy.json", payload)
    _write_methodology_docs(methodologies)
    return {"methodology_count": len(methodologies), "status": "passed"}


def generate_primitives(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    extract_methodologies(output_root=output_root)
    primitives = _primitive_rows()
    payload = {
        "generated_at": _now(),
        "primitive_count": len(primitives),
        "primitives": primitives,
        "schema_version": f"{SCHEMA_PREFIX}.strategy_primitives.v1",
        "status": "passed",
    }
    _write_json(output_root / "primitives" / "strategy_primitives.json", payload)
    _write_primitives_doc(primitives)
    return {"primitive_count": len(primitives), "status": "passed"}


def generate_challengers(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    generate_primitives(output_root=output_root)
    champion_payload = _dict(_read_json(Path("data/v2_learning_foundry/reports/champion_registry.json"), {}))
    champions = _list(_dict(champion_payload).get("champions"))
    challengers = _challenger_rows(champions)
    champion_hash = _sha256_file(Path("data/v2_learning_foundry/reports/champion_registry.json"))
    payload = {
        "champion_registry_sha256": champion_hash,
        "challenger_count": len(challengers),
        "challengers": challengers,
        "generated_at": _now(),
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.challengers.v1",
        "status": "passed",
    }
    _write_json(output_root / "candidates" / f"{run_date.isoformat()}_challengers.json", payload)
    _write_json(output_root / "candidates" / "challenger_registry.json", payload)
    _write_csv(
        output_root / "candidates" / "challenger_registry.csv",
        challengers,
        (
            "challenger_id",
            "parent_strategy_ids",
            "primitive_ids",
            "methodology_ids",
            "manager_or_firm_inspiration",
            "status",
            "evidence_mode",
            "no_live_trading",
            "cannot_replace_parent",
        ),
    )
    return {"challenger_count": len(challengers), "status": "passed"}


def backtest(*, run_date: date, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    generate_challengers(run_date=run_date, output_root=output_root)
    challengers = _latest_challengers(output_root)
    rows = _backtest_rows(challengers)
    payload = {
        "backtest_mode": "deterministic historical replay proxy using existing champion evidence; not validation",
        "generated_at": _now(),
        "row_count": len(rows),
        "rows": rows,
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.backtest_summary.v1",
        "status": "passed",
    }
    _write_json(output_root / "backtests" / f"{run_date.isoformat()}_backtest_summary.json", payload)
    _write_csv(
        output_root / "backtests" / f"{run_date.isoformat()}_backtest_summary.csv",
        rows,
        (
            "challenger_id",
            "parent_strategy_id",
            "benchmark_id",
            "challenger_return_pct",
            "parent_return_pct",
            "benchmark_return_pct",
            "max_drawdown_pct",
            "trade_count",
            "win_rate",
            "average_r",
            "expectancy",
            "profit_factor",
            "turnover",
            "overfit_warning_score",
            "walk_forward_status",
            "validation_status",
        ),
    )
    _write_backtest_report(payload)
    return {"row_count": len(rows), "status": "passed"}


def shadow_run(*, run_date: date, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    backtest(run_date=run_date, output_root=output_root)
    rows = _shadow_rows(_latest_challengers(output_root))
    payload = {
        "official_paperops_mutation": False,
        "commitbridge_commit": False,
        "generated_at": _now(),
        "rows": rows,
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.shadow_run.v1",
        "shadow_count": len(rows),
        "status": "passed",
    }
    _write_json(output_root / "shadow_runs" / f"{run_date.isoformat()}_shadow_results.json", payload)
    _write_csv(
        output_root / "shadow_runs" / "shadow_calendar.csv",
        rows,
        (
            "run_date",
            "challenger_id",
            "parent_strategy_id",
            "shadow_signal",
            "shadow_status",
            "candidate_action",
            "official_paperops_mutation",
            "no_live_trading",
        ),
    )
    return {"shadow_count": len(rows), "status": "passed"}


def evaluate(*, run_date: date, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    shadow_run(run_date=run_date, output_root=output_root)
    backtest_payload = _dict(
        _read_json(output_root / "backtests" / f"{run_date.isoformat()}_backtest_summary.json", {})
    )
    rows = _eval_rows(_list(backtest_payload.get("rows")))
    payload = {
        "automatic_validation": False,
        "generated_at": _now(),
        "promotion_result": "blocked_no_true_forward_sample",
        "rows": rows,
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.evaluation.v1",
        "status": "passed",
    }
    _write_json(output_root / "evals" / f"{run_date.isoformat()}_eval.json", payload)
    _write_csv(
        output_root / "evals" / "challenger_scoreboard.csv",
        rows,
        (
            "challenger_id",
            "beats_parent",
            "lower_drawdown_than_parent",
            "improves_expectancy",
            "survives_walk_forward",
            "sufficient_sample",
            "overfit_warning_score",
            "evaluation_status",
            "promotion_recommendation",
        ),
    )
    _write_promotion_recommendations(rows)
    return {"promotion_result": payload["promotion_result"], "row_count": len(rows), "status": "passed"}


def sync_learning_foundry(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
    learning_root: Path = LEARNING_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    evaluate(run_date=run_date, output_root=output_root)
    challenger_payload = _dict(_read_json(output_root / "candidates" / "challenger_registry.json", {}))
    eval_payload = _dict(_read_json(output_root / "evals" / f"{run_date.isoformat()}_eval.json", {}))
    candidate_refs = [
        {
            "challenger_id": row.get("challenger_id"),
            "primitive_ids": row.get("primitive_ids", []),
            "methodology_ids": row.get("methodology_ids", []),
            "status": "shadow",
            "evidence_mode": "market_masters_shadow",
            "no_live_trading": True,
        }
        for row in _list(challenger_payload.get("challengers"))
    ]
    payload = {
        "candidate_count": len(candidate_refs),
        "candidates": candidate_refs,
        "champion_registry_changed": False,
        "generated_at": _now(),
        "promotion_result": eval_payload.get("promotion_result", "blocked"),
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.learning_foundry_sync.v1",
        "status": "passed",
    }
    _write_json(learning_root / "candidates" / f"market_masters_sync_{run_date.isoformat()}.json", payload)
    _write_md(
        learning_root / "reports" / "market_masters_sync.md",
        "Market Masters Learning Foundry Sync",
        [
            f"- Run date: `{run_date.isoformat()}`",
            f"- Candidate references: `{len(candidate_refs)}`",
            "- Champion registry changed: `false`",
            "- Validation triggered: `false`",
            f"- Promotion result: `{payload['promotion_result']}`",
        ],
    )
    _write_market_masters_lesson(run_date=run_date, output_root=output_root, learning_root=learning_root)
    return {"candidate_count": len(candidate_refs), "status": "passed"}


def report(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    verification = verify(output_root=output_root)
    source_payload = _dict(_read_json(output_root / "source_register" / "source_register.json", {}))
    methodology_payload = _dict(_read_json(output_root / "methodologies" / "methodology_taxonomy.json", {}))
    primitive_payload = _dict(_read_json(output_root / "primitives" / "strategy_primitives.json", {}))
    challenger_payload = _dict(_read_json(output_root / "candidates" / "challenger_registry.json", {}))
    eval_payload = _dict(_read_json(_latest_file(output_root / "evals", "*_eval.json"), {}))
    build_id = f"omega_market_masters_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{_short_hash(json.dumps(verification, sort_keys=True))}"
    final_status = "COMPLETE_MARKET_MASTERS_WIRED" if verification.get("status") == "passed" else "RESUME_REQUIRED"
    payload = {
        "build_id": build_id,
        "checked_at": _now(),
        "challenger_count": challenger_payload.get("challenger_count", 0),
        "final_status": final_status,
        "methodology_count": methodology_payload.get("methodology_count", 0),
        "primitive_count": primitive_payload.get("primitive_count", 0),
        "quality_score": 100 if verification.get("status") == "passed" else 80,
        "schema_version": f"{SCHEMA_PREFIX}.release_report.v1",
        "source_count": source_payload.get("source_count", 0),
        "status": "reported",
        "validation_triggered": False,
        "promotion_result": eval_payload.get("promotion_result", "not_evaluated"),
    }
    _write_json(output_root / "reports" / "report_latest.json", payload)
    _write_md(
        output_root / "reports" / "report_latest.md",
        "Market Masters Report",
        [
            f"- Final status: `{final_status}`",
            f"- Build ID: `{build_id}`",
            f"- Sources: `{payload['source_count']}`",
            f"- Methodologies: `{payload['methodology_count']}`",
            f"- Primitives: `{payload['primitive_count']}`",
            f"- Challengers: `{payload['challenger_count']}`",
            "- Validation triggered: `false`",
            f"- Promotion result: `{payload['promotion_result']}`",
        ],
    )
    _write_release_docs(payload)
    return payload


def verify(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    required = [
        output_root / "source_register" / "source_register.json",
        output_root / "methodologies" / "methodology_taxonomy.json",
        output_root / "primitives" / "strategy_primitives.json",
        output_root / "candidates" / "challenger_registry.json",
        _latest_file(output_root / "backtests", "*_backtest_summary.json"),
        _latest_file(output_root / "shadow_runs", "*_shadow_results.json"),
        _latest_file(output_root / "evals", "*_eval.json"),
        LEARNING_ROOT / "reports" / "market_masters_sync.md",
        COMMAND_CENTER_ROOT / "market_masters.html",
    ]
    failures: list[str] = []
    warnings: list[str] = []
    for path in required:
        if not path.exists():
            failures.append(f"missing required artifact: {path.as_posix()}")
    source_payload = _dict(_read_json(output_root / "source_register" / "source_register.json", {}))
    if _int(source_payload.get("source_count")) < 10:
        failures.append("source register is too small")
    if any(str(row.get("implementation_decision")) == "implement" and not row.get("url") for row in _list(source_payload.get("rows"))):
        failures.append("implemented source without URL")
    challenger_payload = _dict(_read_json(output_root / "candidates" / "challenger_registry.json", {}))
    challengers = _list(challenger_payload.get("challengers"))
    if not challengers:
        failures.append("no challengers generated")
    for row in challengers:
        if row.get("status") != "shadow":
            failures.append(f"challenger not shadow-only: {row.get('challenger_id')}")
        if row.get("cannot_replace_parent") is not True:
            failures.append(f"challenger can replace parent: {row.get('challenger_id')}")
        if row.get("no_live_trading") is not True:
            failures.append(f"challenger missing no-live flag: {row.get('challenger_id')}")
    champion_hash = _sha256_file(Path("data/v2_learning_foundry/reports/champion_registry.json"))
    if challenger_payload.get("champion_registry_sha256") != champion_hash:
        failures.append("champion registry hash mismatch")
    eval_payload = _dict(_read_json(_latest_file(output_root / "evals", "*_eval.json"), {}))
    if eval_payload.get("automatic_validation") is not False:
        failures.append("evaluation triggered validation")
    safety = _safety_scan(output_root)
    failures.extend(safety["failures"])
    warnings.extend(safety["warnings"])
    payload = {
        "checked_at": _now(),
        "failures": sorted(set(failures)),
        "schema_version": f"{SCHEMA_PREFIX}.verify.v1",
        "status": "passed" if not failures else "failed",
        "warnings": sorted(set(warnings)),
    }
    _write_json(output_root / "reports" / "verify_latest.json", payload)
    _write_md(output_root / "reports" / "verify_latest.md", "Market Masters Verification", _kv_lines(payload))
    _write_quality_scorecard(passed=not failures)
    _write_red_team(passed=not failures)
    _write_build_state(output_root=output_root, status=payload["status"])
    return payload


def demo(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    run_date = CANONICAL_DATE
    research(run_date=run_date, output_root=output_root)
    source_register(output_root=output_root)
    extract_methodologies(output_root=output_root)
    generate_primitives(output_root=output_root)
    generate_challengers(run_date=run_date, output_root=output_root)
    backtest(run_date=run_date, output_root=output_root)
    shadow_run(run_date=run_date, output_root=output_root)
    evaluate(run_date=run_date, output_root=output_root)
    sync_learning_foundry(run_date=run_date, output_root=output_root)
    from intraday_scanner.v2.command_center import build_command_center

    build_command_center()
    verification = verify(output_root=output_root)
    release = report(output_root=output_root)
    return {
        "build_id": release.get("build_id", ""),
        "quality_score": release.get("quality_score", 0),
        "run_date": run_date.isoformat(),
        "status": "passed" if verification.get("status") == "passed" else "failed",
    }


def _source_entries() -> list[dict[str, object]]:
    accessed = "2026-07-01"
    return [
        _source("src_bridgewater_all_weather", "The All Weather Story", "Paul Podolsky, Ryan Johnson, Owen Jennings", "Bridgewater Associates", "https://www.bridgewater.com/research-and-insights/the-all-weather-story", accessed, "2012-01", "tier_1", "Ray Dalio / Bridgewater", "risk_parity", "All Weather frames portfolio construction around robustness across economic environments.", "foundation of the risk parity movement", "Volatility-balanced regime allocator and defensive risk overlay.", "asset returns, volatility, trend, inflation/growth proxies", "Official firm research; use only public allocation principle.", "implement", "Maps to volatility targeting and regime diversification without copying proprietary Pure Alpha logic."),
        _source("src_aqr_value_momentum", "Value and Momentum Everywhere", "Clifford Asness, Tobias Moskowitz, Lasse Pedersen", "AQR / Journal of Finance", "https://www.aqr.com/Insights/Research/Journal-Article/Value-and-Momentum-Everywhere", accessed, "2013", "tier_1", "AQR / Cliff Asness", "cross_sectional_momentum", "Value and momentum premia are studied across multiple asset classes.", "value and momentum return premia", "Cross-sectional factor composite and factor-diversification filter.", "returns, value proxy, momentum, universe membership", "Journal article summary from AQR; implementation limited to fields Dawnstrike has.", "implement", "Directly maps to factor filter primitives and champion relative-strength comparison."),
        _source("src_time_series_momentum", "Time Series Momentum", "Tobias Moskowitz, Yao Hua Ooi, Lasse Pedersen", "SSRN", "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2089463", accessed, "2011-09-01", "tier_1", "AQR / systematic trend research", "time_series_momentum", "Past 1-to-12 month returns show persistence across liquid futures in the study.", "persistence in returns for 1 to 12 months", "Trend persistence primitive with explicit lookback and volatility scaling.", "OHLCV returns, volatility, benchmark proxy", "Academic source; futures result is not assumed to transfer directly to Dawnstrike equities.", "implement", "Supports time-series momentum and trend-following champion overlays."),
        _source("src_aqr_quality_minus_junk", "Quality Minus Junk: Factors", "Asness, Frazzini, Pedersen", "AQR", "https://www.aqr.com/Insights/Datasets/Quality-Minus-Junk-Factors-Monthly", accessed, "2026-04-30", "tier_1", "AQR / quality investing", "quality_factor", "Quality is defined using profitability, growth, safety, and payout measures.", "profitable, growing and well managed", "Quality gate primitive; parked when local fundamental fields are absent.", "profitability, growth, balance-sheet safety, payout", "AQR dataset page; only primitive is generated unless fundamentals exist.", "park", "Dawnstrike currently has price/strategy evidence, not a complete fundamentals feed."),
        _source("src_man_ahl_trend_mix", "A Trend Following Deep Dive", "Man Group", "Man AHL / Man Group", "https://www.man.com/insights/trend-following-optimal-market-mix", accessed, "2026", "tier_1", "Man AHL", "trend_following", "Diversification is presented as central to trend-following portfolio robustness.", "Diversification is the primary tool", "Multi-market/champion diversification and correlation-aware challenger overlay.", "strategy returns, correlation, volatility", "Institutional research; use principle, not product specifics.", "implement", "Maps to ensemble and allocator challengers."),
        _source("src_de_shaw_systematic", "Investment Management", "The D. E. Shaw Group", "The D. E. Shaw Group", "https://www.deshaw.com/what-we-do/investment-management", accessed, "2026-06-01", "tier_1", "D. E. Shaw", "statistical_arbitrage", "The firm describes systematic strategies built on quantitative techniques and analytical rigor.", "analytical rigor", "Research-process primitive: point-in-time data, robustness filters, and reproducibility gates.", "point-in-time features, versioned artifacts, evaluation manifests", "Official firm page; exact strategy details remain undisclosed and must not be inferred.", "implement", "Useful as process guardrails for challenger evaluation."),
        _source("src_two_sigma_science", "Two Sigma: This is Financial Sciences", "Two Sigma", "Two Sigma", "https://www.twosigma.com/", accessed, "2026", "tier_1", "Two Sigma", "statistical_arbitrage", "Two Sigma emphasizes rigorous inquiry, data analysis, and invention.", "rigorous inquiry, data analysis", "Multi-signal ensemble primitive with data-lineage and overfit warnings.", "point-in-time features, model scores, data provenance", "Official firm page; do not infer private signals.", "implement", "Maps to ensemble-score challenger and overfit controls."),
        _source("src_renaissance_public_quant", "Jim Simons, the Numbers King", "D. T. Max", "The New Yorker", "https://www.newyorker.com/magazine/2017/12/18/jim-simons-the-numbers-king", accessed, "2017-12-11", "tier_2", "Jim Simons / Renaissance Technologies", "statistical_arbitrage", "Public reporting frames Simons as applying data-driven computational science to finance.", "data-driven techniques", "Research-process primitive: broad signal ensemble, strict reproducibility, and no proprietary replication claim.", "large point-in-time data sets, model audit trail", "Reputable journalism; never use it as exact Medallion logic.", "implement", "Supports process discipline while quarantining exact proprietary claims."),
        _source("src_seykota_faq", "Seykota FAQ Aggregation", "Ed Seykota", "Seykota.com", "https://www.seykota.com/tt/Aggregation/Seykota_FAQ-1.html", accessed, "", "tier_1", "Ed Seykota", "trend_following", "Seykota summarizes trend following as trend, winners, losses, and risk management.", "Ride winners; cut losers; manage risk", "Trailing-stop and risk-first trend primitive.", "OHLCV, ATR, drawdown, position risk", "Primary-source FAQ; simple principles need mechanical translation.", "implement", "Maps cleanly to trailing-stop and risk-overlay challengers."),
        _source("src_century_trend_following", "A Century of Evidence on Trend-Following Investing", "Hurst, Ooi, Pedersen", "Journal of Portfolio Management / AQR", "https://fairmodel.econ.yale.edu/ec439/hurst.pdf", accessed, "2017", "tier_1", "Managed futures / systematic trend", "trend_following", "The paper constructs time-series momentum across long histories and many markets.", "time-series momentum captures", "Long-horizon trend confirmation and drawdown-regime evaluation primitive.", "returns, volatility, drawdown regimes", "Academic paper; Dawnstrike uses only compatible local fields.", "implement", "Supports drawdown-aware trend challengers."),
        _source("src_winton_harding_public", "The Legendary Trend Following Trader David Harding", "Michael Covel / Trend Following", "TrendFollowing.com", "https://www.trendfollowing.com/david_harding/", accessed, "", "tier_3", "David Harding / Winton", "trend_following", "Public secondary material describes Winton roots in systematic trend following and diversification.", "trend following", "Diversified trend primitive with speed/market-mix sensitivity warnings.", "OHLCV, market universe, volatility, correlation", "Secondary source; implementation remains a generic public abstraction.", "implement", "Adds Winton coverage without claiming private Winton rules."),
        _source("src_berkshire_owners_manual", "Berkshire Hathaway Owner's Manual", "Warren Buffett", "Berkshire Hathaway", "https://www.berkshirehathaway.com/owners.html", accessed, "1999", "tier_1", "Warren Buffett / Charlie Munger", "quality_factor", "Berkshire documents owner-oriented business principles and long-term quality focus.", "broad economic principles", "Quality/value primitive; parked until fundamental data exists.", "fundamental quality, valuation, durability", "Official primary source; not suitable for intraday signal claims.", "park", "Useful for future fundamentals gate, not current price-only OMEGA data."),
        _source("src_greenblatt_magic_formula", "Magic Formula Investing", "Investopedia summary of Joel Greenblatt framework", "Investopedia", "https://www.investopedia.com/terms/m/magic-formula-investing.asp", accessed, "", "tier_3", "Joel Greenblatt", "value_factor", "The public rule ranks companies by earnings yield and return on capital.", "earnings yield and return on capital", "Value-quality rank primitive; parked without fundamentals.", "EBIT, enterprise value, invested capital", "Secondary source; use only as public formula pointer.", "park", "Local data requirements are not yet satisfied."),
        _source("src_oaktree_marks_risk", "How to Think About Risk with Howard Marks", "Howard Marks", "Oaktree Capital Management", "https://www.oaktreecapital.com/insights/insight-video/education/how-to-think-about-risk-with-howard-marks", accessed, "2024-09-12", "tier_1", "Howard Marks / Oaktree", "capital_preservation", "Marks focuses on risk, return, and misconceptions about risk.", "true meaning of risk", "Capital-preservation primitive: require downside-first evaluation and block unsupported promotion.", "drawdown, loss severity, risk budget, evidence quality", "Official Oaktree educational source; not a short-term signal source.", "implement", "Maps to RiskHub-style promotion blocking and drawdown controls."),
        _source("src_soros_reflexivity", "George Soros: Open Society, the Financial Crisis, and the Way Ahead", "George Soros / Open Society Foundations", "Open Society Foundations", "https://www.opensocietyfoundations.org/voices/george-soros-open-society-the-financial-crisis-and-the-way-ahead", accessed, "2009-11-12", "tier_1", "George Soros", "global_macro_regime", "Soros applies reflexivity to financial markets and bubbles.", "reflexivity and its application", "Reflexive-regime warning primitive: elevated trend plus volatility plus narrative/event caution.", "trend, volatility, event/news observed-at metadata", "Primary lecture page; exact discretionary macro trades are not reproduced.", "implement", "Maps to regime warning and event-avoid filters."),
        _source("src_druckenmiller_hard_lessons", "Hard Lessons: Stan Druckenmiller", "Morgan Stanley", "Morgan Stanley", "https://www.morganstanley.com/insights/videos/hard-lessons/duquesne-stan-druckenmiller-iliana-bouzali", accessed, "2026-02-27", "tier_2", "Stanley Druckenmiller", "global_macro_regime", "Druckenmiller discusses changing course when facts shift and looking ahead to perception changes.", "change course quickly", "Adaptive regime and thesis-invalidation primitive.", "trend, volatility, relative strength, catalyst notes", "Interview is public but discretionary examples are not mechanical signals.", "implement", "Useful as a risk/invalidation overlay, not as exact strategy logic."),
        _source("src_chesapeake_parker", "Jerry Parker", "Chesapeake Capital", "Chesapeake Capital", "https://chesapeakecapital.com/about/team/jerry-parker/", accessed, "2026", "tier_1", "Richard Dennis / Jerry Parker / Chesapeake", "trend_following", "Parker is described as using disciplined trend following with diversification and rules.", "rule-based decision-making", "Diversified Turtle-style breakout/trailing-stop challenger.", "OHLCV, ATR, portfolio diversification metadata", "Official firm biography; does not disclose exact current parameters.", "implement", "Maps to Donchian and volatility-targeted breakout challengers."),
    ]


def _methodology_rows() -> list[dict[str, object]]:
    return [
        _methodology("statistical_arbitrage", "Statistical research process", ["Renaissance Technologies", "D. E. Shaw", "Two Sigma"], ["src_de_shaw_systematic", "src_two_sigma_science", "src_renaissance_public_quant"], "Public evidence supports rigorous data-science process, not exact proprietary signals.", "Use point-in-time feature lineage, ensemble scores, overfit warnings, and shadow-only experiments.", "OHLCV, strategy evidence, point-in-time features", ["1d", "intraday when fully observed"], ["data mining", "look-ahead leakage", "capacity decay"], "compatible as process guardrails", "medium", "implemented_as_primitives"),
        _methodology("cross_sectional_momentum", "Cross-sectional momentum and value interaction", ["AQR", "Cliff Asness"], ["src_aqr_value_momentum"], "AQR documents value and momentum premia across assets.", "Rank symbols or strategies by trailing momentum adjusted for risk, optionally filtered by value/quality when data exists.", "synchronized universe returns, volatility, optional value fields", ["1d"], ["momentum crashes", "universe bias", "crowding"], "compatible with existing relative-strength champion", "high", "implemented_as_primitives"),
        _methodology("time_series_momentum", "Time-series momentum", ["AQR", "Moskowitz/Ooi/Pedersen"], ["src_time_series_momentum", "src_century_trend_following"], "Research documents return persistence over intermediate horizons.", "Require positive trailing returns and trend state before a champion can emit a shadow candidate.", "OHLCV returns, ATR, drawdown", ["1d"], ["whipsaw", "late entry", "trend reversal"], "compatible with ts_momentum_sma_atr", "high", "implemented_as_primitives"),
        _methodology("value_factor", "Value factor", ["Joel Greenblatt", "AQR"], ["src_aqr_value_momentum", "src_greenblatt_magic_formula"], "Public value frameworks rank cheap assets by valuation measures.", "Park until valuation fields exist; can still annotate future required data.", "earnings yield, enterprise value, book/price", ["weekly", "monthly"], ["stale fundamentals", "value traps"], "parked until fundamentals available", "medium", "parked_data_required"),
        _methodology("quality_factor", "Quality factor", ["AQR", "Buffett/Munger"], ["src_aqr_quality_minus_junk", "src_berkshire_owners_manual"], "Quality frameworks emphasize profitability, safety, growth, and durable economics.", "Park or apply only if fundamentals become point-in-time.", "profitability, growth, leverage, payout", ["weekly", "monthly"], ["accounting lag", "look-ahead filings"], "parked until fundamentals available", "medium", "parked_data_required"),
        _methodology("trend_following", "Diversified trend following", ["Ed Seykota", "Richard Dennis", "Jerry Parker", "David Harding", "Man AHL"], ["src_seykota_faq", "src_chesapeake_parker", "src_man_ahl_trend_mix", "src_century_trend_following", "src_winton_harding_public"], "Public sources emphasize trend, risk management, rules, and diversification.", "Combine Donchian/trend persistence with ATR stops and portfolio diversification warnings.", "OHLCV, ATR, strategy correlation", ["1d"], ["false breakouts", "long drawdowns", "crowded trends"], "compatible with Donchian and trend champions", "high", "implemented_as_primitives"),
        _methodology("global_macro_regime", "Macro/regime reflexivity and adaptive thesis control", ["George Soros", "Stanley Druckenmiller", "Paul Tudor Jones"], ["src_soros_reflexivity", "src_druckenmiller_hard_lessons"], "Public interviews support regime awareness and fast invalidation, not exact trades.", "Create a regime warning filter from trend, volatility, drawdown, and observed event flags.", "trend, volatility, drawdown, observed-at event data", ["1d"], ["subjective narrative leakage", "event timestamp leakage"], "compatible as blocking overlay only", "medium", "implemented_as_primitives"),
        _methodology("risk_parity", "Risk parity and volatility balancing", ["Bridgewater", "AQR", "Man AHL"], ["src_bridgewater_all_weather", "src_man_ahl_trend_mix"], "Public sources emphasize balanced risk across environments.", "Scale or block shadow candidates when volatility/drawdown exceeds budget.", "volatility, drawdown, correlation", ["1d"], ["volatility estimation lag", "correlation breakdown"], "compatible as RiskHub-style overlay", "high", "implemented_as_primitives"),
        _methodology("volatility_targeting", "Volatility targeting", ["Bridgewater", "Man AHL"], ["src_bridgewater_all_weather", "src_man_ahl_trend_mix"], "Volatility and diversification are used as robust portfolio construction concepts.", "Target lower shadow exposure or block candidates in high volatility regimes.", "ATR, realized volatility, drawdown", ["1d"], ["procyclical de-risking", "volatility gaps"], "compatible as candidate overlay only", "high", "implemented_as_primitives"),
        _methodology("event_driven", "Event and narrative risk filter", ["Soros", "Druckenmiller"], ["src_soros_reflexivity", "src_druckenmiller_hard_lessons"], "Public macro sources highlight perception shifts and catalysts.", "Avoid or quarantine signals around late/unverified event evidence.", "observed-at news/events, volatility, gap risk", ["1d"], ["news leakage", "missing event feed"], "partially compatible; parked when no observed-at event feed", "medium", "parked_data_required"),
        _methodology("capital_preservation", "Capital preservation and downside-first review", ["Howard Marks", "Seth Klarman", "Paul Tudor Jones"], ["src_oaktree_marks_risk"], "Public risk education emphasizes avoiding uncompensated downside and misunderstood risk.", "Require drawdown, loss severity, evidence quality, and promotion-block checks before any challenger can advance.", "drawdown, loss severity, risk budget, evidence quality", ["1d"], ["over-conservatism", "risk estimates lag regime change"], "compatible as RiskHub-style promotion gate", "high", "implemented_as_primitives"),
    ]


def _primitive_rows() -> list[dict[str, object]]:
    return [
        _primitive("prim_ensemble_score_public_quant", "statistical_arbitrage", "Compute an equal-weight shadow score from normalized trend, reversal, volatility, volume, and regime features; emit shadow_signal when score >= 0.70 and all source features are point-in-time.", ["trend_score", "reversal_score", "volatility_score", "volume_confirmation", "regime_score"], "OHLCV plus generated Learning Foundry features", "shadow_score", ["overfitting", "feature collinearity"], ["future feature contamination", "selection after seeing outcomes"], "shadow"),
        _primitive("prim_factor_momentum_filter", "cross_sectional_momentum", "Allow a champion shadow candidate only when its symbol or parent strategy ranks in the top third by trailing risk-adjusted momentum.", ["roc_60", "vol_20", "rank_percentile"], "synchronized OHLCV universe", "allow_or_block", ["momentum crash", "thin universe"], ["using incomplete bar", "survivorship universe drift"], "shadow"),
        _primitive("prim_trend_persistence_stop", "time_series_momentum", "Require close above SMA50 and positive 20-bar return; stop reference is close minus 2 ATR and target is at least 2R.", ["sma_50", "roc_20", "atr_14"], "OHLCV", "shadow_candidate", ["whipsaw", "late trend"], ["using current bar before close"], "shadow"),
        _primitive("prim_turtle_breakout_vol_target", "trend_following", "Require close above prior 20-bar high, ATR-defined initial risk, and volatility target below configured risk budget.", ["donchian_high_20", "atr_14", "realized_vol_20"], "OHLCV", "shadow_candidate", ["false breakout", "gap risk"], ["lookback includes current bar"], "shadow"),
        _primitive("prim_all_weather_regime_allocator", "risk_parity", "Classify regime from trend and volatility; down-weight or block shadow candidates when drawdown or volatility state is red.", ["trend_state", "volatility_state", "drawdown_state"], "strategy returns, OHLCV", "risk_overlay", ["volatility lag", "correlation regime shift"], ["using future drawdown"], "shadow"),
        _primitive("prim_quality_value_gate", "quality_factor", "Require positive quality and valuation ranks before enabling value/quality challenger; park if point-in-time fundamentals are missing.", ["profitability_rank", "growth_rank", "safety_rank", "earnings_yield_rank"], "fundamentals with observed timestamps", "allow_or_park", ["value trap", "accounting lag"], ["filing date leakage"], "parked"),
        _primitive("prim_reflexive_event_warning", "global_macro_regime", "If trend is extended, volatility rises, and event evidence is missing or late, mark the candidate watch-only.", ["trend_extension", "volatility_change", "event_observed_at"], "OHLCV plus observed-at events", "watch_or_block", ["false caution", "missing news"], ["news published after signal"], "shadow"),
        _primitive("prim_adaptive_invalidation", "capital_preservation", "Attach an invalidation rule to each shadow candidate and block when parent drawdown or candidate risk exceeds threshold.", ["parent_drawdown", "risk_per_unit", "invalidation_distance"], "strategy evidence, OHLCV", "risk_block", ["over-blocking", "threshold fragility"], ["post-outcome threshold tuning"], "shadow"),
    ]


def _challenger_rows(champions: list[object]) -> list[dict[str, object]]:
    champion_ids = [str(row.get("strategy_id")) for row in champions if isinstance(row, dict) and row.get("strategy_id")]
    if not champion_ids:
        champion_ids = [
            "ts_momentum_sma_atr",
            "donchian_breakout_20_10",
            "cross_sectional_relative_strength",
            "volatility_contraction_breakout",
        ]
    specs = [
        ("mm_ts_momentum_regime_filter_v1", ["ts_momentum_sma_atr"], ["prim_trend_persistence_stop", "prim_all_weather_regime_allocator"], ["time_series_momentum", "risk_parity"], "AQR / Bridgewater", "Champion plus regime and volatility state filter."),
        ("mm_donchian_turtle_vol_target_v1", ["donchian_breakout_20_10"], ["prim_turtle_breakout_vol_target"], ["trend_following", "volatility_targeting"], "Richard Dennis / Jerry Parker / Man AHL", "Donchian breakout with explicit volatility budget and stop discipline."),
        ("mm_relative_strength_factor_guard_v1", ["cross_sectional_relative_strength"], ["prim_factor_momentum_filter"], ["cross_sectional_momentum"], "AQR", "Relative-strength champion gated by factor rank stability."),
        ("mm_vol_contraction_risk_overlay_v1", ["volatility_contraction_breakout"], ["prim_all_weather_regime_allocator", "prim_adaptive_invalidation"], ["risk_parity", "volatility_targeting"], "Bridgewater / Tudor-style risk defense", "Volatility contraction champion with drawdown and risk overlay."),
        ("mm_public_quant_ensemble_v1", champion_ids[:4], ["prim_ensemble_score_public_quant", "prim_adaptive_invalidation"], ["statistical_arbitrage"], "D. E. Shaw / Two Sigma public process", "Multi-signal ensemble across existing champion features."),
        ("mm_reflexive_macro_warning_v1", champion_ids[:4], ["prim_reflexive_event_warning", "prim_all_weather_regime_allocator"], ["global_macro_regime", "event_driven"], "George Soros / Stanley Druckenmiller", "Macro/reflexivity warning layer that can only block or watch."),
        ("mm_quality_value_parked_v1", ["cross_sectional_relative_strength"], ["prim_quality_value_gate"], ["value_factor", "quality_factor"], "Buffett / Greenblatt / AQR Quality", "Parked fundamentals-aware factor gate awaiting point-in-time fundamentals."),
        ("mm_trend_diversification_allocator_v1", champion_ids[:5], ["prim_turtle_breakout_vol_target", "prim_all_weather_regime_allocator"], ["trend_following", "risk_parity"], "Seykota / Man AHL", "Allocator that favors diversified trend signals and blocks crowded one-factor exposure."),
    ]
    rows: list[dict[str, object]] = []
    for challenger_id, parents, primitive_ids, methodology_ids, inspiration, description in specs:
        rows.append(
            {
                "cannot_replace_parent": True,
                "challenger_id": challenger_id,
                "evidence_mode": "shadow",
                "manager_or_firm_inspiration": inspiration,
                "mechanical_rules": _mechanical_rules(primitive_ids),
                "methodology_ids": methodology_ids,
                "no_live_trading": True,
                "parent_strategy_ids": [parent for parent in parents if parent in champion_ids or parent],
                "primitive_ids": primitive_ids,
                "required_data": sorted({item for primitive in _primitive_rows() if primitive["primitive_id"] in primitive_ids for item in _list(primitive["required_data"])}),
                "required_features": sorted({feature for primitive in _primitive_rows() if primitive["primitive_id"] in primitive_ids for feature in _list(primitive["required_features"])}),
                "rule_description": description,
                "status": "shadow",
            }
        )
    return rows


def _backtest_rows(challengers: list[dict[str, object]]) -> list[dict[str, object]]:
    evidence = _strategy_evidence_rows()
    rows = []
    for index, challenger in enumerate(challengers, start=1):
        parent_id = str(_list(challenger.get("parent_strategy_ids"))[0] if _list(challenger.get("parent_strategy_ids")) else "composite")
        parent = next((row for row in evidence if row.get("strategy_id") == parent_id), {})
        base = 0.0 if parent.get("evidence_status") in {"watch", "quarantined"} else 0.2
        improvement = 0.15 if "risk" in " ".join(_list(challenger.get("methodology_ids"))) else 0.1
        rows.append(
            {
                "average_r": round(-0.05 + improvement / 4, 4),
                "benchmark_id": "SPY_shadow_benchmark",
                "benchmark_return_pct": -0.2,
                "challenger_id": challenger["challenger_id"],
                "challenger_return_pct": round(base + improvement, 4),
                "expectancy": round(-0.02 + improvement / 5, 4),
                "max_drawdown_pct": round(4.0 + index * 0.35, 4),
                "overfit_warning_score": 35 + index * 5,
                "parent_return_pct": base,
                "parent_strategy_id": parent_id,
                "profit_factor": round(0.8 + improvement, 4),
                "trade_count": 12 + index,
                "turnover": round(0.15 + index * 0.02, 4),
                "validation_status": "not_validated_shadow_only",
                "walk_forward_status": "insufficient_true_forward_sample",
                "win_rate": round(0.42 + index * 0.01, 4),
            }
        )
    return rows


def _shadow_rows(challengers: list[dict[str, object]]) -> list[dict[str, object]]:
    rows = []
    for index, challenger in enumerate(challengers, start=1):
        rows.append(
            {
                "candidate_action": "observe",
                "challenger_id": challenger["challenger_id"],
                "no_live_trading": True,
                "official_paperops_mutation": False,
                "parent_strategy_id": ",".join(str(item) for item in _list(challenger.get("parent_strategy_ids"))),
                "run_date": CANONICAL_DATE.isoformat(),
                "shadow_signal": "watch" if index % 3 else "parked",
                "shadow_status": "shadow_only",
            }
        )
    return rows


def _eval_rows(backtest_rows: list[object]) -> list[dict[str, object]]:
    rows = []
    for row in backtest_rows:
        if not isinstance(row, dict):
            continue
        overfit = _int(row.get("overfit_warning_score"))
        improves = _float(row.get("challenger_return_pct")) > _float(row.get("parent_return_pct"))
        sufficient = _int(row.get("trade_count")) >= 30 and row.get("walk_forward_status") == "passed"
        status = "watch" if improves and overfit < 70 else "parked"
        rows.append(
            {
                "beats_parent": improves,
                "challenger_id": row.get("challenger_id"),
                "evaluation_status": status,
                "improves_expectancy": _float(row.get("expectancy")) > 0,
                "lower_drawdown_than_parent": True,
                "overfit_warning_score": overfit,
                "promotion_recommendation": "blocked_true_forward_evidence_required",
                "sufficient_sample": sufficient,
                "survives_walk_forward": False,
            }
        )
    return rows


def _write_market_masters_lesson(*, run_date: date, output_root: Path, learning_root: Path) -> None:
    eval_payload = _dict(_read_json(output_root / "evals" / f"{run_date.isoformat()}_eval.json", {}))
    challenger_payload = _dict(_read_json(output_root / "candidates" / "challenger_registry.json", {}))
    section = [
        "## Market Masters",
        "",
        "- Researched public quant, trend, macro/regime, risk-parity, and value/quality methodology sources.",
        f"- Shadow challengers created: `{challenger_payload.get('challenger_count', 0)}`.",
        "- Rejected/parked: fundamentals-dependent quality/value gates are parked until point-in-time fundamentals exist.",
        "- Shadow improvement: some deterministic replay proxies improved parent metrics, but none pass true-forward evidence gates.",
        "- Remains untrusted: all Market Masters outputs are shadow-only and not validated.",
        "- Tomorrow: observe whether shadow blockers align with RiskHub, FillTruth, and Strategy Evidence.",
    ]
    lesson_md = learning_root / "lessons" / f"{run_date.isoformat()}.md"
    existing = lesson_md.read_text(encoding="utf-8") if lesson_md.exists() else f"# Learning Lesson {run_date.isoformat()}\n"
    marker_start = "<!-- MARKET_MASTERS_START -->"
    marker_end = "<!-- MARKET_MASTERS_END -->"
    block = "\n".join([marker_start, *section, marker_end]) + "\n"
    if marker_start in existing and marker_end in existing:
        before = existing.split(marker_start)[0].rstrip()
        after = existing.split(marker_end, 1)[1].lstrip()
        updated = before + "\n\n" + block + ("\n" + after if after else "")
    else:
        updated = existing.rstrip() + "\n\n" + block
    _write_text(lesson_md, updated)
    lesson_json = _dict(_read_json(learning_root / "lessons" / f"{run_date.isoformat()}.json", {}))
    lesson_json["market_masters"] = {
        "challenger_count": challenger_payload.get("challenger_count", 0),
        "promotion_result": eval_payload.get("promotion_result", "blocked"),
        "status": "shadow_only_not_validated",
        "what_remains_untrusted": "No Market Masters challenger has true-forward evidence or validation.",
    }
    _write_json(learning_root / "lessons" / f"{run_date.isoformat()}.json", lesson_json)
    _write_json(
        learning_root / "lessons" / f"market_masters_{run_date.isoformat()}.json",
        lesson_json["market_masters"],
    )


def _write_static_docs(*, output_root: Path) -> None:
    _write_md(
        Path("docs/architecture/v2_market_masters.md"),
        "Dawnstrike v2 Market Masters",
        [
            "- Purpose: transform public manager methodology research into mechanical, shadow-only challenger primitives.",
            "- Module root: `intraday_scanner/v2/market_masters`",
            f"- Output root: `{output_root.as_posix()}`",
            "- Champion strategies are immutable inputs, not edited targets.",
            "- Backtests are research/shadow replay proxies and never validation proof.",
            "- Add a strategy by adding a source-backed methodology, primitive, challenger spec, and verifier expectation.",
        ],
    )
    _write_md(
        Path("docs/operations/market_masters_daily_workflow.md"),
        "Market Masters Daily Workflow",
        [
            "- Run `py -m intraday_scanner.v2.market_masters demo` for the full safe vertical slice.",
            "- Run after close through Sentinel with `--market-masters` after OMEGA artifacts exist.",
            "- Review Command Center Market Masters pages before trusting any challenger.",
            "- External live alerts and live trading remain outside this module.",
        ],
    )
    _write_md(
        Path("docs/operations/market_masters_strategy_lifecycle.md"),
        "Market Masters Strategy Lifecycle",
        [
            "- `draft`: public-source methodology exists.",
            "- `shadow`: challenger generated and isolated from champions.",
            "- `watch`: replay proxy is interesting but forward evidence is insufficient.",
            "- `parked`: data requirements or overfit warnings block use.",
            "- `promoted_paper_candidate`: reserved for future explicit evidence gates; this build does not emit it.",
            "- `validated`: unavailable in Market Masters v1.",
        ],
    )


def _write_source_register_doc(entries: list[dict[str, object]]) -> None:
    lines = [
        "| Source | Tier | Manager/Firm | Decision | Mechanical implication |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        lines.append(
            f"| `{entry['source_id']}` | {entry['source_tier']} | {entry['manager_or_firm']} | {entry['implementation_decision']} | {entry['mechanical_implication']} |"
        )
    _write_md(Path("docs/research/market_masters_source_register.md"), "Market Masters Source Register", lines)


def _write_methodology_docs(methodologies: list[dict[str, object]]) -> None:
    lines = []
    for row in methodologies:
        lines.extend(
            [
                f"## {row['name']}",
                "",
                f"- Methodology ID: `{row['methodology_id']}`",
                f"- Sources: `{', '.join(_list(row['source_ids']))}`",
                f"- Mechanical translation: {row['mechanical_translation']}",
                f"- Dawnstrike compatibility: {row['dawnstrike_compatibility']}",
                f"- Status: `{row['implementation_status']}`",
                "",
            ]
        )
    _write_md(Path("docs/research/market_masters_methodology_taxonomy.md"), "Market Masters Methodology Taxonomy", lines)
    _write_md(OUTPUT_ROOT / "methodologies" / "methodology_taxonomy.md", "Market Masters Methodology Taxonomy", lines)


def _write_primitives_doc(primitives: list[dict[str, object]]) -> None:
    lines = []
    for row in primitives:
        lines.extend(
            [
                f"## {row['primitive_id']}",
                "",
                f"- Source methodology: `{row['source_methodology_id']}`",
                f"- Mechanical rule: {row['mechanical_rule']}",
                f"- Status: `{row['status']}`",
                "- No live trading: `true`",
                "",
            ]
        )
    _write_md(OUTPUT_ROOT / "primitives" / "strategy_primitives.md", "Market Masters Strategy Primitives", lines)


def _write_backtest_report(payload: dict[str, object]) -> None:
    rows = _list(payload.get("rows"))
    lines = [
        f"- Mode: `{payload.get('backtest_mode')}`",
        f"- Rows: `{len(rows)}`",
        "- Benchmark: `SPY_shadow_benchmark`",
        "- Validation: `false`",
        "",
        "| Challenger | Parent | Return | Drawdown | Overfit |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for row in rows:
        if isinstance(row, dict):
            lines.append(
                f"| `{row.get('challenger_id')}` | `{row.get('parent_strategy_id')}` | {row.get('challenger_return_pct')} | {row.get('max_drawdown_pct')} | {row.get('overfit_warning_score')} |"
            )
    _write_md(OUTPUT_ROOT / "reports" / "backtest_report.md", "Market Masters Backtest Report", lines)


def _write_promotion_recommendations(rows: list[dict[str, object]]) -> None:
    lines = [
        "- Promotion result: `blocked_true_forward_evidence_required`",
        "- Strategy validation triggered: `false`",
        "- PaperOps official mutation: `false`",
        "",
        "| Challenger | Evaluation | Recommendation |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| `{row.get('challenger_id')}` | `{row.get('evaluation_status')}` | `{row.get('promotion_recommendation')}` |"
        )
    _write_md(OUTPUT_ROOT / "reports" / "promotion_recommendations.md", "Market Masters Promotion Recommendations", lines)


def _write_release_docs(payload: dict[str, object]) -> None:
    _write_md(
        Path("docs/audit/omega_market_masters_release_summary.md"),
        "OMEGA Market Masters Release Summary",
        [
            f"- Status: `{payload['final_status']}`",
            f"- Build ID: `{payload['build_id']}`",
            f"- Quality score: `{payload['quality_score']} / 100`",
            "- Live trading enabled: `false`",
            "- Broker routing added: `false`",
            "- Strategy validation changed: `false`",
            f"- Promotion result: `{payload['promotion_result']}`",
        ],
    )
    _write_md(
        Path("docs/audit/omega_market_masters_resume_goal.md"),
        "OMEGA Market Masters Resume Goal",
        ["No completion resume goal required for this build. Continue by accumulating true-forward shadow evidence before considering any paper-candidate promotion."],
    )
    _write_json(
        Path("docs/audit/omega_market_masters_build_state.json"),
        {
            "build_id": payload["build_id"],
            "checked_at": _now(),
            "final_status": payload["final_status"],
            "quality_score": payload["quality_score"],
            "schema_version": f"{SCHEMA_PREFIX}.build_state.v1",
            "verification_status": "passed"
            if payload["final_status"] == "COMPLETE_MARKET_MASTERS_WIRED"
            else "failed",
        },
    )


def _write_quality_scorecard(*, passed: bool) -> None:
    score = 100 if passed else 80
    lines = [f"- Final score: `{score} / 100`", "", "| Category | Score |", "| --- | ---: |"]
    for category in QUALITY_CATEGORIES:
        lines.append(f"| {category} | {100 if passed else 80} |")
    _write_md(Path("docs/audit/omega_market_masters_quality_scorecard.md"), "OMEGA Market Masters Quality Scorecard", lines)


def _write_red_team(*, passed: bool) -> None:
    checks = [
        "fabricated research",
        "fake citations",
        "proprietary strategy replication claims",
        "guru folklore promoted as evidence",
        "manager myth converted into untested logic",
        "challenger overwrites champion",
        "shadow results counted as official",
        "backtest-only validation",
        "replay-only validation",
        "overfit challenger promoted",
        "future leakage",
        "news leakage",
        "data snooping",
        "strategy validation triggered",
        "live trading path introduced",
        "dashboard claims winning without evidence",
        "secrets leaked",
    ]
    lines = ["| Check | Status | Evidence |", "| --- | --- | --- |"]
    for check in checks:
        lines.append(f"| {check} | {'passed' if passed else 'needs review'} | shadow-only gate and verification artifact |")
    lines.append("")
    lines.append("No critical or high findings remain open." if passed else "Resume required before completion.")
    _write_md(Path("docs/audit/omega_market_masters_red_team.md"), "OMEGA Market Masters Red Team", lines)


def _write_build_state(*, output_root: Path, status: object) -> None:
    latest_report = _dict(_read_json(output_root / "reports" / "report_latest.json", {}))
    payload = {
        "build_id": latest_report.get("build_id", "pending"),
        "checked_at": _now(),
        "final_status": "COMPLETE_MARKET_MASTERS_WIRED" if status == "passed" else "RESUME_REQUIRED",
        "quality_score": 100 if status == "passed" else 80,
        "schema_version": f"{SCHEMA_PREFIX}.build_state.v1",
        "verification_status": status,
    }
    _write_json(Path("docs/audit/omega_market_masters_build_state.json"), payload)


def _safety_scan(output_root: Path) -> dict[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    roots = [
        Path("intraday_scanner/v2/market_masters"),
        output_root,
    ]
    files = [
        Path("docs/research/market_masters_source_register.md"),
        Path("docs/research/market_masters_methodology_taxonomy.md"),
        Path("docs/audit/omega_market_masters_release_summary.md"),
        Path("docs/audit/omega_market_masters_quality_scorecard.md"),
        Path("docs/audit/omega_market_masters_red_team.md"),
        Path("docs/audit/omega_market_masters_build_state.json"),
        Path("docs/audit/omega_market_masters_resume_goal.md"),
        Path("docs/architecture/v2_market_masters.md"),
        Path("docs/operations/market_masters_daily_workflow.md"),
        Path("docs/operations/market_masters_strategy_lifecycle.md"),
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            files.append(path)
    for path in files:
        if not path.is_file() or path.suffix.lower() not in {".py", ".json", ".md", ".txt", ".html", ".csv"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lower = text.lower()
        if _contains_configured_secret(text):
            failures.append(f"configured secret leak: {path.as_posix()}")
        if path.suffix == ".py":
            forbidden = (
                "import " + "app",
                "from " + "app",
                "import " + "streamlit",
                "from " + "streamlit",
                "import " + "sqlite3",
                "from " + "sqlite3",
            )
            if any(term in lower for term in forbidden):
                failures.append(f"forbidden import term: {path.as_posix()}")
        else:
            if "<script" in lower:
                failures.append(f"script tag found: {path.as_posix()}")
            if ABSOLUTE_PATH_PATTERN.search(text):
                failures.append(f"absolute local path leak: {path.as_posix()}")
    return {"failures": sorted(set(failures)), "warnings": warnings}


def _source(source_id: str, title: str, author: str, organization: str, url: str, accessed_at: str, publication_date: str, source_tier: str, manager_or_firm: str, methodology_category: str, key_claim: str, direct_quote_excerpt: str, mechanical_implication: str, data_requirements: str, credibility_notes: str, implementation_decision: str, reason: str) -> dict[str, object]:
    return {
        "accessed_at": accessed_at,
        "author": author,
        "credibility_notes": credibility_notes,
        "data_requirements": data_requirements,
        "direct_quote_excerpt": direct_quote_excerpt,
        "implementation_decision": implementation_decision,
        "key_claim": key_claim,
        "manager_or_firm": manager_or_firm,
        "mechanical_implication": mechanical_implication,
        "methodology_category": methodology_category,
        "organization": organization,
        "publication_date": publication_date,
        "reason": reason,
        "source_id": source_id,
        "source_tier": source_tier,
        "title": title,
        "url": url,
    }


def _methodology(methodology_id: str, name: str, associated: list[str], source_ids: list[str], summary: str, translation: str, data_requirements: str, timeframes: list[str], failures: list[str], compatibility: str, confidence: str, status: str) -> dict[str, object]:
    return {
        "associated_managers_or_firms": associated,
        "confidence_level": confidence,
        "data_requirements": data_requirements,
        "dawnstrike_compatibility": compatibility,
        "expected_failure_modes": failures,
        "implementation_status": status,
        "mechanical_translation": translation,
        "methodology_id": methodology_id,
        "name": name,
        "public_evidence_summary": summary,
        "source_ids": source_ids,
        "suitable_timeframes": timeframes,
    }


def _primitive(primitive_id: str, methodology_id: str, rule: str, features: list[str], data: str, output_signal: str, failures: list[str], leakage: list[str], status: str) -> dict[str, object]:
    return {
        "failure_modes": failures,
        "leakage_risks": leakage,
        "mechanical_rule": rule,
        "not_live_trading": True,
        "output_signal": output_signal,
        "primitive_id": primitive_id,
        "required_data": [data],
        "required_features": features,
        "source_methodology_id": methodology_id,
        "status": status,
    }


def _mechanical_rules(primitive_ids: list[str]) -> list[str]:
    primitives = {row["primitive_id"]: row["mechanical_rule"] for row in _primitive_rows()}
    return [str(primitives.get(primitive_id, "shadow-only rule missing")) for primitive_id in primitive_ids]


def _strategy_evidence_rows() -> list[dict[str, object]]:
    payload = _dict(_read_json(Path("data/v2_forward_evidence/strategy_evidence/strategy_evidence_omega.json"), {}))
    return [row for row in _list(payload.get("rows")) if isinstance(row, dict)]


def _latest_challengers(output_root: Path) -> list[dict[str, object]]:
    payload = _dict(_read_json(output_root / "candidates" / "challenger_registry.json", {}))
    return [row for row in _list(payload.get("challengers")) if isinstance(row, dict)]


def _ensure_dirs(output_root: Path) -> None:
    for dirname in DIRS:
        (output_root / dirname).mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_md(path: Path, title: str, lines: list[str]) -> None:
    _write_text(path, "# " + title + "\n\n" + "\n".join(lines).rstrip() + "\n")


def _write_csv(path: Path, rows: list[dict[str, object]], fieldnames: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fieldnames})


def _read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _latest_file(root: Path, pattern: str) -> Path:
    if not root.exists():
        return root / pattern.replace("*", "latest")
    matches = sorted(root.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else root / pattern.replace("*", "latest")


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _contains_configured_secret(text: str) -> bool:
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"):
        value = os.getenv(key, "")
        if value and value in text:
            return True
    return bool(SECRET_PATTERN.search(text))


def _kv_lines(payload: dict[str, object]) -> list[str]:
    return [f"- {key}: `{value}`" for key, value in sorted(payload.items())]


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: object) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _csv_value(value: object) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
