"""Build Dawnstrike's framework-free public site from the canonical snapshot."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.services.daily_finalize_service import DailyFinalizeService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    parser.add_argument("--out-dir", default="build/public")
    parser.add_argument("--date", default=None)
    parser.add_argument("--retry-limit", type=int, default=2)
    parser.add_argument("--retry-delay-seconds", type=int, default=0)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    output_root = (root / args.out_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    _clear_known_outputs(output_root)
    market_date = args.date or datetime.now().date().isoformat()
    result = DailyFinalizeService(root / args.db_path, output_root).run(
        market_date=market_date,
        retry_limit=max(0, args.retry_limit),
        retry_delay_seconds=max(0, args.retry_delay_seconds),
    )
    shutil.copy2(root / "web" / "index.html", output_root / "index.html")
    shutil.copy2(root / "web" / "favicon.svg", output_root / "favicon.svg")
    shutil.copytree(root / "web" / "assets", output_root / "assets", dirs_exist_ok=True)
    (output_root / "build-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.public_build.v1",
                "market_date": market_date,
                "status": result.get("status"),
                "readiness": result.get("readiness"),
                "research_only": True,
                "live_trading_enabled": False,
            },
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(result, sort_keys=True, indent=2, default=str))
    return 2 if result.get("status") == "FAILED" else 0


def _clear_known_outputs(output_root: Path) -> None:
    for name in ("index.html", "readiness.json", "stage-manifest.json", "build-manifest.json"):
        path = output_root / name
        if path.is_file():
            path.unlink()
    for name in ("assets", "data"):
        path = output_root / name
        if path.is_dir():
            shutil.rmtree(path)


if __name__ == "__main__":
    raise SystemExit(main())
