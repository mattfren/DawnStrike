"""CLI compatibility entry point for governed state preparation.

Keep the implementation in :mod:`scripts.state_preparation` so activation
contract tests can import it without invoking a process.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT_TEXT = str(_REPO_ROOT)
if _REPO_ROOT_TEXT in sys.path:
    sys.path.remove(_REPO_ROOT_TEXT)
sys.path.insert(0, _REPO_ROOT_TEXT)

try:
    from scripts import state_preparation as _state_preparation
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    import state_preparation as _state_preparation

_EXPECTED_IMPLEMENTATION = (_REPO_ROOT / "scripts" / "state_preparation.py").resolve()
if Path(_state_preparation.__file__).resolve() != _EXPECTED_IMPLEMENTATION:
    raise RuntimeError("state preparation CLI did not load the exact candidate implementation")
main = _state_preparation.main

if __name__ == "__main__":
    raise SystemExit(main())
