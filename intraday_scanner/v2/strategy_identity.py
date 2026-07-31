"""Canonical deterministic identity for versioned v2 strategies."""

from __future__ import annotations

import hashlib
import inspect
import json

from intraday_scanner.v2.strategies import StrategySpec


def strategy_semantics_payload(strategy: StrategySpec) -> dict[str, object]:
    """Return the bounded configuration whose digest identifies strategy semantics."""

    try:
        source = inspect.getsource(strategy.generate_signal)
    except (OSError, TypeError):
        source = ""
    module = inspect.getmodule(strategy.generate_signal)
    try:
        module_source = inspect.getsource(module) if module is not None else ""
    except (OSError, TypeError):
        module_source = ""
    return {
        "compatible_timeframe": strategy.compatible_timeframe,
        "entry_logic": strategy.entry_logic,
        "exit_logic": strategy.exit_logic,
        "generate_signal_module": getattr(strategy.generate_signal, "__module__", "unknown"),
        "generate_signal_name": getattr(strategy.generate_signal, "__qualname__", "unknown"),
        "generate_signal_source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "implementation_module_sha256": hashlib.sha256(
            module_source.encode("utf-8")
        ).hexdigest(),
        "indicators": list(strategy.indicators),
        "parameters": strategy.parameters,
        "position_sizing_assumption": strategy.position_sizing_assumption,
        "required_data_fields": list(strategy.required_data_fields),
        "status": strategy.status,
        "stop_logic": strategy.stop_logic,
        "strategy_id": strategy.strategy_id,
        "strategy_version": strategy.version,
        "target_logic": strategy.target_logic,
    }


def strategy_semantics_fingerprint(strategy: StrategySpec) -> str:
    canonical = json.dumps(
        strategy_semantics_payload(strategy),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
