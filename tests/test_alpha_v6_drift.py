from __future__ import annotations

from intraday_scanner.alpha.v6.contracts import canonical_hash
from intraday_scanner.alpha.v6.drift import build_drift_report


def test_v6_drift_quarantines_missing_or_materially_shifted_evidence() -> None:
    missing = build_drift_report(baseline_rows=[], current_rows=[])
    baseline = [
        {
            "observation_id": f"baseline-{index}",
            "market_date": f"2026-01-{index // 4 + 1:02d}",
            "setup_key": "A",
            "regime_key": "R",
        }
        for index in range(20)
    ]
    current = [
        {
            "observation_id": f"current-{index}",
            "market_date": f"2026-02-{index // 4 + 1:02d}",
            "setup_key": "B",
            "regime_key": "R",
        }
        for index in range(20)
    ]
    reference_window = {
        "start": "2026-01-01",
        "end": "2026-01-05",
        "market_dates": [f"2026-01-{index:02d}" for index in range(1, 6)],
    }
    recent_window = {
        "start": "2026-02-01",
        "end": "2026-02-05",
        "market_dates": [f"2026-02-{index:02d}" for index in range(1, 6)],
    }
    config = {"dimensions": ["setup", "regime"]}
    source = {"provider": "fixture", "snapshot": "drift-v6"}
    shifted = build_drift_report(
        baseline_rows=baseline,
        current_rows=current,
        reference_window=reference_window,
        recent_window=recent_window,
        config=config,
        source=source,
        config_hash_sha256=canonical_hash(config),
        source_hash_sha256=canonical_hash(source),
        window_hash_sha256=canonical_hash({"reference": reference_window, "recent": recent_window}),
        input_hash_sha256=canonical_hash(
            {
                "reference": sorted(baseline, key=canonical_hash),
                "recent": sorted(current, key=canonical_hash),
            }
        ),
        code_sha="a" * 40,
    )

    assert missing["auto_quarantine"] is True
    assert shifted["status"] == "QUARANTINE_DRIFT"


def test_v6_drift_identity_is_stable_for_identical_evidence() -> None:
    baseline = [{"setup_key": "breakout", "regime_key": "BULL"}]
    current = [{"setup_key": "mean_reversion", "regime_key": "BEAR"}]

    first = build_drift_report(baseline_rows=baseline, current_rows=current)
    second = build_drift_report(baseline_rows=baseline, current_rows=current)

    assert first["drift_report_id"] == second["drift_report_id"]
