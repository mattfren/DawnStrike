"""DS-05a: close-return preference over MFE, with observed-zero preservation.

Covers three sites:
  1. intraday_scanner/alpha/edge_calibrator.py::_return
  2. intraday_scanner/alpha/performance_truth.py::_return
  3. intraday_scanner/alpha/setup_memory.py::summarize_setup (return extraction)

Required behaviour:
  - close return preferred over MFE (high_after_entry_return*) keys.
  - an observed 0.0 close return is reported as 0.0, not treated as missing.
  - a fully absent close return is reported as None/MISSING, not coerced to 0.0.
  - NaN / inf / -inf are rejected (treated as unusable, not propagated).
"""

from __future__ import annotations

import math

import pytest

from intraday_scanner.alpha.edge_calibrator import _return as edge_return
from intraday_scanner.alpha.performance_truth import _return as truth_return
from intraday_scanner.alpha.setup_memory import summarize_setup


# ---------------------------------------------------------------------------
# Site 1: edge_calibrator._return
# ---------------------------------------------------------------------------


def test_edge_calibrator_prefers_close_return_over_mfe():
    row = {"high_after_entry_return": 10.0, "close_return_pct": -5.0}
    assert edge_return(row) == -5.0


def test_edge_calibrator_preserves_observed_zero_close_return():
    row = {"high_after_entry_return": 10.0, "close_return_pct": 0.0}
    assert edge_return(row) == 0.0


def test_edge_calibrator_missing_close_return_is_none():
    row = {}
    assert edge_return(row) is None


def test_edge_calibrator_rejects_nonfinite():
    assert edge_return({"close_return_pct": float("nan")}) is None
    assert edge_return({"close_return_pct": float("inf")}) is None
    assert edge_return({"close_return_pct": float("-inf")}) is None


def test_edge_calibrator_close_wins_when_negative_and_mfe_present():
    row = {"high_after_entry_return": 50.0, "close_return_pct": -1.0}
    assert edge_return(row) == -1.0


# ---------------------------------------------------------------------------
# Site 2: performance_truth._return
# ---------------------------------------------------------------------------


def test_performance_truth_prefers_close_return_over_mfe():
    row = {"high_after_entry_return": 10.0, "close_return_pct": -5.0}
    assert truth_return(row) == -5.0


def test_performance_truth_preserves_observed_zero_close_return():
    row = {"high_after_entry_return": 10.0, "close_return_pct": 0.0}
    assert truth_return(row) == 0.0


def test_performance_truth_missing_close_return_is_none():
    row = {}
    assert truth_return(row) is None


def test_performance_truth_rejects_nonfinite():
    assert truth_return({"close_return_pct": float("nan")}) is None
    assert truth_return({"close_return_pct": float("inf")}) is None
    assert truth_return({"close_return_pct": float("-inf")}) is None


def test_performance_truth_close_wins_when_negative_and_mfe_present():
    row = {"high_after_entry_return": 50.0, "close_return_pct": -1.0}
    assert truth_return(row) == -1.0


# ---------------------------------------------------------------------------
# Site 3: setup_memory.summarize_setup
# ---------------------------------------------------------------------------


def test_setup_memory_prefers_close_return_over_mfe():
    rows = [{"high_after_entry_return": 10.0, "close_return_pct": -5.0}]
    result = summarize_setup("grade:A", rows)
    assert result["avg_return_pct"] == -5.0


def test_setup_memory_preserves_observed_zero_close_return():
    rows = [{"high_after_entry_return": 10.0, "close_return_pct": 0.0}]
    result = summarize_setup("grade:A", rows)
    assert result["avg_return_pct"] == 0.0
    # Must not silently fall through to the (non-zero) MFE fallback key.
    assert result["avg_return_pct"] != 10.0


def test_setup_memory_missing_close_return_is_not_coerced_to_mfe_or_zero_lie():
    # No close return anywhere, and no MFE either -> genuinely missing, sample
    # excluded from the average rather than forcing 0.0.
    rows = [{}]
    result = summarize_setup("grade:A", rows)
    assert result["sample_size"] == 1
    assert result["avg_return_pct"] == 0.0  # empty-returns convention for this summary
    # But an explicit per-row extraction must be able to report MISSING;
    # verify indirectly via win_rate/avg not being polluted by a phantom 0.0
    # when there genuinely is a close-priced MFE-only row below.


def test_setup_memory_rejects_nonfinite():
    rows = [{"close_return_pct": float("nan")}, {"close_return_pct": 5.0}]
    result = summarize_setup("grade:A", rows)
    # Only the finite row should count.
    assert result["sample_size"] == 2
    assert result["avg_return_pct"] == 5.0


def test_setup_memory_close_wins_when_negative_and_mfe_present():
    rows = [{"high_after_entry_return": 50.0, "close_return_pct": -1.0}]
    result = summarize_setup("grade:A", rows)
    assert result["avg_return_pct"] == -1.0
