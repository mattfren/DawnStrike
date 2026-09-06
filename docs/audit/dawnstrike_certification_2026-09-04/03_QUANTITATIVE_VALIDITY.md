# 03 — Quantitative validity

Commit `14ca714f`. Verdict B evidence. Every claim below was established by reading the code at the
cited lines and, where stated, by executing it.

## Summary

Dawnstrike contains one genuinely excellent quantitative component and does not use it in
production. The correctness problems that matter are not arithmetic bugs in the live path — they are
**fidelity and default-selection problems**: production measures the wrong thing (a polled spot
price instead of a bar path), and the offline audit's default entry rule does not match the
strategy's own stated entry condition.

## 1. Timing integrity — `PASS` on the paths audited

- **Position engine refuses lookahead explicitly.**
  `v2/paper_ops/position_management.py:143-144`:
  `if bar.timestamp < opened_at: raise ValueError("position policy cannot inspect a bar before the open")`.
  A hard refusal, not a silent skip.
- **Paper-audit eligibility is causal.** `services/audit_service.py:248-259` retains only bars at or
  after both `config.signal_time` and the candidate's recommendation timestamp, so an entry cannot
  precede its signal.
- **Premarket bar reconciliation enforces the session window.**
  `services/alpha_cycle_service.py:3560-3567` requires every raw bar to fall on the decision session
  date between 04:00 and 09:29 ET, be strictly ordered, and satisfy
  `bar_time + 1min <= requested_at` — i.e. only *completed* bars count. Partially formed bars cannot
  masquerade as complete.
- **Signal, alert, entry and outcome timestamps are distinguishable.** `historical_signals` carries
  `generated_at`, `market_date`; receipts carry `expected_at`, `observed_at`, `requested_at`,
  `bar_completed_at` as separate fields.

I found no lookahead defect on the paths I audited. I did **not** complete an adversarial leakage
probe of the V6 dataset/label builders — see *Not established* below.

## 2. Fill methodology — the decisive dimension

Full evidence: `evidence/fill_methodology.md`.

Three simulation paths exist with different fidelity:

| Path | Entry rule | Path model | Production? |
|---|---|---|---|
| `services/audit_service.py` | `open` **(default)** or `breakout` | intraday bars | CLI only |
| `v2/paper_ops/position_management.py` | policy-driven | full OHLC path | **No caller** |
| `services/trade_watcher_service.py` | breakout confirmation | **polled spot price** | **Yes** |

### 2a. Historical concern #10 — CONFIRMED

The mission asked whether paper audit uses "the first eligible open rather than the strategy's
actual breakout-fill condition." **It does, by default.**

`audit_service.py:151-164`; default `open` at `config.py:70`, `config.py:302`, `paper_audit.py:19`,
`.env.example`. Under the default, the simulation enters at the first eligible bar's open regardless
of whether the trigger was ever touched — filling trades the strategy would never take and deleting
the confirmation selection effect. The product's own stored rule reads
`"Watch only if price confirms above 1.809."`

When `breakout` **is** selected the implementation is correct and conservative: first bar whose
*high* reaches the trigger, filled at `max(trigger, bar.open)` so a gap-up fills at the worse open;
never touched → `_no_entry_trade`, no forced fill.

Residual: `_breakout_entry_index` returns `0` when `trigger <= 0` (`audit_service.py:332-333`),
silently degrading breakout mode to open mode.

### 2b. Same-bar ambiguity — RESOLVED CORRECTLY (historical concern disproved)

`position_management.py:140-205` handles every ambiguous case properly:

- stop gap-through checked **first**, exiting at `bar.open` (the realistic worse price);
- stop evaluated **before** target within the same bar — `same_bar_policy = "stop_first_conservative"`;
- target gap-through exits at `bar.open`; target touch exits at the level;
- trailing stop between the two.

The historical claim that one-minute OHLC leaves stop/target ordering unresolvable is **false at
this commit** for this engine. It is resolved, conservatively, which is the correct engineering answer.

### 2c. Production runs the lower-fidelity model — `FAIL` for path accuracy

`evaluate_position_management` has zero non-test callers outside `v2/paper_ops`;
`trade_watcher_service.py` never references it.

