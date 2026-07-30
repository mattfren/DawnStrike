"""Stable service entrypoint for Dawnstrike's canonical performance ledger.

The implementation lives in ``intraday_scanner.performance.service`` so the
typed contracts and the calculation owner remain colocated. This module is the
service-layer import promised by the remediation contract and prevents callers
from creating a second return engine.
"""

from intraday_scanner.performance.service import CanonicalPerformanceService

__all__ = ["CanonicalPerformanceService"]
