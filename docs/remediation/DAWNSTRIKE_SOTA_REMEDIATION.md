# Dawnstrike — Deep Audit and State-of-the-Art Remediation Plan

Date: 2026-08-09
Scope: full repository + `data/shadow_real.sqlite` forensics + backtest artifacts
Method: evidence only. Every claim below is traceable to a file, a row count, or a query.

---

## Executive summary

Dawnstrike is **excellent software wrapped around an unvalidated strategy**.

The engineering is genuinely strong — arguably institutional-grade in its discipline around
truth, provenance, and refusal-to-lie. The trading system inside it has never been validated,
measures its own outcomes on the wrong timeframe, and sizes risk with two mutually
contradictory formulas, neither derived from volatility.

The realized paper record is 8 round-trips, 25% win rate, **-$487.61 on $1,000 fixed notional
per trade**, with a single -47.3% loss caused by a stop placed 47% below entry on a
fixed-dollar position.

The good news: the reason it isn't working is not "the model needs tuning." It is four
concrete, fixable structural defects, and the infrastructure to fix them **already exists in
this repository, unused**. Roughly 60% of the remediation is wiring existing high-quality
components to the production path rather than writing new ones.

**The single highest-leverage change is Phase 1 (intraday bar spine).** Nothing downstream —
learning, attribution, ML, or honest backtesting — is possible without it. Everything else in
this plan is gated behind it.

---

# PART I — AUDIT

## What is genuinely strong (protect this)

These are not consolation prizes. They are the reason this project is salvageable in weeks
rather than rebuilt in months.

| Asset | Location | Why it matters |
|---|---|---|
| Causal, stop-first position policy | `intraday_scanner/v2/paper_ops/position_management.py` | Correct same-bar conservatism, gap-through handling, SHA-fingerprinted policies. Production-grade. **Currently unused by live trades.** |
| Leakage-safe walk-forward ML | `intraday_scanner/alpha/v6/` | Frozen artifacts, evidence-gated complexity, no-lookahead tests. Better than most funded shops. **Never trained — 0 rows in all 19 tables.** |
| No-lookahead indicator library | `intraday_scanner/v2/indicators/core.py` | Explicit causal SMA/ATR/RSI/vol. **ATR exists and is never used for stop placement.** |
| Real backtest metrics | `intraday_scanner/v2/backtest/engine.py` | Sharpe, Calmar, Sortino, profit factor, R-multiples, modeled slippage. |
| Truth discipline | run contracts, `data_ineligible`, "pending ≠ zero", insufficient-sample labeling | Rare and valuable. **The system refuses to fabricate performance.** Keep this culture absolutely intact. |
| Engineering hygiene | 128 test files, mypy, ruff, secrets baseline, migrations, idempotent retries, run manifests | Real CI discipline. |

## The seven defects

### F1 — The strategy that trades has never been backtested; the strategies backtested are never traded

The v2 research lab holds 9 strategies. **All nine are `compatible_timeframe: "1d"`**
(`intraday_scanner/v2/strategies/catalog.py`), with multi-day holds — `end_of_day_behavior:
"hold"`, `timeout_trading_sessions` 3–10 (`position_management.py:253-321`).

Production trades `alphaops_v4` — an *intraday* premarket gapper. Zero overlap.

Backtested results (`outputs/audit/omega_alpha_producer_preview_20260713/backtests/`):

| Strategy | Trades | Sharpe | Profit factor | Return |
|---|---|---|---|---|
| volatility_contraction_breakout | 25 | 0.94 | 2.46 | +15.4% |
| donchian_breakout_20_10 | 60 | 0.91 | 1.69 | +18.8% |
| cross_sectional_relative_strength | 33 | 0.71 | 1.86 | +12.4% |
| ts_momentum_sma_atr | 128 | 0.40 | 1.16 | +8.4% |
| bullish_fvg_continuation | 92 | 0.51 | 1.29 | +10.8% |
| pullback_reclaim_uptrend | 66 | -0.49 | 0.76 | -5.1% |
| failed_breakout_reversal_short | 195 | -0.80 | 0.76 | -9.3% |

Even the best of these is a 25-trade daily swing system — not statistically meaningful, and
not what the platform trades. **`alphaops_v4` went to paper trading with zero prior evidence
of any kind.**

Additionally, `engine.py:533-538` hardcodes `math.sqrt(252)` and treats every bar as one day.
Feeding it 1-minute bars silently produces Sharpe figures wrong by a factor of ~√390.

