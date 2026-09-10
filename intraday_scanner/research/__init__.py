"""Bounded, research-only R5/R7 protocol helpers."""

from .r5_r7_protocol import (
    admit_d022_trade,
    bootstrap_paired_session_returns,
    compare_v5_baseline_challenger,
    build_income_illustration,
    build_observer_controller,
    build_research_protocol,
    evaluate_confirmation_summary,
    evaluate_controller_update,
    evaluate_gap_orb15_signal,
    run_v5_admission,
    run_r7_weekly_controller,
    replay_account_twr,
    simulate_causal_fill_lifecycle,
)

__all__ = [
    "build_income_illustration",
    "admit_d022_trade",
    "bootstrap_paired_session_returns",
    "compare_v5_baseline_challenger",
    "build_observer_controller",
    "build_research_protocol",
    "evaluate_confirmation_summary",
    "evaluate_controller_update",
    "evaluate_gap_orb15_signal",
    "run_v5_admission",
    "run_r7_weekly_controller",
    "replay_account_twr",
    "simulate_causal_fill_lifecycle",
]
