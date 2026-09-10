"""Run one OPS09 capture and its existing public consumers under one guard."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from intraday_scanner.observation.ops06_bars_adapter import adapt_ops05_to_r3  # noqa: E402
from intraday_scanner.observation.ops09 import _run_consumers  # noqa: E402


def _ops05_main():
    module_path = REPO_ROOT / "scripts" / "ops05_historical_bars.py"
    spec = importlib.util.spec_from_file_location("dawnstrike_ops05_cli", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("OPS09 could not load the source-pinned OPS05 CLI")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--census", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-config-hash", required=True)
    parser.add_argument("--capture-receipt-hash", required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--decision-artifact", type=Path)
    parser.add_argument("--adapter-output-root", type=Path)
    parser.add_argument("--database-path", type=Path)
    parser.add_argument("--repo-sha", required=True)
    parser.add_argument("--as-of", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    ops05_args = [
        "ops05_historical_bars.py", "--market-date", args.market_date,
        "--census", str(args.census), "--output-root", str(args.output_root),
        "--source-config-hash", args.source_config_hash,
        "--capture-receipt-hash", args.capture_receipt_hash,
    ]
    if args.fixture is not None:
        ops05_args += ["--fixture", str(args.fixture)]
    elif args.execute:
        ops05_args += ["--execute", "--env-file", str(args.env_file or ".env")]
    else:
        print(json.dumps({"status": "READY", "capture_root": str(args.output_root.resolve())}))
        return 0
    old_argv = sys.argv
    try:
        sys.argv = ops05_args
        exit_code = int(_ops05_main() or 0)
    finally:
        sys.argv = old_argv
    if exit_code != 0:
        return exit_code
    receipt_path = args.output_root.resolve() / "receipt.json"
    payload = {"status": "CAPTURED", "capture_root": str(args.output_root.resolve())}
    if args.decision_artifact is None or not args.decision_artifact.is_file():
        payload["decision_status"] = "MISSING_INPUT"
        print(json.dumps(payload, sort_keys=True))
        return 0
    adapter_root = (args.adapter_output_root or (args.output_root.resolve().parent / "ops06")).resolve()
    adapted = adapt_ops05_to_r3(
        observation_root=args.output_root.resolve(),
        decision_artifact=args.decision_artifact.resolve(),
        output_root=adapter_root,
        as_of=args.as_of,
    )
    if args.database_path is None:
        raise RuntimeError("OPS09 native pipeline requires an isolated database path")
    consumers = _run_consumers(
        adapted=adapted,
        database_path=args.database_path.resolve(),
        session={"market_date": args.market_date},
        repo_sha=args.repo_sha,
    )
    payload.update({
        "decision_status": "BOUND",
        "adapter_output_root": str(adapter_root),
        "adapter_status": adapted["adapter_packet"].get("status"),
        "label_count": len(adapted["adapter_packet"].get("labels", [])),
        "consumers": {
            "daily_status": consumers["daily"].get("status"),
            "weekly_status": consumers["weekly"].get("status"),
            "database_path": consumers["database_path"],
        },
    })
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