Production entry (`~1749-1790`) is genuinely conservative — it requires `price >= trigger`, stands
down `already_invalidated` if price is already through the stop, stands down `already_extended` if
price is already at target (refusing late entry), and enforces a reward/risk floor. Exit
(`_exit_decision`, `1838-1877`) checks stop before target, then an EOD flatten.

The flaw is the **input**: both consume one scalar `price` from a five-minute observation bundle.
Therefore production cannot see a stop touched and recovered between polls, cannot see a target
touched between polls, and fills at the poll price rather than the level. For sub-$25 premarket
gappers, intrabar excursion across five minutes is large.

**Results from this path must never be presented as path-accurate backtest results.**

## 3. Market realism — `PARTIAL`

Present and versioned: `alphaops-v5-cost-model-50bps-0.005ps` (50 bps per side plus per-share),
`slippage_bps` applied in `audit_service.py:166`, fee/slippage/borrow columns in the ledger, a
`slippage_stress_multiplier >= 1.50` floor in challenger gating, halt tracking, and short-borrow
verification flags.

Not established: NBBO/queue position, market impact, latency, or attainable-fill evidence. The cost
model is a *modelled* deduction, not evidence of an obtainable fill. With zero completed trades,
none of it has ever been exercised.

## 4. Statistical validity — cannot be exercised; controls look strong on paper

**There is no sample.** 25 forward days, 0 trades; every outcome/label/evaluation table empty. So
win rate, expectancy, profit factor, drawdown, Sharpe, calibration and significance are all
undefined — not weak, undefined.

The *gating* code is unusually strict where I read it: `challenger_evaluation.py:106-139` raises if
`min_forward_sessions < 60`, `min_closed_trades < 100`, `min_coverage_pct < 98.0`,
`slippage_stress_multiplier < 1.50`; the untouched holdout is frozen at registration
(`:1843-1880`) and cannot be reselected on retry; forward and replay cohorts are kept in separate
fields.

**Evidence hierarchy is correctly separated in the schema** — `cohort` distinguishes
`official_forward_paper` from `alphaops_signal_research`; `evidence_mode` records
`forward_observed`; `evidence_state` distinguishes `missing` / `pending` / `no_trade`. Fixture,
in-sample, out-of-sample, forward and live are not merged. This is a real strength.

## 5. Expected return and confidence provenance — one confirmed defect, one corrected claim

`intraday_scanner/expectancy.py`. Executed with **zero** audit history:

```
sample_size = 0   effective_sample_size = 0.0   model_basis = "score prior only"
expected_return_pct = 4.99      confidence_pct = 18.0     tier = exploratory
lower..upper = -14.88 .. 24.86
win_probability_pct = 100.0
```

**`win_probability_pct = 100.0` derived from zero evidence, alongside a −14.88% lower bound, is
internally contradictory and wrong.** All prior weight sits on a positive heuristic prior, so the
"probability" is definitionally 100%.

**Correcting the severity claim:** this value is **never rendered**. It is absent from
`EXPECTANCY_COLUMNS` (`app.py:113-121`) and from both per-ticker render sites (`app.py:2496-2508`,
`app.py:4460-4485`). The defect is latent, not user-facing. It should still be removed or renamed,
because a stored field named `win_probability_pct` will eventually be displayed by someone.

**What a user does see with zero evidence** is honest in the table and thin on the card:

- table shows `expected_return_pct`, `confidence_pct`, the band, and an explanation reading
  *"Basis: score prior only; tier: exploratory. Run paper audits for this scanner output."*
- the setup card (`app.py:4460-4485`) shows a bare **"Expected +4.99%"** with **no** basis, tier or
  confidence qualifier.

That card is the product-truth gap worth fixing: the qualifier exists and simply is not carried onto
the surface where the number is most prominent.

## 6. Reproducibility — `PARTIAL`

Scan scoring is deterministic. Two runs over the documented sample produced byte-identical
`ranked_candidates.csv`, `top_explosive.csv` and `avoid_list.csv`; `scan_summary.json` differed only
in `run_id` and `created_at`.

