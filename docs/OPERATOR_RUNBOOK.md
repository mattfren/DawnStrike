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

## Outcomes

Save manual outcome CSVs under:

```text
data\inbox\outcomes\outcomes_YYYY-MM-DD.csv
```

Then run outcome import/audit from the UI or CLI. Missing values stay
unavailable and are not counted as zero.

For AlphaOps learning, run:

```powershell
py -m intraday_scanner.cli alpha-outcomes --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-learn --db-path data\shadow_real.sqlite
```
