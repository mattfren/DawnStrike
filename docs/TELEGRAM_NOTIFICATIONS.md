# Telegram Notifications

Telegram messages are compact research/watchlist notifications. They do not
place orders, hold broker credentials, or expose secrets.

## Watchlist Example

```text
🚀 Dawnstrike Watchlist
⏱ 8:15 CT | 3 picks | Source: web/manual

1) NOVA — 88.1 | +89% | $5.20
   🎯 $5.48 | 🛑 $3.20
   📰 FDA phase 2 data
   ⚠️ none

🚫 Avoid: 1
Research only. No orders placed.
```

## Source Failure Example

```text
📡 Dawnstrike Source Check
No usable rows found.
Tried:
- local inbox: empty
- stockanalysis: no rows
- tradingview: stale / failed

Next:
Try again during premarket or drop CSV into data\inbox\screener.
```

## Test Commands

Dry-run without secrets:

```powershell
py -m intraday_scanner.cli telegram-test --dry-run --db-path data\shadow_real.sqlite
```

Real Telegram send:

```powershell
py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite
```

Force only the Telegram test event through dedupe:

```powershell
py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite --force
```

Dry-run and real-send tests use separate event keys. `--force` bypasses only the
test-event dedupe key. Tokens and chat IDs are not printed or persisted in
notification payloads.
