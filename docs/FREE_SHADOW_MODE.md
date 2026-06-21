# Free Shadow Mode

Free Shadow Mode is the zero-dollar validation loop for Dawnstrike. It lets you
manually collect premarket screener rows, normalize them, run the normal scoring
engine, save the exact calls, and later upload manual outcomes for paper audit.

It is research/watchlist software only. It does not place broker orders, store
broker trading credentials, or prove future returns.

## Fast Daily Workflow

Export a free screener CSV or copied table, drop it into `data\inbox\screener`,
then run one paper-only pass:

```powershell
py -m intraday_scanner.cli auto-shadow-from-screener `
  --input data\inbox\screener\morning_export.csv `
  --db-path data\shadow.sqlite `
  --out-dir outputs\auto_shadow\morning_export `
  --ai-normalizer none `
  --persist `
  --print
```

Or leave the watcher running:

```powershell
.\scripts\watch_screener_inbox.ps1
```

The dashboard Free Shadow panel then shows the latest raw file, normalized
snapshot, run summary, top paper picks, avoid list, and data warnings.

See `docs\SCREENER_AUTOMATION.md` for aliases, folder flow, and Windows task
shortcuts.

For notification-only mode, see `docs\E2E_AUTOMATION.md` and
`docs\NOTIFICATION_ONLY_WORKFLOW.md`.

## Manual Workflow

```powershell
cd C:\Users\MattFields\Dawnstrike
py -m intraday_scanner.cli init-db --db-path data\shadow.sqlite
py -m intraday_scanner.cli print-upload-prompt
```

Copy your screener table into ChatGPT with the printed prompt, or export a CSV
directly from your screener/broker. ChatGPT may normalize pasted rows, but it is
not the source of truth for prices.

Normalize the manual snapshot:

```powershell
py -m intraday_scanner.cli import-manual-snapshot `
  --input data\manual\morning_snapshot_YYYY-MM-DD.csv `
  --out outputs\manual_snapshot_YYYY-MM-DD `
  --db-path data\shadow.sqlite `
  --persist
```

Run the timestamped shadow scan:

```powershell
py -m intraday_scanner.cli free-shadow-scan `
  --snapshot outputs\manual_snapshot_YYYY-MM-DD\premarket_snapshot.csv `
  --db-path data\shadow.sqlite `
  --out-dir outputs\shadow_scan_YYYY-MM-DD `
  --persist `
  --print
```

After lunch or close, import outcomes and audit them:

```powershell
py -m intraday_scanner.cli import-manual-outcomes `
  --input data\manual\outcomes_YYYY-MM-DD.csv `
  --db-path data\shadow.sqlite `
  --persist

py -m intraday_scanner.cli audit-manual-outcomes `
  --db-path data\shadow.sqlite `
  --out-dir outputs\shadow_audit_YYYY-MM-DD `
  --persist

py -m intraday_scanner.cli evaluate-intelligence-outcomes `
  --db-path data\shadow.sqlite `
  --out-dir outputs\intelligence_outcomes_YYYY-MM-DD `
  --persist

py -m intraday_scanner.cli free-shadow-report `
  --db-path data\shadow.sqlite `
  --out-dir outputs\shadow_report `
  --persist
```

## Labels

Free Shadow Mode writes explicit labels:

- `data_source_kind=manual`
- `shadow_mode=true`
- `manual_uploaded_data=true`
- `paid_data=false`
- `fixture_only=false` unless the input path is under `sample_data`

Manual/free results are for validation only. They are not paid/live provider
validation and are not a recommendation to use real money.
