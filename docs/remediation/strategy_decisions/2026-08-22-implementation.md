# Strategy decision receipts — 2026-08-22

This change adds an additive, research-only evidence layer for the nine
strategy manifests. It does not replace the existing AlphaOps selection path.
The default is `DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED=false` and
`DAWNSTRIKE_STRATEGY_EVIDENCE_SHADOW_ONLY=true`; when shadow mode is enabled,
receipts are computed and persisted while legacy selection remains unchanged.

## Registry and policy

Every manifest includes the common `HARD_MARKET` conditions (`valid_symbol`,
point-in-time OHLCV, positive price and volume, source identity/freshness, and
source conflict checks), common `HARD_RISK` conditions (halt, entry/stop/target
geometry, 1.50R, risk budget, and spread), and advisory float, secondary source,
historical sample, and catalyst conditions. Strategy-specific core and
contextual conditions are defined in `condition_registry.py` for:

1. `ts_momentum_sma_atr` — trend, extension, volatility, offering/dilution,
   corporate action, adverse event.
2. `donchian_breakout_20_10` — breakout quality, extension, participation,
   volatility, catalyst, filing risk, offering/dilution.
3. `cross_sectional_relative_strength` — rank membership/margin, sector
   concentration, identity, and sector/industry. Unresolved sector is one
   explicit `UNKNOWN` bucket and remains conditional.
4. `pullback_reclaim_uptrend` — slope, waterfall, reclaim, adverse event,
   offering/dilution, regulatory event.
5. `volatility_contraction_breakout` — participation, dead liquidity, regime,
   contraction breakout, earnings, regulatory event, catalyst timing.
6. `failed_breakout_reversal_short` — rejection, squeeze, failed-breakout
   confirmation, borrow/locate, offering/dilution, squeeze event, corporate action.
7. `bullish_fvg_continuation` — gap, participation, trend, explicit daily-OHLC
   proxy disclosure, intraday microstructure, catalyst, offering/dilution.
8. `gap_up_continuation` — gap, close location, trend, participation, data
   quality, corporate-action basis, catalyst, offering/dilution.
9. `gap_up_continuation_atr` — the gap-up manifest plus ATR normalization and
   volatility-event context.

The deterministic policy preserves the existing 1.50R floor and score threshold.
Hard market, hard risk, and core failures block. Advisory gaps produce
`PICK_WITH_DISCLOSED_GAPS`; execution-only gaps produce `CONDITIONAL_PICK` and
keep paper-entry eligibility false. A confirmed corporate-action failure is
blocked, while unresolved corporate-action basis remains conditional. Broker
execution is always false.

## AI and source trust

`StrategyGapResolver` uses the existing OpenAI Responses pattern with
`web_search`, strict JSON-schema output, `store=False`, `max_retries=0`, bounded
tool calls, and actual response-model identity. It permits only contextual claim
types, requires cited URLs and point-in-time publication timestamps, hashes the
citation identity, enforces ticker matching, rejects prompt injection and all
forbidden market/trade fields, preserves contradictions/unknowns, and degrades
provider failures to `MISSING_DISCLOSED`. AI cannot provide price, volume, VWAP,
spread, float, entry, stop, target, reward/risk, return, probability, sizing, or
a recommendation.

## Persistence and operator surfaces

Migration 31 is forward-only and creates immutable receipt, condition-result,
claim, and resolution-run tables. Canonical JSON and SHA-256 identity are
stored; duplicate receipt reuse requires an exact payload match. Prior signal
and performance rows are not rewritten. Telegram includes tier, receipt ID,
disclosed gaps, and entry-confirmation state when receipt fields are present.
Daily learning accepts receipts as evidence inventory and produces unapplied,
research-only condition summaries; it never changes policy or promotes a
strategy automatically.

## Verification and boundaries

Focused receipt/resolver/registry/integration/operator tests pass. The full
suite, exact pushed-SHA CI, stacked PR URL, and final commit SHA are recorded in
`2026-08-22-proof.json` after verification. No runtime, state database,
scheduler, deployment, production alias, merge, or broker route was touched.
Missing truth was never converted to zero, false, safe, or passed.