One defect: `config_hash` (`scoring.py:407-409`) hashes `config.public_dict()`, which includes
`database_path` and `output_dir`. Two runs differing only in `--out-dir` produce different hashes
(`e464382b3509` vs `2e59adc5acdf`); identical paths reproduce it exactly (`1885ac5b81d9` twice). It
is therefore **path-sensitive, not nondeterministic** — but it cannot be compared across machines or
output directories, which defeats its provenance purpose. Independently corroborated by a second
reviewer (`SCAN-06`).

## 7. Valid and invalid claims today

**Valid:**
- "Deterministic, explainable premarket ranking with an explicit avoid list."
- "Every missing observation is recorded as missing and never as zero."
- "No trade was taken on N of N observed sessions."

**Invalid — must not be stated:**
- Any win rate, expectancy, profit factor, Sharpe, drawdown or return figure.
- "Validated", "proven", "backtested edge", or any variant.
- Any claim that production paper results are path-accurate.
- Any target-hit statistic derived from the polled model.

## Not established (declared, not glossed)

- **V6 leakage probe:** I did not complete an adversarial lookahead/leakage probe of
  `alpha/v6/dataset_builder.py`, `label_builder.py` or `calibration.py`. The dedicated subagent pass
  did not finish. Status `UNKNOWN`; all V6 tables are empty, so nothing is currently at risk, but
  this must be closed before any V6 promotion.
- **Metric arithmetic:** 12 statistical findings were raised with cited lines. I verified two of them
  myself (below); the remaining ten did not complete adversarial verification and remain
  **hypotheses, not findings** — deflated-Sharpe unit mismatch, multiple-testing haircut ~16× too
  small, missing minimum-sample gate in `evaluate_return_predictions`, profit-factor sentinel
  1,000,000, 2-of-3 walk-forward sign test, `annualized_observation_sharpe` over traded days only.
  Both hypotheses I *did* check were real **but had their severity overstated**, so treat the
  remainder as plausible and unconfirmed.

## 8. Two metric defects confirmed by execution

### DS-016 — Sortino divides by dispersion among losses, not downside deviation

`v2/backtest/engine.py:537-538`:

```python
downside = [value for value in daily_returns if value < 0]
downside_vol = pstdev(downside) * math.sqrt(252) if len(downside) >= 2 else 0.0
```

`pstdev(downside)` is the spread of the losses **about their own mean**, not the root-mean-square of
negative deviations from the target across all periods. Executed on a realistic stop-loss series
(losses clustered near −1R):

| | engine | textbook |
|---|---:|---:|
| downside vol | 0.0011225 | 0.10040171 |
| Sortino | **4714.49** | 52.71 |

**89× overstated** — and the failure mode is perverse: the *tighter* the loss clustering, the smaller
`pstdev(downside)` and the *larger* the reported Sortino. Perfectly consistent losses give
`pstdev = 0`, so Sortino returns `None`. **The metric degrades precisely as risk control improves.**

### DS-017 — `sqrt(252)` hardcoded over per-bar returns

`daily_returns = _equity_returns(equity_curve)` are per-**equity-point** (per-bar) returns despite
the name, yet `volatility = pstdev(daily_returns) * math.sqrt(252)` and
`sharpe = (average_daily_return * 252) / volatility` hardcode 252 with no timeframe parameter. The
ratio reduces to `avg_per_bar · √252 / pstdev_per_bar`; per-bar data with N bars/day requires
`√(252·N)`. Feeding 1-minute bars therefore **understates** Sharpe by √390 ≈ 19.7×.

`_annualized_return` (`:795-806`) uses `days = max((end - start).days, 1)` over 365.25 with no
minimum-window guard, so a one-day backtest compounds to an absurd CAGR — which `calmar` inherits.

### Impact is bounded — and this is why verification mattered

Neither `sortino` nor `sharpe` appears anywhere in `challenger_evaluation.py` or
`promotion_policy.py`. **No promotion gate consumes them**; promotion uses profit factor, coverage,
session counts and drawdown. They are reachable and displayable through the
`strategy-challenger-backtest` CLI, so a user could read a wrong number — but no strategy is promoted
on the strength of one. The newer `v2/backtest/intraday_engine.py` does **not** repeat either defect.

Severity is therefore **MEDIUM**, not the HIGH originally assigned.
