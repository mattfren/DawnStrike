# Dawnstrike AlphaOps v4 Final Report - 2026-06-21

## Summary

Built AlphaOps v4 as an additive research/watchlist layer on top of Signal
Engine v3. It persists feature vectors, risk/no-trade decisions, alpha signals,
source reliability, setup memory, outcome labels, learning runs, and performance
truth reports. No broker/order execution path was added.

Second-pass hardening expanded the feature vector to the named AlphaOps fields,
added source-reliability score adjustments, added a gated date-split offline
regression path, upgraded performance truth buckets, and rendered dashboard
bucket tables when outcome labels exist.

## Files Changed

- `intraday_scanner/alpha/*`
- `intraday_scanner/services/alpha_cycle_service.py`
- `intraday_scanner/services/learning_service.py`
- `intraday_scanner/services/source_reliability_service.py`
- `intraday_scanner/services/signal_review_service.py`
- `intraday_scanner/storage/sqlite_store.py`
- `intraday_scanner/cli.py`
- `intraday_scanner/notifiers/console.py`
- `intraday_scanner/notifiers/service.py`
- `intraday_scanner/notifiers/telegram_formatter.py`
- `intraday_scanner/dashboard/data_loader.py`
- `app.py`
- `tests/test_alpha_ops.py`
- `docs/ALPHAOPS_V4.md`
- `docs/PLAYBOOK_ENGINE.md`
- `README.md`
- `docs/SIGNAL_ENGINE_V3.md`
- `docs/PERFORMANCE_AUDIT.md`
- `docs/DATA_QUALITY.md`
- `docs/OPERATOR_RUNBOOK.md`
- `docs/TELEGRAM_NOTIFICATIONS.md`

## Commands Run

```powershell
py -m pip install -e ".[dev]"
py -m pytest -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall intraday_scanner app.py tests
git diff --check
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.example.yaml --out-dir outputs\source_doctor --print
py -m intraday_scanner.cli web-auto-collect --config config\web_sources.example.yaml --db-path data\shadow_real.sqlite --out-dir outputs\web_auto\alphaops_validation --persist --print
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.example.yaml --automation-config config\automation.example.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify console --dry-run --max-cycles 1
py -m intraday_scanner.cli alpha-cycle --config config\web_sources.example.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle_validation --notify console --dry-run
py -m intraday_scanner.cli alpha-cycle --config tests\fixtures\web_sources_fixture.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle_followup --notify console --dry-run
py -m intraday_scanner.cli alpha-monitor --db-path data\shadow_real.sqlite --notify console --dry-run
py -m intraday_scanner.cli alpha-status --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
py -m intraday_scanner.cli alpha-learn --db-path data\shadow_real.sqlite
rg -n "place_order|submit_order|create_order|market_order|limit_order|buy\(|sell\(|execute_trade|order execution|orders_enabled" intraday_scanner app.py tests docs README.md
```

## Test Results

- `py -m pytest -p no:cacheprovider`: PASS, 147 passed.
- `py -m ruff check .`: PASS.
- `py -m mypy intraday_scanner`: PASS, no issues in 89 source files.
- `py -m compileall intraday_scanner app.py tests`: PASS.
- `git diff --check`: PASS; Windows LF-to-CRLF warnings only.

## Operational Results

- `web-source-doctor`: PASS. Found enabled candidate sources and 110 normalized
  rows from live public-source checks.
- `web-auto-collect`: PASS. Persisted a public-source shadow snapshot with 103
  deduped candidates.
- `web-telegram-daemon`: PASS after fixing Windows console Unicode dry-run
  output. The rerun completed one successful dry-run cycle.
- `alpha-cycle`: PASS. Persisted AlphaOps feature vectors and research-only
  signals. The follow-up fixture cycle persisted latest scan
  `957b6910-d918-4274-89b0-b61313e6776b` with Alpha 66.62, source reliability
  adjustment, `ml_score_used=false`, and no order behavior.
- `alpha-monitor`: PASS. No live/current price source was configured, so it sent
  one manual-review dry-run monitor message and used dedupe protection.
- `alpha-status`: PASS. Reported `dawnstrike-alphaops-v4`, 11 signals, 11
  feature vectors, 4 source reliability rows, 0 outcome labels, and
  `enough_evidence=false`.
- `alpha-report`: PASS. Wrote `outputs\alpha_report\alpha_report.json` and
  `outputs\alpha_report\alpha_report.md` with insufficient-sample truth.
- `alpha-learn`: PASS. Completed with zero labels and persisted an
  insufficient-sample truth run.

