"""CLI compatibility entry point for governed state preparation.

Keep the implementation in :mod:`scripts.state_preparation` so activation
contract tests can import it without invoking a process.
"""

try:
    from scripts.state_preparation import main
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from state_preparation import main

if __name__ == "__main__":
    raise SystemExit(main())
