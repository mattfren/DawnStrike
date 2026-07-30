"""Build Dawnstrike's framework-free public site from the canonical snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
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
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Build a diagnostic artifact from a dirty checkout; never use for deployment.",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    source = _source_metadata(root)
    if source.get("source_clean") is not True and not args.allow_dirty:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "reason": "source_not_clean",
                    "source_sha": source.get("source_sha"),
                    "next_action": "Commit the candidate, then rebuild from the clean SHA.",
                },
                sort_keys=True,
                indent=2,
            )
        )
        return 2
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
    data_hash = str((result.get("readiness") or {}).get("payload_sha256") or "")
    build_id = hashlib.sha256(
        f"{source.get('source_sha')}:{data_hash}:{market_date}".encode()
    ).hexdigest()[:20]
    file_hashes = _file_hashes(output_root, exclude={"build-manifest.json"})
    (output_root / "build-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.public_build.v1",
                "source_sha": source.get("source_sha"),
                "source_clean": source.get("source_clean"),
                "build_id": build_id,
                "data_hash_sha256": data_hash,
                "market_date": market_date,
                "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "status": result.get("status"),
                "readiness": result.get("readiness"),
                "file_hashes": file_hashes,
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


def _source_metadata(root: Path) -> dict[str, object]:
    try:
        source_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return {"source_sha": source_sha, "source_clean": not bool(dirty)}
    except (OSError, subprocess.CalledProcessError):
        return {"source_sha": None, "source_clean": False}


def _file_hashes(root: Path, *, exclude: set[str]) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and str(path.relative_to(root)).replace("\\", "/") not in exclude
    }


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