### F2 — Outcome truth is measured on DAILY bars. This is fatal for day trading.

The outcome resolver is `fetch_yahoo_chart_daily_dataset(range_period="2y", interval="1d")`
(`intraday_scanner/public_data/yahoo_chart_fetcher.py:23-28`).

Consequences, all confirmed in data:

- **Stop-vs-target ordering is unresolvable.** With only a daily high and low you cannot know
  which was touched first. Path dependency *is* day trading.
- `max_favorable_excursion` and `max_adverse_excursion` are **NULL on all 8 positions**.
- `signal_outcomes.high_after_entry` / `low_after_entry` are the *day's* high/low, not
  post-entry extremes.
- `halt_events`: **0 rows**, while trading halt-prone sub-$5 names.

**"Determine exactly why we lost" is currently structurally impossible.** Not hard — impossible.
The data to answer it is never collected.

### F3 — Risk geometry is arbitrary, with two contradictory models

Across 218 signals carrying all three levels, reward:risk collapses into exactly two clusters:

```
R:R = 0.875  → 165 signals   (0.873–0.880)
R:R = 1.500  →  53 signals   (exact)
```

**Cluster A (R:R 0.875)** comes from the premarket-range extension in `scoring.py:68-86`.
An 0.875 R:R needs a **53.3% win rate just to break even before costs** — and roughly 57%+
after a realistic 200bps round trip on this instrument class. It is structurally
negative-expectancy by construction.

**Cluster B (R:R exactly 1.500)** is risk-derived — the target is `entry + 1.5 × (entry − stop)`.
This **directly contradicts** `"target_derived_from_risk": False` written into the same payload
at `scoring.py:115`. One of these two is lying to the learning layer.

Stop distances, by grade:

```
grade C:  n=37   min 15.5%   median 40.7%   max 73.7%
grade D:  n=181  min  2.0%   median  2.0%   max 79.4%
```

- Grade D's min = median = **2.0% is a hardcoded clamp**, not market structure. On a stock
  gapping 40%, a 2% stop is inside the noise band — a guaranteed stop-out.
- Grade C's median 40.7% stop is not a stop. It is a hope.
- **No ATR appears anywhere in level construction**, despite a working implementation sitting
  in `v2/indicators/core.py`.

The worst case, fully traced:

```
BIYA  2026-07-20
  entry     $9.39675   14:44:24Z
  stop      $4.9939    (-46.9% from entry)
  raw exit  $4.9805    16:00:02Z   (0.27% through the stop — normal gap-through)
  fill      $4.955598  (after 50bps modeled slippage)
  P&L       -$472.63 on $1,000 notional  (-47.3%)
```

Note what this trace **exonerates**: stop *execution* was correct. The exit printed only 0.27%
through the stop level, which is ordinary intra-session gap-through and exactly what a
conservative stop-first policy should produce. The exit machinery is not the defect.

The defect is entirely **stop placement**: a 46.9% stop is not risk management. Combined with
**fixed $1,000 notional sizing** rather than fixed-fractional risk, a single trade was permitted
to risk 47% of its unit. With
a 47% stop, one trade risks 47% of the unit. Correct sizing is
`qty = (equity × risk_pct) / (entry − stop)`, which would have sized BIYA to ~1/20th of what
was taken.

### F4 — The catalyst engine is 100% non-functional, and catalyst is the entire edge

**All 218 signals carry `catalyst:no_clear_catalyst`.** Every one. Including rank-1 picks on
`extreme_gap`.

In gap-and-go, the catalyst *is* the distribution. A 40% gap on an S-3 shelf takedown and a
40% gap on PDUFA approval are opposite trades. Trading them identically — which is what a
universal `no_clear_catalyst` label forces — makes the system a coin flip carrying a
negative-expectancy R:R.

The setup taxonomy therefore degenerates: 14 distinct `primary_setup` values, all of the form
`grade:{C,D}|gap:*|volume:*|catalyst:no_clear_catalyst`. **No A or B grade has ever been
produced.**

### F5 — The universe is adversarially selected

From `.env.example`:

```
INTRADAY_MIN_PRICE=0.5
INTRADAY_MAX_PRICE=25
INTRADAY_IDEAL_GAP_LOW_PCT=35
INTRADAY_IDEAL_GAP_HIGH_PCT=140
INTRADAY_MIN_PREMARKET_DOLLAR_VOLUME=500000
```

