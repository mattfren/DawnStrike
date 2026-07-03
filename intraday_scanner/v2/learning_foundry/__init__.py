"""Dawnstrike v2 Autonomous Learning Foundry."""

from intraday_scanner.v2.learning_foundry.core import (
    backtest_candidates,
    build_features,
    build_labels,
    build_regimes,
    daily_learn,
    demo,
    evaluate,
    generate_candidates,
    ingest_news,
    init,
    promote_review,
    report,
    shadow_run,
    train,
    verify,
    write_lesson,
)

__all__ = [
    "backtest_candidates",
    "build_features",
    "build_labels",
    "build_regimes",
    "daily_learn",
    "demo",
    "evaluate",
    "generate_candidates",
    "init",
    "ingest_news",
    "promote_review",
    "report",
    "shadow_run",
    "train",
    "verify",
    "write_lesson",
]
