"""Dawnstrike point-in-time mover research and paper-observation lab."""

from .calendar_report import (
    render_strategy_calendar_report,
    write_strategy_calendar_report,
)
from .candidate_runtime import run_candidate_study
from .candidate_study import (
    CandidateSplitAssignment,
    CandidateStudyAssumptions,
    CandidateUniverseDenominator,
    study_all_candidates,
)
from .core import (
    DEFAULT_OUTPUT_ROOT,
    MoverLabPaths,
    analyze,
    build_snapshots_from_bars,
    init,
    paper_scan,
    reconcile_paper_signals,
    verify,
)

__all__ = [
    "DEFAULT_OUTPUT_ROOT",
    "CandidateSplitAssignment",
    "CandidateStudyAssumptions",
    "CandidateUniverseDenominator",
    "MoverLabPaths",
    "analyze",
    "build_snapshots_from_bars",
    "init",
    "paper_scan",
    "reconcile_paper_signals",
    "render_strategy_calendar_report",
    "run_candidate_study",
    "study_all_candidates",
    "verify",
    "write_strategy_calendar_report",
]