This targets sub-$25, low-float names gapping 35–140%. That cohort is dominated by dilution
events, shelf takedowns, and promotional activity — the segment most structurally hostile to
an uninformed buyer. Empirically: 25% win rate over 8 trades.

This is *not* an argument to abandon the universe. It is an argument that **in this universe,
catalyst classification is not a feature — it is the whole strategy** (see Phase 4).

### F6 — The learning loop captures ~2% of its data. The ML has never run.

```
historical_signals   234    (28 market days)
signal_outcomes       10
  └─ complete_sourced  5    ← the only usable labels
  └─ not_triggered     5
alpha_outcome_labels   0
alpha_v6_* (19 tables) 0    ← every one
```

**A 2.1% outcome capture rate.** The v6 gates require 100 labels for the linear model and
500 labels + 60 distinct dates for gradient boosting (`alpha/v6/models.py:8-10`). At the
current rate of ~0.18 labels/day, gradient boosting arrives in roughly **ten years**.

64 of the 111 tables in `shadow_real.sqlite` are empty. The adaptive architecture is complete
and bloodless.

### F7 — Costs are modeled optimistically for the instrument class

`INTRADAY_SLIPPAGE_BPS=50` per side. On sub-$5 low-float names in the opening minutes,
effective spread plus impact routinely runs 100–300bps per side. Also absent: borrow cost and
locate for shorts, partial fills, queue position, and LULD halt handling.

A move from $9.40 to $4.96 in 75 minutes (BIYA) would in reality trip multiple LULD halts. The
modeled exit price was very likely unobtainable.

## Housekeeping (low severity, non-blocking)

- `app.py` is a 172KB monolith.
- 20 stray `.sqlite` files in `data/`; ~15 `.pytest_*_tmp_*` directories in the repo root.
- `intraday_scanner/notifications/` and `intraday_scanner/notifiers/` are duplicate packages.
- No market-data provider is configured — only `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are
  set in `.env`. The entire data spine is unauthenticated public web scraping.
- `source_confidence` is computed as `max(20, 100 - optional_missing * 10)`
  (`providers/public_table_provider.py:466`). That is field *completeness*, not confidence.
  It carries no information about correctness, yet is consumed as a quality signal.

---

# PART II — REMEDIATION PLAN

## Design principle

The current system **trades first and learns never**. Every phase below is gated so that
becomes impossible: no strategy may allocate capital — paper or real — until it has produced
evidence under realistic costs.

Phases 1 and 2 are strictly sequential and block everything. Phases 3–5 can partially overlap.

---

### Phase 0 — Stop the bleeding (Days 1–2)

1. **Halt `alphaops_v4` paper allocation.** It has no evidence and negative realized expectancy.
   Keep signal *generation* running — the signals are the training data.
2. **Freeze the baseline.** Snapshot `shadow_real.sqlite` to an immutable, hashed archive.
3. **Quarantine the 8 trades.** Tag `cohort = 'pre_evidence_v0'`; exclude from all forward
   statistics permanently. Do not delete — they are the control group.
4. **Fix the contradiction in `scoring.py`.** Either the target is risk-derived or it is
   range-derived. Pick one and make the payload flag true. A learning system fed a mislabeled
   feature learns the wrong thing.

**Gate:** no capital allocation of any kind until Phase 5 clears.

---

### Phase 1 — The intraday bar spine (Weeks 1–3) — THE unlock

This is the highest-leverage work in the plan. Everything downstream depends on it.

**Replace daily Yahoo bars with true intraday bars including extended hours.**

Provider recommendation, in order:

| Provider | Cost | Why |
|---|---|---|
| **Polygon.io Stocks Starter** | ~$29–79/mo | Full-market 1m aggregates + trades/quotes, 2yr+ history, extended hours. `POLYGON_API_KEY` slot already exists. **Best fit.** |
| Databento | usage-based | True tick/MBO if you later need queue-position realism. |
| Alpaca (free IEX) | $0 | **Not adequate** — IEX is ~2% of consolidated volume. On low-float names the tape is nearly empty. |

Build `intraday_scanner/v2/data_truth/bar_spine.py`:

- Immutable, content-hashed, point-in-time 1-minute OHLCV **+ NBBO quotes**
- Session tagging: `pre` / `regular` / `post`
- LULD halt detection and halt-window flagging
- True spread at decision time (replaces the fictional flat 50bps)
- Backfill **2 years** across the gapper universe

**Gate:** every one of the 234 historical signals can be replayed minute-by-minute with
deterministic stop/target ordering. Prove it with a test that replays all 234 and asserts zero
ambiguous resolutions.

---

### Phase 2 — Path-aware outcome engine (Weeks 3–4)

With real intraday bars, reconstruct what actually happened.

For every signal, replay the path and record:

- **First-touch resolution** — stop vs. target, using the conservative same-bar policy that
  already exists at `position_management.py:40` (`stop_first_conservative`)
- **MFE/MAE curves** at 1-minute granularity (fills the NULLs)
- **Time-to-target / time-to-stop**
- **Counterfactual exits** — what N alternative exit policies would have returned on the same
  path (this is the deterministic improvement engine you asked for)

Then **backfill all 234 historical signals**.

This single step takes the label count from **5 → ~234**, which clears the v6 linear-model gate
of 100 immediately.

**Gate:** ≥200 path-resolved labels with non-null MFE/MAE.

---

### Phase 3 — Real risk geometry (Weeks 4–5)

Delete both broken level models. Replace with volatility-normalized levels.

```python
# Levels anchored to measured volatility and real structure
atr5m   = atr(bars_5m, period=14)              # v2/indicators/core.py — already exists
stop    = max(
    entry - k_regime * atr5m,                  # volatility floor
    opening_range_low,                          # structural floor
    vwap * (1 - buffer),
)
stop    = clamp(stop, max_distance_pct=8.0)    # hard cap, already in radar config

