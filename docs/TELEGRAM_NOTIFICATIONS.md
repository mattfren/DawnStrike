# Telegram Notifications

Dawnstrike Telegram messages are research/watchlist notifications only. They use
labels such as `WATCH`, `HIGH VOLATILITY WATCH`, `BREAKOUT TRIGGER`, `CAUTION`,
`INVALIDATED`, `THESIS BROKEN`, and `OUTCOME NEEDED`.

## Create A Bot

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`.
3. Follow BotFather prompts and copy the bot token.
4. Add the bot to the chat where you want Dawnstrike alerts.

## Get Chat ID

One simple path:

1. Send any message in the target chat after adding the bot.
2. Visit this URL in a browser, replacing the token:

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates
```

3. Copy the `chat.id` value for the chat.

## Set Secrets

Use `.env` or your shell environment:

```powershell
$env:TELEGRAM_BOT_TOKEN="..."
$env:TELEGRAM_CHAT_ID="..."
```

Do not commit `.env`.

## Test

Dry-run without secrets:

```powershell
py -m intraday_scanner.cli telegram-test --dry-run --db-path data\shadow_real.sqlite
```

Real Telegram send:

```powershell
py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite
```

Missing secrets fail clearly and do not print the token.

## Run Auto-Pilot

```powershell
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.yaml --automation-config config\automation.example.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify telegram --max-cycles 1
```

The message includes timestamp, data quality, top tickers, score, gap, premarket
price, dollar volume, supplied catalyst text, breakout trigger, invalidation,
risk flags, and the research-only disclaimer.
