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

If you want console scripts directly, add your Python Scripts directory:

```powershell
$env:PATH = "C:\Users\MattFields\AppData\Local\Programs\Python\Python313\Scripts;$env:PATH"
intraday-scan --help
```

Recommended local app command:

```powershell
py -m streamlit run app.py --server.port 8502
```

The app does not place orders, hold broker credentials, or execute trades.

