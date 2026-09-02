# Scenario Intelligence Operations

Scenario Intelligence is a research-only, paper-audit pipeline. It cannot place
broker orders. Alpaca supplies news and price observations; OpenAI is restricted
to schema-validated factual claim extraction. Deterministic code owns all
research actions, levels, vetoes, and paper lifecycle decisions.

## Runtime environment

Set these values in `C:\r\dawnstrike-state\secrets\runtime.env` on the
Windows runtime host. Do not put provider keys in Vercel: Vercel receives only
the sanitized static publication after daily readiness succeeds.

```text
ALPACA_API_KEY_ID=<your Alpaca key id>
ALPACA_API_SECRET_KEY=<your Alpaca secret>
OPENAI_API_KEY=<your OpenAI API key>
DAWNSTRIKE_SCENARIO_INTELLIGENCE_ENABLED=true
DAWNSTRIKE_OPENAI_MODEL=gpt-5.6-terra
INTRADAY_NOTIFIER_CHANNELS=telegram
TELEGRAM_BOT_TOKEN=<existing bot token>
TELEGRAM_CHAT_ID=<existing chat id>
```

Optional bounded-cost controls:

```text
DAWNSTRIKE_SCENARIO_MAX_ARTICLES_PER_RUN=20
DAWNSTRIKE_SCENARIO_ARTICLE_MAX_CHARS=12000
DAWNSTRIKE_SCENARIO_OPENAI_TIMEOUT_SECONDS=45
```

## Required evidence path

1. `scenario-doctor` must report `READY` without printing credentials.
2. Morning and five-minute monitor jobs run `scenario-monitor` for the bounded
   premarket research universe, even when deterministic AlphaOps produces zero
   final picks. The task ledger records the optional `scenario_intelligence`
   stage, while each Scenario run receipt records article, cache-hit, and actual
   model-request counts. With zero articles, zero model requests is the correct
   result; the system must never manufacture content merely to call a model.
3. Entered paper scenarios use the shared paper watcher. Its durable outbox sends
   only deduplicated entry and exit Telegram notices.
4. EOD runs `scenario-close` and `scenario-finalize`. Reported returns exclude
   untriggered, open, missing, and quarantined outcomes. A closed position is
   still ineligible unless its entry and exit fills, named cost model and cost
   values, stable lifecycle identities, and same-horizon sourced SPY bars are
   complete. Missing SPY evidence leaves every return metric for that row null.
5. `build_public.py` binds `scenarios.json` and its manifest into the same atomic
   publication set as performance and calendar data. A degraded artifact must
   not be deployed or promoted.

## Operator checks

Direct ambient-Python commands are local observational diagnostics only and
must not be used as production acceptance evidence. Authoritative production
proof comes from the exact-SHA activation/scheduled runner using the protected
interpreter after importing `C:\r\dawnstrike-state\secrets\runtime.env`, plus
the same-date finalizer and public-artifact receipts. A missing OpenAI key,
disabled feature flag, uncalibrated state, or unavailable provider remains
`NOT_READY`; do not substitute a developer-shell result.

The dashboard's Scenario Intelligence view separates forward paper performance
from historical provider-timestamp replay. `UNCALIBRATED` is a withholding
state, not a probability or an investment recommendation.

OpenAI does not select AlphaOps picks. It only extracts schema-validated factual
claims from sourced articles. Deterministic code owns every action and may still
abstain after a successful model request when price, liquidity, source, or
point-in-time evidence is missing.
