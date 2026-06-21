# Telegram Notifications

Dawnstrike Telegram messages are compact research/watchlist notifications. They
do not place orders, submit trades, store broker credentials, or provide
financial advice.

Supported operator labels include `🟢 Opening Breakout Candidate`,
`🔥 Momentum Continuation Watch`, `👀 Watch Only`, `🟡 Needs Confirmation`,
`❌ Avoid / Gap-and-Crap Risk`, `OUTCOME NEEDED`, and `MANUAL REVIEW`.

## Compact Message Example

```text
🚀 DAWNSTRIKE WATCHLIST
Data: ✅ 4 candidates | ⚠️ 0 issues
Time: 8:15 CT | Source: manual/web

1. 🟢 NOVA — Opening Breakout Candidate
Premarket: $5.20 | Gap: +89% | Score: 88
Catalyst: Tier A — FDA Phase 2 Data
Structure: Strong
Plan: Trade only over $5.48 confirmation
Stop: $5.12
Targets: $6.00 / $6.75
Avoid if: Fails VWAP or rejects opening range

🚫 Avoid: 1
Research only. No orders placed.
```

No raw JSON is sent in compact mode. Debug-style payloads are only included when
the config explicitly enables debug fields.

## Set Secrets

Use `.env` or your shell environment:

```powershell
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHAT_ID="..."
```

Do not commit `.env`. The token and chat ID are never printed or persisted in
notification payloads.

## Test

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

Dry-run and real-send tests use separate dedupe keys, so a dry-run does not
block the real Telegram test.

## Auto-Pilot

```powershell
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.yaml --automation-config config\automation.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify telegram --max-cycles 1
```

One cycle sends at most:

1. Source failure or morning watchlist
2. Manual monitor needed, only when picks exist
3. Outcome needed, only when picks exist and outcomes are missing
4. Summary, only when enabled

No-data cycles send one short source-check message instead of noisy follow-ups.
That message includes `No usable rows found`, sources tried, the top failure
reason when known, and the next step:
`try again during premarket or drop CSV into data\inbox\screener`. No outcome
reminder is sent when no picks exist.
