# Windows Setup

Use module-entry commands on Windows until your Python Scripts directory is on
PATH. This avoids relying on `intraday-scan.exe`.

```powershell
cd C:\Users\MattFields\Dawnstrike
py -m pip install -e ".[dev]"
py -m pytest -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m intraday_scanner.cli --help
```

Keep using module-entry commands for development. The protected production
prefix is not added to `PATH`; its console wrappers are outside the scheduled
execution contract, and only the hash-pinned `uv.exe` is admitted by the
governed Vercel publisher.

Recommended local app command:

```powershell
py -m streamlit run app.py --server.port 8502
```

The app does not place orders, hold broker credentials, or execute trades.

