"""Offline, idempotent D029 weekly dispatch; never enables broker execution."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from intraday_scanner.research import build_research_protocol, dispatch_r7_weekly


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--code-sha", required=True)
    args = parser.parse_args()
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    protocol = build_research_protocol(source_root=Path.cwd())
    result = dispatch_r7_weekly(dataset=dataset, protocol=protocol, code_sha=args.code_sha, state_path=args.state, dispatch_id=args.dispatch_id)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
