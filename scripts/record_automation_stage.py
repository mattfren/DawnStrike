"""Persist a dated upstream automation receipt for daily finalization.

This tiny boundary keeps the publication pipeline independent from the large
legacy operator process while retaining its real exit status and source paths.
It never creates market data and never places orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--status", choices=("complete", "failed"), required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    stages = [
        {
            "stage": name,
            "status": "complete" if args.status == "complete" else "failed",
            "source": str(Path(args.source_root) / args.out_dir),
        }
        for name in (
            "source_collection",
            "candidate_normalization",
            "selection",
            "delivery",
            "paper_fills",
            "outcome_capture",
        )
    ]
    payload = {
        "schema_version": "dawnstrike.upstream_automation_receipt.v1",
        "market_date": args.market_date,
        "status": args.status,
        "exit_code": args.exit_code,
        "source_root": str(Path(args.source_root).resolve()),
        "out_dir": args.out_dir,
        "stages": stages,
        "research_only": True,
        "live_trading_enabled": False,
        "recorded_at": now,
    }
    run_id = hashlib.sha256(
        f"dawnstrike:eod:{args.market_date}".encode()
    ).hexdigest()[:24]
    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_runs (
                id TEXT PRIMARY KEY,
                run_type TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                out_dir TEXT,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO automation_runs
                (id, run_type, status, started_at, completed_at, out_dir, payload_json)
            VALUES (?, 'alphaops_eod', ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                completed_at = excluded.completed_at,
                out_dir = excluded.out_dir,
                payload_json = excluded.payload_json
            """,
            (
                run_id,
                args.status,
                now,
                now,
                args.out_dir,
                json.dumps(payload, sort_keys=True),
            ),
        )
    print(json.dumps({"run_id": run_id, **payload}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