## AlphaOps Behavior

- Fewer than 20 real shadow days uses rule-based scoring with
  `INSUFFICIENT_SAMPLE` confidence/expectancy.
- Empirical edge calibration uses shrinkage toward the global mean.
- Source reliability is part of scoring. Reliable sources can add a small bonus;
  poor source reliability penalizes the alpha score.
- The offline ML path is date-split, deterministic, and inactive unless enough
  dated outcomes exist and the model beats the rule baseline out of sample.
- Hard risk gates block alerts for halt/offering, invalid ticker, missing
  price/volume, sub-min price, extreme spread, stale source, source conflict,
  and missing source confidence.
- No-trade is a first-class outcome and can send "No clean edge today" instead
  of forcing a weak pick.
- Monitor labels include `BREAKOUT WATCH`, `CAUTION`, `INVALIDATED`,
  `THESIS BROKEN`, and `MANUAL REVIEW`.

## Risk Governor Details

Hard avoids include current halt, active offering/dilution, invalid ticker,
missing price, missing volume, sub-min price, extreme spread, stale source,
source conflict, and missing source confidence. Soft penalties include unknown
float, missing previous close/high/low, no catalyst, public URL-only data, low
source count, and mega-gap exhaustion.

## No-Trade Filter Details

No-trade is valid when there are no usable candidates, source confidence is too
low, the top candidate is too risky, all candidates are hard-avoid, spread or
volume quality is poor, source data is stale/conflicting, historical edge is
insufficient, or drawdown risk is too high. The follow-up fixture validation
returned one watchlist candidate rather than forcing a no-trade, while the test
suite covers the no-clean-edge path.

## Learning Loop Details

Every official alpha signal stores rank, timestamp, setup key, feature hash,
Telegram key, alert state, risk flags, and AlphaOps scores. Manual outcomes are
labeled into 1m/5m/15m/lunch/close winners, high-after-entry return,
low-after-entry drawdown, MFE/MAE, failed-fast, held-up, squeeze, and trap
labels. Learning updates setup memory, source reliability, risk-flag impact,
catalyst buckets, score deciles, and alpha buckets.

## Telegram Examples

Morning:

```text
🚀 Dawnstrike Alpha Watch
⏱ 8:15 CT | Edge: HIGH | 3 picks
1) NOVA — Alpha 91.4 | HIGH
   Trigger $5.48 | Invalid $3.20 | Target $6.25
No orders placed. Research only.
```

No-trade:

```text
📡 Dawnstrike Alpha Check
No clean edge today.
Reason: weak source quality / high risk
Next: wait or use manual CSV fallback.
```

Outcome summary:

```text
📊 Dawnstrike Shadow Results
Sample: 7 days — insufficient sample
```

## Dashboard Fields

The dashboard loads AlphaOps signals, feature counts, source reliability, setup
memory, outcome labels, score decile performance, setup bucket returns, risk
flag impact, catalyst bucket returns, outlier dependency, missing outcome rate,
real days collected, and evidence sufficiency.

## Performance Truth Engine

`alpha-report` includes top1/top3/top5, median and average returns, win rate,
worst day, max drawdown, outlier dependency, best/worst setup, source,
catalyst, and risk flag buckets, score deciles, alpha buckets, setup sample
size, missing outcome rate, and evidence warnings. It will not claim success
when fewer than 20 real days exist, fewer than 60 days exist for strong
evidence, returns are fixture/sample/manual-only, outcomes are missing, or one
outlier dominates.

## Remaining Blockers

- Real validation still requires collecting real market-day shadow outcomes.
- Live/current price monitoring requires a configured current-price source.
- Telegram real-send requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## What Requires Paid Data

Higher-quality real-time quotes, spreads, complete premarket high/low, float,
short interest, halt/offering enrichment, and lower-latency current-price
monitoring require proper provider data. Public web rows remain unverified
shadow data.

## Exact Daily Command

```powershell
py -m intraday_scanner.cli alpha-cycle --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle --notify telegram
```

## Exact Scheduled-Task Command

```powershell
schtasks /Create /TN "Dawnstrike AlphaOps" /SC MINUTE /MO 5 /TR "py -m intraday_scanner.cli alpha-monitor --db-path data\shadow_real.sqlite --notify telegram" /F
```

## No-Trading Safety Result

PASS. Safety search found no implementation path for order submission. Matches
were safety docs/tests plus `orders_enabled: False`. No broker/order behavior
was changed, no order execution was added, and no secrets/DB/output/log files
are intended for commit.