# Targets from the MEASURED MFE distribution per setup cluster (Phase 2 output),
# not a constant multiple.
target  = mfe_quantile(setup_cluster, q=0.60)

# Fixed-fractional sizing — NOT fixed notional
qty     = (equity * risk_pct) / (entry - stop)
```

Additional hard controls:

- Max single-trade loss: **1R**, enforced at the sizing layer
- Daily loss limit → auto-flat and stand down
- Halt-aware exit logic (no fills assumed inside a LULD halt)
- Realistic cost model: spread-derived slippage from actual NBBO, not a flat constant

**Gate:** no signal ships without ATR-derived levels and a post-cost expectancy that survives
a 200bps round trip.

---

### Phase 4 — The catalyst engine (Weeks 5–7) — where the real edge lives

This is the genuine innovation, and it directly targets the 100%-`no_clear_catalyst` failure.

**Classify at the filing level, not the headline level.** `providers/sec_edgar_provider.py`
already exists as a foundation.

Ingest and structure:

- **SEC EDGAR full-text + form type**: S-1, S-3, **424B5 / 424B4** (offering pricing), 8-K with
  item codes, 13D/G
- **Shelf capacity remaining** and ATM program presence
- **Warrant overhang** and recent reverse-split history

> **The core insight: an S-3 or 424B5 filed within 72 hours of a gap is the strongest
> avoid/fade signal available in this universe.** That is the dilution tell, it is public, it
> is machine-readable, and it is almost never surfaced by retail tools — which show you the gap
> and not the offering that funded it.

Build a catalyst taxonomy with *measured* conditional distributions from Phase 2 labels:

`offering` · `reverse_split` · `fda_pdufa` · `earnings` · `contract_award` · `index_add` ·
`short_squeeze_mechanics` · `no_news_momentum`

**Use the LLM layer for extraction, not prediction.** `ai/headline_classifier.py` and
`ai/scenario_claim_extractor.py` already exist — point them at turning filings and headlines
into *structured claims* (dollar amount, share count, pricing, dilution %). The prediction
stays deterministic and auditable. This preserves your truth discipline while adding real
comprehension.

**Gate:** <10% of signals labeled `no_clear_catalyst`, with per-category outcome
distributions measurably distinct.

---

### Phase 5 — Backtest the strategy you actually trade (Weeks 7–9)

1. Port the intraday gapper into `v2/strategies/` as a first-class, versioned, fingerprinted
   strategy with `compatible_timeframe: "1m"`.
2. **Fix `v2/backtest/engine.py`** — parameterize the `sqrt(252)` annualization by timeframe.
   As written it will silently produce wrong Sharpe on intraday bars.
3. Walk-forward across 2 years with spread-derived costs.
4. Benchmarks that must be beaten:
   - cash
   - buy-and-hold the same universe
   - **the inverse of your own signal** — if systematically fading your picks is profitable,
     that is the single most valuable discovery available, and current evidence (25% win rate)
     makes it a live hypothesis worth testing explicitly

**Promotion gate — all must pass:**

| Metric | Threshold |
|---|---|
| Out-of-sample trades | ≥ 200 |
| Profit factor (OOS) | > 1.3 |
| Sharpe (OOS) | > 1.0 |
| Max drawdown | < 15% |
| Expectancy after 200bps round trip | > 0 |

Only on a full pass does paper allocation resume.

---

### Phase 6 — Activate the v6 ML layer (Weeks 9–11)

Label counts now clear the gates. Keep the existing walk-forward and leakage discipline
unchanged — that part is already correct.

- Train on path-aware labels with the **catalyst taxonomy as the dominant feature block**
- Shadow-only until it beats the deterministic Phase 3/4 baseline out-of-sample
- Retain the existing promotion review and drift monitoring

**Gate:** ML may only override the deterministic baseline after beating it OOS across ≥2
walk-forward folds.

---

### Phase 7 — Deterministic post-mortem engine (Weeks 11–12)

This is the "determine exactly why, then get better" engine, and Phase 2 is what makes it
possible.

Every closed trade auto-decomposes into an attributed cause, written to a `loss_attribution`
table:

| Cause | Detection | Auto-remediation target |
|---|---|---|
| Signal error | feature vector on the wrong side of the trained boundary | scoring weights |
| Catalyst misread | offering discovered post-hoc in EDGAR | catalyst ingest latency / coverage |
| Level error | realized ATR vs. assumed ATR at entry | `k_regime` stop multiplier |
| Execution error | fill vs. modeled price, halt, spread | cost model, entry timing |
| Regime error | market-wide risk-off coincident | regime filter thresholds |
| **Variance** | trade was correct, outcome was tail | **nothing — do not tune** |

That last row matters most. The commonest way an adaptive trading system destroys itself is
overfitting to variance. Explicitly classifying "this was a good trade that lost" and
**forbidding a parameter update** is what makes the loop deterministic rather than reactive.

---

### Phase 8 — Broker integration (only after Phases 1–7)

A caution worth stating plainly before you build toward it: **Robinhood is a poor venue for
this strategy.** No official automation API (unofficial clients risk account lockout under
their ToS); PDT restrictions below $25k directly conflict with a day-trading loop; and fill
quality on low-float names is weak.

**Alpaca** (already integrated, `ALPACA_API_KEY_ID` slot exists) or **IBKR** are materially
better fits and would let you reuse the provider layer you have already written.

---

## Sequencing summary

```
Week  1  2  3  4  5  6  7  8  9 10 11 12
P0    ▓
P1    ▓▓▓▓▓▓▓▓
P2          ▓▓▓▓▓
P3             ▓▓▓▓▓
P4                ▓▓▓▓▓▓▓▓
P5                      ▓▓▓▓▓▓▓▓
P6                            ▓▓▓▓▓▓
P7                                 ▓▓▓▓▓
P8                                      → gated on all above
```

**Recurring cost:** ~$30–80/month (market data). Everything else is engineering time.

## Priority ranking by leverage

1. **Phase 1** — intraday bar spine. Blocks everything. Nothing else matters until done.
2. **Phase 4** — catalyst/dilution engine. The only durable, defensible edge in this plan.
3. **Phase 3** — ATR risk geometry + fixed-fractional sizing. Eliminates the -47% class of loss
   outright, and is the cheapest of the six to implement.
4. **Phase 2** — path-aware labels. Turns the learning loop on.
5. **Phase 5** — honest backtest. The gate that prevents repeating this.
6. **Phases 6–7** — compounding improvements, only valuable after 1–5.

## What to preserve at all costs

The truth discipline. `data_ineligible` contracts, "pending ≠ zero", insufficient-sample
labeling, no-trade decisions being first-class records. Most systems in this space fail because
they quietly flatter themselves. Yours refuses to. **That property is worth more than any model
in the repository — do not trade it away for a nicer-looking equity curve.**

## Honest framing

This plan addresses software correctness and evidence discipline — whether the platform
measures what it claims to measure. It cannot establish that any strategy will be profitable;
that is what Phase 5's gates exist to test, and a legitimate outcome of Phase 5 is discovering
that the current strategy has no edge. The gates are designed so you learn that cheaply, on
paper, from evidence — rather than expensively, later, with capital.
