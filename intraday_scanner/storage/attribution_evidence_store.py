"""Append-only persistence for evidence-linked trade attribution receipts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from intraday_scanner.errors import StorageError
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

FACTOR_STATUSES = frozenset(
    {
        "observed_defect",
        "supported_contributor",
        "suspected",
        "unknown",
        "not_applicable",
    }
)


class AttributionEvidenceError(StorageError):
    """Raised when attribution evidence cannot be persisted or loaded."""


class AttributionEvidenceStore:
    """Persist immutable case and factor evidence without destructive updates."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        SQLiteScanStore(self.db_path).initialize()

    def persist_cases(self, cases: list[dict[str, Any]]) -> dict[str, int]:
        """Insert cases and their zero-to-many factors idempotently."""

        self.initialize()
        case_inserted = 0
        case_skipped = 0
        factor_inserted = 0
        factor_skipped = 0
        try:
            with sqlite3.connect(self.db_path) as connection:
                for case in cases:
                    self._validate_case(case)
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO trade_attribution_cases
                        (case_id, trade_id, market_date, ticker, strategy_id,
                         attribution_status, coverage_status, evidence_hash_sha256,
                         payload_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(case["case_id"]),
                            str(case["trade_id"]),
                            _optional(case.get("market_date")),
                            _optional(case.get("ticker")),
                            _optional(case.get("strategy_id")),
                            str(case["attribution_status"]),
                            str(case["coverage_status"]),
                            str(case["evidence_hash_sha256"]),
                            json.dumps(case, sort_keys=True, default=str),
                            str(case.get("created_at") or ""),
                        ),
                    )
                    if cursor.rowcount:
                        case_inserted += 1
                    else:
                        case_skipped += 1
                    for factor in list(case.get("factors") or []):
                        self._validate_factor(factor)
                        cursor = connection.execute(
                            """
                            INSERT OR IGNORE INTO trade_attribution_factors
                            (factor_id, case_id, factor_key, factor_status,
                             evidence_hash_sha256, evaluator_version, confidence_basis,
                             counterfactual_policy, payload_json, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(factor["factor_id"]),
                                str(case["case_id"]),
                                str(factor["factor_key"]),
                                str(factor["factor_status"]),
                                _optional(factor.get("evidence_hash_sha256")),
                                str(factor["evaluator_version"]),
                                str(factor["confidence_basis"]),
                                str(factor["counterfactual_policy"]),
                                json.dumps(factor, sort_keys=True, default=str),
                                str(factor.get("created_at") or ""),
                            ),
                        )
                        if cursor.rowcount:
                            factor_inserted += 1
                        else:
                            factor_skipped += 1
            return {
                "case_inserted": case_inserted,
                "case_skipped": case_skipped,
                "factor_inserted": factor_inserted,
                "factor_skipped": factor_skipped,
            }
        except sqlite3.Error as exc:
            raise AttributionEvidenceError(
                f"Could not persist trade attribution evidence: {exc}"
            ) from exc

    def load_cases(self, *, limit: int = 100_000) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with sqlite3.connect(self.db_path) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json FROM trade_attribution_cases
                    ORDER BY market_date ASC, trade_id ASC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise AttributionEvidenceError(
                f"Could not load trade attribution cases: {exc}"
            ) from exc

    def load_factors(self, case_id: str) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with sqlite3.connect(self.db_path) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json FROM trade_attribution_factors
                    WHERE case_id = ? ORDER BY factor_key ASC, factor_id ASC
                    """,
                    (case_id,),
                ).fetchall()
            return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise AttributionEvidenceError(
                f"Could not load trade attribution factors: {exc}"
            ) from exc

    @staticmethod
    def _validate_case(case: dict[str, Any]) -> None:
        for field in (
            "case_id",
            "trade_id",
            "attribution_status",
            "coverage_status",
            "evidence_hash_sha256",
            "created_at",
        ):
            if not str(case.get(field) or "").strip():
                raise ValueError(f"attribution case missing {field}")

    @staticmethod
    def _validate_factor(factor: dict[str, Any]) -> None:
        for field in (
            "factor_id",
            "factor_key",
            "factor_status",
            "evaluator_version",
            "confidence_basis",
            "counterfactual_policy",
            "created_at",
        ):
            if not str(factor.get(field) or "").strip():
                raise ValueError(f"attribution factor missing {field}")
        if factor["factor_status"] not in FACTOR_STATUSES:
            raise ValueError(f"invalid attribution factor status: {factor['factor_status']}")


def _optional(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = ["AttributionEvidenceError", "AttributionEvidenceStore", "FACTOR_STATUSES"]
