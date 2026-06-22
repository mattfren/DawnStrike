# Operator Runbook

Dawnstrike is research/watchlist software only. It does not place orders or hold
broker credentials.

## Morning Flow

1. Run source doctor:

```powershell
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

2. Collect and persist candidates:

```powershell
py -m intraday_scanner.cli web-auto-collect --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\web_auto_test --persist --print
```

3. Run one Telegram/web cycle:

```powershell
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.yaml --automation-config config\automation.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify telegram --max-cycles 1
```

4. Start dashboard:

```powershell
py -m streamlit run app.py --server.port 8502
```

5. Review picks, source confidence, risk flags, and Telegram preview. Any broker
action remains outside Dawnstrike.

## AlphaOps Flow

Run the adaptive research cycle:

```powershell
py -m intraday_scanner.cli alpha-morning --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_morning --notify telegram --dry-run
```

Then monitor the same names:

```powershell
py -m intraday_scanner.cli alpha-monitor --db-path data\shadow_real.sqlite --notify telegram --dry-run
```

Use `alpha-status` to check whether feature vectors, source reliability, setup
memory, and enough real days exist. Use `alpha-report` for top1/top3/top5 and
insufficient-sample truth. Remove `--dry-run` only after Telegram secrets are
configured. Dawnstrike still does not place orders.

Daily command:

```powershell
py -m intraday_scanner.cli alpha-cycle --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle --notify telegram
```

Windows scheduled-task registration:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_alphaops_tasks.ps1
```

## Outcomes

Save manual outcome CSVs under:

```text
data\inbox\outcomes\outcomes_YYYY-MM-DD.csv
```

Then import and audit the outcomes. Missing values stay unavailable and are not
counted as zero.

```powershell
py -m intraday_scanner.cli import-manual-outcomes --input data\inbox\outcomes\outcomes_YYYY-MM-DD.csv --db-path data\shadow_real.sqlite --persist
py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist
py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report
```

For AlphaOps learning and reporting, run:

```powershell
py -m intraday_scanner.cli alpha-outcomes --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-learn --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
```

## Historical Calendar

Use the dashboard `Historical Calendar` tab to review saved daily picks, missing
outcomes, Telegram messages, equal-weight top1/top3/top5 shadow returns, and
monitor-exit evidence. The tab reads `data\shadow_real.sqlite` by default.
Missing outcome rows show `Outcome needed` and are not counted as zero.

Generate the offline report:

```powershell
py -m intraday_scanner.cli calendar-report --db-path data\shadow_real.sqlite --out-dir outputs\calendar_report
py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report
```

For a specific month:

```powershell
py -m intraday_scanner.cli calendar-report --db-path data\shadow_real.sqlite --out-dir outputs\calendar_report --month 2026-06
```

End-of-day scheduled flow can run:

```powershell
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist
py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report
```

It does not fabricate outcomes when no CSV has been imported. See
`docs\HISTORICAL_SIGNAL_LEDGER.md`, `docs\RETURN_ATTRIBUTION.md`, and
`docs\HISTORICAL_CALENDAR.md`.
