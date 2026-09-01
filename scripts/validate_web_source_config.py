"""Fail-closed validator for Dawnstrike's durable web-source contract."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.approved_tools import run_git
from intraday_scanner.providers.web_source_base import validate_web_source_config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--receipt", default=None)
    args = parser.parse_args()
    result = validate_web_source_config(args.config)
    result["validated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if args.runtime_root:
        try:
            result["runtime_sha"] = run_git(
                Path(args.runtime_root), "rev-parse", "HEAD"
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            result["ready"] = False
            result["status"] = "BLOCKED_CONFIGURATION"
            result.setdefault("violations", []).append("runtime_sha_unavailable")
            result["detail"] = str(exc)
    if args.receipt:
        receipt = Path(args.receipt)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        timestamp = result["validated_at"].replace(":", "")
        temporary = receipt.with_name(f"{receipt.name}.{timestamp}.tmp")
        temporary.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(receipt)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ready") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
