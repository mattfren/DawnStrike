"""D088 isolated verifier harness; production sources remain unchanged."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import scripts.ops09_pipeline as pipeline


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: harness.py MARKER RELEASE pipeline-args...")
    marker = Path(sys.argv[1]).resolve()
    release = Path(sys.argv[2]).resolve()
    original_consumers = pipeline._run_consumers

    def gated_consumers(*args, **kwargs):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "phase": "consumer_entry_before_transaction",
                    "pid": os.getpid(),
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
                raise RuntimeError("D088 consumer gate timed out")
            time.sleep(0.05)
        return original_consumers(*args, **kwargs)

    pipeline._run_consumers = gated_consumers
    pipeline_args = sys.argv[3:]
    sys.argv = ["scripts/ops09_pipeline.py", *pipeline_args]
    return int(pipeline.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
