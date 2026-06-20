"""Universe parsing helpers."""

from __future__ import annotations

from pathlib import Path


def parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [symbol.strip().upper() for symbol in raw.split(",") if symbol.strip()]


def load_symbols_file(path: str | Path | None) -> list[str]:
    if path is None:
        return []
    symbol_path = Path(path)
    lines = symbol_path.read_text(encoding="utf-8").splitlines()
    return [line.strip().upper() for line in lines if line.strip() and not line.startswith("#")]
