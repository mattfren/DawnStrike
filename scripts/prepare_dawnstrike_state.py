"""CLI compatibility entry point for governed state preparation.

Keep the implementation in :mod:`scripts.state_preparation` so activation
contract tests can import it without invoking a process.
"""

import sys
from pathlib import Path

# Direct execution sets sys.path[0] to ``scripts`` and can inherit a stale
# runtime checkout through PYTHONPATH.  Put this candidate's repository root
# first before importing any package so the CLI cannot mix source and runtime
# module versions.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT in sys.path:
    sys.path.remove(_REPO_ROOT)
sys.path.insert(0, _REPO_ROOT)

try:
    from scripts.state_preparation import main
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from state_preparation import main

if __name__ == "__main__":
    raise SystemExit(main())
