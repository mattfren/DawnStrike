# 07 — Post-audit remediation and deployment record

The audit in `00`–`06` is frozen at its certification snapshot. This document
records everything done **after** that freeze, so the audit's integrity is
preserved and the later work is auditable on its own terms.

- Branch: `codex/dawnstrike-cert-remediation-20260904`
- Pull request: [#44](https://github.com/mattfren/DawnStrike/pull/44)
- Base: `origin/main` @ `14ca714f`

## Why more work was needed

The audit fixed the monitoring loop, but a separate question remained: even with
the loop running, **would it ever take a trade?**

It would not. All 16 live signals carried `can_alert = 0`, and
`trade_watcher._is_watchable_signal` rejects those outright, so `trade-watch`
would have seen nothing on any day. Eight gates failed on every signal, and
three of them were unsatisfiable by construction.

## Root causes and fixes

### 1. Halt and SEC feeds were disabled

`nasdaq_halts` and `sec_edgar` were `enabled: false` in the live
`web_sources.yaml`, while the alert gate **hard-blocks** any signal whose halt
and SEC status are not verified clear. The two feeds that can set those fields
were switched off, so the gate was unsatisfiable.

Both were verified working before enabling: halt RSS returned `status: success`;
SEC submissions returned HTTP 200 for every requested ticker.

*Live config change, backed up to `web_sources.yaml.bak-before-enable-feeds-20260905`.*

### 2. Float was never populated — a silent 14-point penalty

`formula._float_rotation_score` awards up to 14 of 100 points but returns a hard
`0.0` without `float_shares`. Nothing in the pipeline supplied float, so every
candidate silently forfeited those points and none could reach an A/B setup
grade. The best live candidate scored **69.03 against a B threshold of 70**.

New `providers/sec_float_provider.py` + `services/float_enrichment_service.py`
resolve share counts from SEC XBRL — a source already allowlisted and enabled,
requiring no new vendor or credential. It runs *before* `ScanService` reads the
snapshot, or the enrichment could not influence the grade it exists to inform.

Three honesty constraints are enforced:

- **Stale filings are refused.** Two issuers still return 2015 share counts;
  anything older than `MAX_FILING_AGE_DAYS` is discarded and the ticker keeps
  `unknown_float` rather than receiving a wrong number.
- **Never fabricates.** No SEC data leaves the row untouched.
- **Never overwrites** an operator-supplied float, which is more precise.

Shares outstanding ≥ free float, so rotation is *understated*: the proxy can
only under-score, never inflate. 11 of 16 live tickers resolve.

### 3. The configured universe was silently overridden

`_alphaops_scanner_config` replaced the operator's universe unconditionally:

| Setting | Configured | Hardcoded override |
|---|---:|---:|
| `max_price` | $25 | **$500** |
| `min_price` | $0.50 | $1.00 |
| `min_gap_pct` | 15% | **1%** |
| `ideal_gap_high_pct` | 140% | 25% |
| `max_credible_gap_pct` | 300% | **50%** |

That admitted SNOW ($377) and HPE (1.28% gap) and made the scoring model
incoherent: the admitted large caps rotated 0.01%–0.10% of their float and
earned nothing from a small-cap formula, while a real 93% gap was penalised for
exceeding a hardcoded 50% ceiling the operator's own config sets to 300%.

The configured universe is authoritative again; the liquid profile remains
available behind `DAWNSTRIKE_ALPHAOPS_LIQUID_UNIVERSE`.

**Operational consequence:** the live runtime carries no `INTRADAY_*` universe
variables, so it resolves to code defaults ($0.50–$25, gap ≥15%). Production's
candidate list narrows materially — 16 → 6 on 2026-09-03 data.

### 4. Stops spanned the entire premarket range

The stop was pinned to `premarket_low * 0.985`, producing a **35.4%** stop on a
93% gapper — outside the alert gate's own 15% policy and the mechanism behind
the worst recorded single loss.

The stop is now a fraction of observed premarket range, floored against noise
and capped so one trade's loss is bounded
(`stop_range_fraction`, `min_stop_distance_pct`, `max_stop_distance_pct`).

Targets are deliberately left structure-anchored. Widening reward:risk by
tightening the stop is legitimate; manufacturing a target from the risk distance
is circular, and the alert gate correctly rejects it as gaming.

Measured on the real candidates:

| Ticker | Old stop | New stop | Old R:R | New R:R |
|---|---:|---:|---:|---:|
| PPBT | 39.2% | **12.0%** | 1.55 | **5.06** |
| BIAF | 35.4% | **12.0%** | 1.54 | **4.54** |
| LIDR | 22.1% | **10.2%** | 1.47 | **3.19** |
| GPRO | 14.4% | 6.3% | 1.38 | 3.16 |
| CHPT | 11.5% | 4.8% | 1.32 | 3.13 |
| TLYS | 11.0% | 4.5% | 1.30 | 3.13 |

Worst-case single-trade loss is hard-capped at 12%; reward:risk roughly triples.

**Trade-off, stated plainly:** tighter stops mean more frequent small losses in
place of rare catastrophic ones. All three thresholds are tunable via
`INTRADAY_STOP_RANGE_FRACTION`, `INTRADAY_MIN_STOP_DISTANCE_PCT`,
`INTRADAY_MAX_STOP_DISTANCE_PCT`.

### 5. A closed loop prevented the first trade

`confidence_bucket` stays `INSUFFICIENT_SAMPLE` until 20 real outcome days
exist, but outcome days accrue only from entries, and entries required passing
that gate. The system could never bootstrap itself.

Bootstrap paper mode (`DAWNSTRIKE_BOOTSTRAP_PAPER_MODE`, off by default) waives
**only** evidence that cannot exist before the first trade. Halt, SEC, spread,
price/level validity, volume, data quality, gap regime and stop distance are
never waivable — a test fails if any is added to a waivable set. Waived items
are recorded on the row as `bootstrap_waived_reasons`, so a bootstrap entry can
never be mistaken for a fully evidenced one.

### 6. Catalyst classifier manufactured catalysts

It had no concept of **earnings** — the most common premarket gap catalyst — and
attributed multi-ticker roundups to individual candidates. One candidate (CHPT)
was assigned an article about **Snowflake**.

Roundups now score 0.20 flagged `non_specific_market_roundup`; real earnings
reach 0.90.

### 7. `A+` was rejected as below threshold

`_setup_grade` emits `"A+"` for scores ≥ 90, but `ALERTABLE_SETUP_GRADES` was
`{"A", "B"}`. The single best grade the scorer can produce was being rejected.

## CI determinism

CI was **already red on `main`** at `14ca714f` (13 failing jobs) before this
branch existed. Of the failures unique to this branch, the genuine ones were:

- **Holiday-blind fixtures.** `_future_session_date` and the retained-history
  builder skipped weekends but not market holidays. Running on Saturday
  2026-09-05 selected Labor Day Monday 2026-09-07 — `weekday()` is 0, so the old
  guard passed it through — and six tests failed with `CSV source symbol TST
  lacks exact completed bar 2026-09-04`. Now uses the project's own
  `is_market_holiday`, so the suite no longer depends on the date it runs.
- **Two more Streamlit tests** with the `AppTest.from_file("app.py")`
  relative-path defect, silently not running.
- **mypy on Linux.** `capture_operations.py` imports from
  `scripts/dawnstrike_python_bootstrap.py`, so `mypy intraday_scanner` follows
  into Windows-only `ctypes.WinDLL`. Guarded by `os.name != "nt"`, but mypy
  narrows on `sys.platform`. Annotated with the convention that file already
  uses for its POSIX-only `fcntl` calls.

## A deployment trap caught before it shipped

`Import-DawnstrikeEnvironment` **silently skips** any key absent from its
allowlist. `DAWNSTRIKE_BOOTSTRAP_PAPER_MODE` was not listed, so setting it in
`runtime.env` would have produced no error and no effect — bootstrap mode was
undeployable. Both operator flags are now allowlisted, with a test that fails if
a flag the Python layer reads is missing, plus an end-to-end check that the
loader exports it and still ignores unknown keys.

## Verification

- Monitor fix proven against **real production artifacts** from all four failed
  days: every one `ACCEPTED`, `selection=valid_no_edge`.
- Each fix proven to fail before and pass after by reverting it and re-running
  its test. The prior-day-target control case passes in both states, so the test
  is not vacuous.
- `ruff check .`, `mypy intraday_scanner` (364 files), `compileall`,
  `node --check`, `git diff --check` — all clean.
- Local suite: 5,156 → 5,200 collected, 5,090 → 5,134 passed, 60 → 60 failed
  after the trading fixes (zero regressions; all 44 added tests pass), with a
  further 8 tests fixed by the CI-determinism work.
- One added test caught a real bug during development: the 12% stop cap drifted
  to 12.0003% from tick rounding. The stop now rounds toward entry, so the cap
  is a hard bound.

## What is deployed, and what cannot be

`vercel.json` sets `"git": {"deploymentEnabled": false}`, so pushing to GitHub
cannot trigger a Vercel deploy. Production publishing runs through the daily
finalize, which refuses to publish while upstream is degraded — the fail-closed
guard working as designed.

**Hard calendar constraint.** Verified against the product's own calendar:

```
2026-09-05 Sat   |  2026-09-07 Mon  holiday=True  (Labor Day)
2026-09-08 Tue   holiday=False
```

The next live session is **Tuesday 2026-09-08**. A live premarket cycle — and
the first-ever `alpha_monitor-<date>.stdout.log` — cannot be produced before
then by any amount of work.

### Deployment status

| Step | Status |
|---|---|
| PR opened and CI run | see below |
| Merge to `main` | see below |
| Runtime updated | see below |
| Scheduled tasks re-registered | see below |
| Bootstrap mode enabled | see below |
| Live cycle verified | **BLOCKED until 2026-09-08** |
| Vercel production publish | **BLOCKED** — requires a healthy pipeline |

*(Completed as the deployment proceeds; see the closing summary in the session
record for final state.)*

## Standing limitation

25 forward paper sessions have produced **zero trades**, so no performance claim
of any kind is supportable. Nothing in this work changes that. What changed is
that the system moved from *mathematically incapable of ever trading* to *able
to trade when a qualifying setup appears* — with a single trade's loss capped at
12% rather than 39%.
