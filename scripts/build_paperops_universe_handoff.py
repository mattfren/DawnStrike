"""Build or validate the governed Morning-to-PaperOps universe handoff."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.v2.paper_ops.universe_handoff import _cli

if __name__ == "__main__":
    raise SystemExit(_cli())
