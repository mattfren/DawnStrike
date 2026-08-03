from __future__ import annotations

from intraday_scanner.alpha.v6.drift import build_drift_report


def test_v6_drift_quarantines_missing_or_materially_shifted_evidence() -> None:
    missing = build_drift_report(baseline_rows=[], current_rows=[])
    shifted = build_drift_report(
        baseline_rows=[{"setup_key": "A", "regime_key": "R"}] * 20,
        current_rows=[{"setup_key": "B", "regime_key": "R"}] * 20,
    )

    assert missing["auto_quarantine"] is True
    assert shifted["status"] == "QUARANTINE_DRIFT"


def test_v6_drift_identity_is_stable_for_identical_evidence() -> None:
    baseline = [{"setup_key": "breakout", "regime_key": "BULL"}]
    current = [{"setup_key": "mean_reversion", "regime_key": "BEAR"}]

    first = build_drift_report(baseline_rows=baseline, current_rows=current)
    second = build_drift_report(baseline_rows=baseline, current_rows=current)

    assert first["drift_report_id"] == second["drift_report_id"]
