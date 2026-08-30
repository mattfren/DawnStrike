"""Hostile evidence for the governed Morning acquisition boundary."""

import ast
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from intraday_scanner.services import luna_core_universe_service as core

OBSERVED_AT = datetime(2026, 1, 5, 13, 5, tzinfo=timezone.utc)


class _DelayedBatchProvider:
    """Provider probe that exposes accidental overlap and completion reordering."""

    def __init__(self, delays: dict[str, float]) -> None:
        self.delays = delays
        self.calls: list[tuple[str, ...]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def validate_credentials(self) -> None:
        return None

    def get_premarket_snapshot(self, symbols, config):
        batch = tuple(symbols)
        with self._lock:
            self.calls.append(batch)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delays.get(batch[0], 0.0))
            return [
                {
                    "ticker": symbol,
                    "source": "alpaca_iex",
                    "source_timestamp": "2026-01-05T13:04:30+00:00",
                    "premarket_price": 10,
                    "premarket_volume": 100,
                    "dollar_volume": 1000,
                    "previous_close": 9,
                    "premarket_high": 10,
                    "premarket_low": 9,
                }
                for symbol in symbols
            ]
        finally:
            with self._lock:
                self.active -= 1


def _run_discovery(delays: dict[str, float]) -> tuple[dict, _DelayedBatchProvider]:
    provider = _DelayedBatchProvider(delays)
    result = core.discover_core_universe_rows(
        {
            "status": "READY",
            "content_hash_sha256": "c" * 64,
            "members": [
                {"symbol": "ALFA", "index_memberships": ["S&P 500"]},
                {"symbol": "BRAV", "index_memberships": ["S&P 500"]},
                {"symbol": "CHAR", "index_memberships": ["Nasdaq-100"]},
                {"symbol": "DELT", "index_memberships": ["Nasdaq-100"]},
            ],
        },
        config=SimpleNamespace(premarket_enrichment_max_age_seconds=600),
        provider=provider,
        observed_at=OBSERVED_AT,
        batch_size=2,
        minimum_fresh_rows=1,
    )
    return result, provider


def test_core_batches_are_serial_and_hash_stable_under_hostile_delays() -> None:
    first, first_provider = _run_discovery({"ALFA": 0.03, "CHAR": 0.0})
    second, second_provider = _run_discovery({"ALFA": 0.0, "CHAR": 0.03})

    expected_calls = [("ALFA", "BRAV"), ("CHAR", "DELT")]
    assert first_provider.calls == expected_calls
    assert second_provider.calls == expected_calls
    assert first_provider.max_active == second_provider.max_active == 1
    assert first["coverage_receipt_hashes"] == second["coverage_receipt_hashes"]
    assert (
        first["discovery_coverage_receipt"]["coverage_receipt_hash_sha256"]
        == second["discovery_coverage_receipt"]["coverage_receipt_hash_sha256"]
    )
    assert [row["ticker"] for row in first["rows"]] == [
        row["ticker"] for row in second["rows"]
    ]


def test_alpha_cycle_keeps_shared_mover_core_boundary_serial() -> None:
    source_path = Path("intraday_scanner/services/alpha_cycle_service.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    alpha_cycle = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "alpha_cycle"
    )
    calls = []
    for node in ast.walk(alpha_cycle):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
        if name in {"web_auto_collect", "discover_core_universe_rows"}:
            calls.append((node.lineno, name))

    mover_lines = [line for line, name in calls if name == "web_auto_collect"]
    core_lines = [line for line, name in calls if name == "discover_core_universe_rows"]
    assert mover_lines and core_lines
    assert any(line > max(mover_lines) for line in core_lines)
    assert all(
        not (
            isinstance(node, ast.Name)
            and node.id == "ThreadPoolExecutor"
            or isinstance(node, ast.Attribute)
            and node.attr == "ThreadPoolExecutor"
        )
        for node in ast.walk(alpha_cycle)
    )

    script = Path("scripts/run_alphaops_morning.ps1").read_text(encoding="utf-8").lower()
    assert all(token not in script for token in ("start-job", "start-threadjob", "-parallel"))
