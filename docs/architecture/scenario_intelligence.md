# Scenario Intelligence

Scenario Intelligence is a research-only, paper-audit addition to Dawnstrike.
It never submits, routes, or simulates a broker order.

## Runtime flow

1. Alpaca's read-only News API provides current articles and historical news
   records. Current records are labeled `forward_observed`; historical records
   are labeled `provider_published_at_proxy` and never contribute to forward
   returns.
2. `gpt-5.6-terra` may extract a strict JSON list of factual claims. The
   extraction contract rejects actions, prices, probabilities, expected returns,
   sizing, and all nested equivalents.
3. The versioned deterministic policy evaluates source tier, fact type,
   direction, uncertainty, price, ATR, liquidity, and spread. It alone can
   emit `WATCH`, `ENTER_LONG`, `AVOID`, or `ABSTAIN` and calculate paper levels.
4. An `ENTER_LONG` becomes a bounded `scenario_forward` paper selection. The
   existing watcher records intents, fills, positions, and exits; broker
   execution remains locked. The Scenario lifecycle link is refreshed after
   watcher actions and preserves the entry/exit intent IDs, position-backed
   paper-trade ID, entry/exit fill IDs, and outcome ID.
5. End-of-day finalization admits a return only when one actual entry fill and
   one actual exit fill agree with the closed position, both fills have explicit
   non-negative cost values under one named cost model, and sourced SPY bars
   cover that same fill-to-fill horizon. Untriggered, open, missing, and
   quarantined rows remain separate states and never enter a return denominator.
   Confidence intervals use a fixed-seed, with-replacement bootstrap so the same
   eligible ledger produces the same interval.

## Historical replay

`scenario-replay` is a separate audit cohort. It uses a completed pre-event
bar, the first provider quote after the timestamp, only later regular-session
bars, explicit two-fill slippage, and sourced SPY bars over the same replay
horizon. Any entry/exit or stop/target ordering that cannot be known within one
OHLC bar is quarantined with no eligible return.
It is a timestamp-proxy study, not a forward performance claim.

## Required runtime environment

Place these in the private runtime file
`C:\r\dawnstrike-state\secrets\runtime.env`, never Vercel and never Git:

```text
OPENAI_API_KEY=your_openai_api_key
DAWNSTRIKE_SCENARIO_INTELLIGENCE_ENABLED=true
DAWNSTRIKE_OPENAI_MODEL=gpt-5.6-terra
```

Alpaca credentials remain the existing `ALPACA_API_KEY_ID` and
`ALPACA_API_SECRET_KEY` values in the same file. Optional bounded controls are
`DAWNSTRIKE_SCENARIO_OPENAI_TIMEOUT_SECONDS=45`,
`DAWNSTRIKE_SCENARIO_MAX_ARTICLES_PER_RUN=20`, and
`DAWNSTRIKE_SCENARIO_ARTICLE_MAX_CHARS=12000`.

## Operator commands

```powershell
py -m intraday_scanner.cli scenario-doctor --db-path C:\r\dawnstrike-state\shadow_real.sqlite
py -m intraday_scanner.cli scenario-cycle --db-path C:\r\dawnstrike-state\shadow_real.sqlite --symbols NVDA,TSLA
py -m intraday_scanner.cli scenario-replay --db-path C:\r\dawnstrike-state\shadow_real.sqlite --symbols NVDA,TSLA --start 2026-07-01T00:00:00Z --end 2026-08-01T23:59:59Z
```

The scheduled morning and monitor runners invoke the forward cycle only when
the feature flag is true. Daily finalization closes open scenario paper
positions, reconciles the forward ledger, rebuilds `scenarios.json`, and
publishes it with the static dashboard.
