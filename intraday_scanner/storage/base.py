"""Storage interface for scan results and paper audits."""

from __future__ import annotations

from typing import Protocol

from intraday_scanner.models import ScanResult


class ScanStore(Protocol):
    def persist_scan_result(self, result: ScanResult) -> None:
        ...

    def load_latest_scan(self) -> dict[str, object] | None:
        ...
