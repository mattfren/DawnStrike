# Screener Automation

Dawnstrike can run a zero-dollar Free Shadow scan from raw screener exports. This
is research-only automation: it normalizes files, scores paper picks, archives the
raw export, and records what happened. It does not place orders, connect to a
broker trading API, or call a paid market-data provider.

## Folder Flow

- Drop raw exports into `data\inbox\screener`.
- Successful files move to `data\processed\screener`.
- Failed files move to `data\failed\screener`.
- Normalized snapshots and scan outputs write under `outputs\auto_shadow`.
- Automation logs write to `logs\screener_automation.log`.

Supported raw file types are `.csv`, `.tsv`, and copied text tables.

## Accepted Screener Columns

The deterministic parser recognizes common aliases:

- ticker: `ticker`, `symbol`
- company: `company`, `name`, `security`
- price: `price`, `last`, `premarket_price`
- previous close: `previous close`, `prev close`, `close`
- high/low: `premarket high`, `high`, `premarket low`, `low`
- volume: `premarket volume`, `pre-market volume`, `volume`
- catalyst: `headline`, `news`, `catalyst`
- URL: `url`, `link`, `source url`
- timestamp: `timestamp`, `as_of`, `as of`

Dawnstrike computes `dollar_volume` and `gap_pct` from the supplied prices and
volume. Unknown enrichment fields stay blank; the parser does not invent float,
market cap, short interest, halt, offering, reverse split, or catalyst URL data.

Every normalized row is labeled:

- `data_source_kind=manual`
- `shadow_mode=true`
- `paid_data=false`
- `manual_uploaded_data=true`

## One File

```powershell
py -m intraday_scanner.cli auto-shadow-from-screener `
  --input data\inbox\screener\morning_export.csv `
  --db-path data\shadow.sqlite `
  --out-dir outputs\auto_shadow\morning_export `
  --ai-normalizer none `
  --persist `
  --print
```

This writes `run_summary.json`, `ranked_candidates.csv`, `top_explosive.csv`,
`avoid_list.csv`, and the normalized `premarket_snapshot.csv`.

## Watch The Inbox

```powershell
py -m intraday_scanner.cli watch-screener-inbox `
  --inbox data\inbox\screener `
  --db-path data\shadow.sqlite `
  --out-root outputs\auto_shadow `
  --ai-normalizer none `
  --poll-seconds 10
```

For a finite test run:

```powershell
py -m intraday_scanner.cli watch-screener-inbox `
  --inbox data\inbox\screener `
  --db-path data\shadow.sqlite `
  --out-root outputs\auto_shadow `
  --ai-normalizer none `
  --max-files 1 `
  --poll-seconds 1
```

## Daily Task

```powershell
py -m intraday_scanner.cli auto-shadow-daily `
  --date 2026-06-20 `
  --db-path data\shadow.sqlite `
  --ai-normalizer none
```

Windows shortcuts:

- `scripts\run_auto_shadow_once.ps1`
- `scripts\watch_screener_inbox.ps1`
- `scripts\run_daily_shadow_scan.bat`

## Optional AI Normalizer

Default is `--ai-normalizer none`. Use that for normal CSV/table exports.

`--ai-normalizer codex-cli` is an optional fallback for malformed copied text.
It uses the local Codex CLI, the checked-in
`templates\chatgpt_screener_to_snapshot_prompt.md`, and validates the returned
CSV before scoring it. If Codex is missing, not logged in, or returns malformed
rows, the command fails clearly and the automation path archives the raw file in
`data\failed\screener`.

`--ai-normalizer openai-api` is stubbed in this zero-secrets build and requires
an explicit `OPENAI_API_KEY`; tests do not call the network.

## Dashboard

Open the dashboard and choose the SQLite data source with `data\shadow.sqlite`.
The Free Shadow panel shows the inbox path, processed/failed counts, latest raw
file, latest normalized snapshot, latest run summary, top picks, avoid list, and
data warnings.

Use these picks as a paper validation aid. If you trade manually in a broker,
Dawnstrike is still not executing or managing that trade.
