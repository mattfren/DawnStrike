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
powershell -ExecutionPolicy Bypass -File scripts\open_command_center_production.ps1
```

Open `http://127.0.0.1:8502/`. This is the canonical local operator dashboard.
The app does not place orders, hold broker credentials, or execute trades.
