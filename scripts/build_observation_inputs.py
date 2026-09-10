"""Bind an authenticated intraday capture receipt into R2 observer inputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.observation.producer import (
    ObservationProducerError,
    build_observation_inputs,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-receipt", required=True, type=Path)
    parser.add_argument("--scope-declaration", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--decision-deadline", required=True)
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--max-events", type=int, default=100_000)
    parser.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument(
        "--reduction-mode",
        choices=("none", "bounded_derivative"),
        default="none",
        help="Keep hard event/byte caps; bounded_derivative records source counts and emits an explicit subset.",
    )
    args = parser.parse_args()
    try:
        result = build_observation_inputs(
            capture_receipt_path=args.capture_receipt,
            scope_declaration_path=args.scope_declaration,
            output_root=args.output_root,
            decision_deadline=args.decision_deadline,
            repository_root=args.repository_root,
            max_events=args.max_events,
            max_bytes=args.max_bytes,
            reduction_mode=args.reduction_mode,
        )
    except ObservationProducerError as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
