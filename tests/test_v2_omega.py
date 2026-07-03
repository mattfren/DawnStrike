from __future__ import annotations

from pathlib import Path

from intraday_scanner.v2.omega.core import _score_omega


def test_omega_score_contract_reaches_target_without_exceeding_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("scripts").mkdir()
    Path("scripts/run_omega_daily.ps1").write_text("# fixture\n", encoding="utf-8")
    Path("tests").mkdir()
    Path("tests/test_v2_forward_autopilot.py").write_text("# fixture\n", encoding="utf-8")
    Path("docs/audit").mkdir(parents=True)
    Path("docs/audit/forward_autopilot_red_team.md").write_text("# fixture\n", encoding="utf-8")

    quality = _score_omega(
        frozen_payload={
            "completed_bar_proof": True,
            "decision_cards": [{"symbol": "TST"}],
            "pick_set_hash": "hash",
        },
        integrity={"status": "passed"},
        command_center_qa={"status": "passed"},
        datatruth={"snapshot_id": "snapshot-20260629", "skipped_incomplete_bars": 0},
        daily_report={
            "frozen_pick_hash": "hash",
            "paper_ops": {"status": "passed"},
            "riskhub_status": "passed",
        },
        strategy_evidence={"rows": [{"strategy_id": "test_strategy"}]},
        shadow={"evidence_mode": "shadow_forward_replay"},
        calendar={"rows": 1},
    )

    assert quality["score"] == 98
    assert quality["target"] == 98
    assert quality["status"] == "target_met"
    assert quality["blockers"] == []
