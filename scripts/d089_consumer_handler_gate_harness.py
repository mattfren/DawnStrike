"""D089 isolated gate inside the unchanged public daily consumer handler."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import scripts.ops09_pipeline as pipeline
from intraday_scanner.observation import ops09 as consumer_module


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: harness.py MARKER RELEASE pipeline-args...")
    marker = Path(sys.argv[1]).resolve()
    release = Path(sys.argv[2]).resolve()
    original_monitor = consumer_module.run_alpha_v6_daily_monitor

    def gated_monitor(store, *args, **kwargs):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "phase": "daily_handler_entered_after_store_initialize",
                    "pid": os.getpid(),
                    "store_type": type(store).__name__,
                    "marker": str(marker),
                    "created_at": time.time(),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        deadline = time.monotonic() + 300.0
        while not release.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("D089 daily consumer gate timed out")
            time.sleep(0.05)
        return original_monitor(store, *args, **kwargs)

    consumer_module.run_alpha_v6_daily_monitor = gated_monitor
    pipeline_args = sys.argv[3:]
    sys.argv = ["scripts/ops09_pipeline.py", *pipeline_args]
    return int(pipeline.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
