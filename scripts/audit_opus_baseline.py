"""Emit a deterministic, read-only inventory of the Stage A SQLite snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

AUDITED_SHA = "ba39a5353045b7d417936ed1aed0ee4802169759"
DEFAULT_OUTPUT = Path("docs/audit/evidence/opus5_baseline_20260809.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quoted_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, object]]:
    cursor = connection.execute(sql)
    names = [column[0] for column in cursor.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


@contextmanager
def _read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise RuntimeError(f"unable to open SQLite snapshot read-only: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=1")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise RuntimeError("SQLite connection did not enter query_only mode")
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error as exc:
            raise RuntimeError(f"SQLite integrity_check failed: {exc}") from exc
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
        yield connection
    finally:
        connection.close()


def _optional_query(
    connection: sqlite3.Connection,
    available_tables: set[str],
    sql: str,
    required_tables: set[str],
) -> list[dict[str, object]] | dict[str, object]:
    missing = sorted(required_tables - available_tables)
    if missing:
        return {"status": "MISSING_TABLE", "missing_tables": missing}
    return _rows(connection, sql)


def collect_inventory(snapshot: Path, audited_sha: str = AUDITED_SHA) -> dict[str, object]:
    with _read_only_connection(snapshot) as connection:
        table_names = [
            row["name"]
            for row in _rows(
                connection,
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name;
                """,
            )
        ]
        available_tables = {str(name) for name in table_names}
        table_counts = {
            str(name): connection.execute(
                f"SELECT COUNT(*) FROM {_quoted_identifier(str(name))}"
            ).fetchone()[0]
            for name in table_names
        }
        baseline_sql = {
            "table_names": [{"name": name} for name in table_names],
            "historical_signal_inventory": _optional_query(
                connection,
                available_tables,
                """
                SELECT COUNT(*) AS rows,
                       COUNT(DISTINCT market_date) AS days,
                       MIN(market_date) AS first_date,
                       MAX(market_date) AS last_date
                FROM historical_signals;
                """,
                {"historical_signals"},
            ),
            "signal_selection_inventory": _optional_query(
                connection,
                available_tables,
                """
                SELECT strategy_id, strategy_version, cohort, decision,
                       COUNT(*) AS rows,
                       COUNT(DISTINCT substr(selected_at, 1, 10)) AS days
                FROM signal_selections
                GROUP BY strategy_id, strategy_version, cohort, decision
                ORDER BY strategy_id, strategy_version, cohort, decision;
                """,
                {"signal_selections"},
            ),
            "outcome_status_source_inventory": _optional_query(
                connection,
                available_tables,
                """
                SELECT outcome_status, outcome_source,
                       COUNT(*) AS rows,
                       COUNT(DISTINCT signal_id) AS signals
                FROM signal_outcomes
                GROUP BY outcome_status, outcome_source
                ORDER BY outcome_status, outcome_source;
                """,
                {"signal_outcomes"},
            ),
            "alpha_v6_table_names": [
                row
                for row in _rows(
                    connection,
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table' AND name LIKE 'alpha_v6_%'
                    ORDER BY name;
                    """,
                )
            ],
            "alpha_v6_label_eligibility": _optional_query(
                connection,
                available_tables,
                """
                SELECT label_family, learning_eligible,
                       COALESCE(exclusion_reason, '') AS exclusion_reason,
                       COUNT(*) AS rows
                FROM alpha_v6_labels
                GROUP BY label_family, learning_eligible, COALESCE(exclusion_reason, '')
                ORDER BY label_family, learning_eligible, exclusion_reason;
                """,
                {"alpha_v6_labels"},
            ),
            "alpha_v6_outcome_eligibility": _optional_query(
                connection,
                available_tables,
                """
                SELECT outcome_status, learning_eligible, COUNT(*) AS rows
                FROM alpha_v6_outcomes
                GROUP BY outcome_status, learning_eligible
                ORDER BY outcome_status, learning_eligible;
                """,
                {"alpha_v6_outcomes"},
            ),
            "alpha_v6_datasets": _optional_query(
                connection,
                available_tables,
                """
                SELECT dataset_id, created_at, training_cutoff, row_count,
                       dataset_hash_sha256
                FROM alpha_v6_datasets
                ORDER BY created_at;
                """,
                {"alpha_v6_datasets"},
            ),
            "paper_positions": _optional_query(
                connection,
                available_tables,
                """
                SELECT position_id, market_date, ticker, status, quantity,
                       opened_at, closed_at, entry_price, exit_price,
                       stop_price, target_price, notional,
                       realized_pnl, realized_return_pct,
                       max_favorable_excursion, max_adverse_excursion
                FROM paper_positions
                ORDER BY opened_at;
                """,
                {"paper_positions"},
            ),
            "biya_trade_intents": _optional_query(
                connection,
                available_tables,
                """
                SELECT intent_id, action, decision_time, decision_price,
                       trigger_price, stop_price, target_price,
                       quantity, notional, reason
                FROM trade_intents
                WHERE ticker = 'BIYA' AND market_date = '2026-07-20'
                ORDER BY decision_time;
                """,
                {"trade_intents"},
            ),
            "biya_trade_fills": _optional_query(
                connection,
                available_tables,
                """
                SELECT fill_id, side, fill_time, fill_price,
                       quantity, gross_notional, slippage_bps
                FROM paper_trade_fills
                WHERE ticker = 'BIYA' AND market_date = '2026-07-20'
                ORDER BY fill_time;
                """,
                {"paper_trade_fills"},
            ),
            "halt_inventory": _optional_query(
                connection,
                available_tables,
                """
                SELECT COUNT(*) AS halt_rows,
                       COUNT(DISTINCT ticker) AS halt_tickers
                FROM halt_events;
                """,
                {"halt_events"},
            ),
            "notification_inventory": _optional_query(
                connection,
                available_tables,
                """
                SELECT COUNT(*) AS rows,
                       COUNT(DISTINCT event_key) AS distinct_event_keys
                FROM notifications_sent;
                """,
                {"notifications_sent"},
            ),
            "duplicate_notifications": _optional_query(
                connection,
                available_tables,
                """
                SELECT event_key, channel, COUNT(*) AS rows
                FROM notifications_sent
                GROUP BY event_key, channel
                HAVING COUNT(*) > 1;
                """,
                {"notifications_sent"},
            ),
            "reward_risk_and_target_provenance": _optional_query(
                connection,
                available_tables,
                """
                SELECT s.market_date, s.ticker,
                       (s.target_1 - s.entry_watch_level) /
                       NULLIF(s.entry_watch_level - s.invalidation_level, 0)
                       AS reward_risk,
                       j.value AS target_derived_from_risk
                FROM historical_signals AS s
                LEFT JOIN json_tree(s.raw_payload_json) AS j
                  ON j.key = 'target_derived_from_risk'
                ORDER BY s.market_date, s.ticker;
                """,
                {"historical_signals"},
            ),
        }
        schema_version = None
        if "schema_version" in available_tables:
            schema_version = connection.execute(
                "SELECT version FROM schema_version ORDER BY applied_at DESC LIMIT 1"
            ).fetchone()[0]
        return {
            "schema": "dawnstrike.opus5-baseline.v1",
            "audited_source_sha": audited_sha,
            "snapshot_name": snapshot.name,
            "snapshot_sha256": _sha256(snapshot),
            "integrity_check": "ok",
            "query_only": 1,
            "schema_version": schema_version,
            "table_counts": table_counts,
            "baseline_sql": baseline_sql,
        }


def write_inventory(
    snapshot: Path, output: Path, audited_sha: str = AUDITED_SHA
) -> dict[str, object]:
    inventory = collect_inventory(snapshot, audited_sha)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--audited-sha", default=AUDITED_SHA)
    args = parser.parse_args()
    if not args.snapshot_db.is_file():
        parser.error(f"snapshot database does not exist: {args.snapshot_db}")
    try:
        inventory = write_inventory(args.snapshot_db, args.output, args.audited_sha)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "status": "PASS",
                "output": str(args.output),
                "snapshot_sha256": inventory["snapshot_sha256"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
