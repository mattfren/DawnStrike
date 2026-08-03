from __future__ import annotations

from intraday_scanner.alpha.v6.calibration import calibration_report, interval_coverage


def test_v6_calibration_and_interval_coverage_require_real_labels() -> None:
    report = calibration_report(
        [
            {"activation_probability": 0.8, "activation_label": 1},
            {"activation_probability": 0.2, "activation_label": 0},
        ]
    )
    intervals = interval_coverage(
        [
            {
                "interval_lower_pct": -1.0,
                "interval_upper_pct": 2.0,
                "realized_return_pct": 1.0,
            }
        ]
    )

    assert report["status"] == "EVALUABLE"
    assert report["brier_score"] is not None
    assert intervals["coverage_pct"] == 100.0
    assert calibration_report([])["brier_score"] is None
