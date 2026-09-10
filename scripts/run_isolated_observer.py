"""Run the isolated R2 raw observation/census sidecar from retained inputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.observation.runner import ObservationRunError, run_observer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source-events", type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-source-config-sha256")
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--max-events", type=int, default=10_000)
    parser.add_argument("--max-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args()
    try:
        result = run_observer(
            manifest_path=args.manifest,
            source_events_path=args.source_events,
            output_root=args.output_root,
            repository_root=args.repository_root,
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_source_config_sha256=args.expected_source_config_sha256,
            max_pages=args.max_pages,
            max_events=args.max_events,
            max_bytes=args.max_bytes,
            retries=args.retries,
            stop_path=args.stop_file,
        )
    except ObservationRunError as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result.receipt, sort_keys=True))
    return 0 if result.status in {"CAPTURED", "EMPTY", "PARTIAL", "STOPPED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
