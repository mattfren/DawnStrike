# Dawnstrike Opus 5 audit and Luna execution directive

Date: 2026-08-09
Audited source SHA: `ba39a5353045b7d417936ed1aed0ee4802169759`
Controlling Opus attachment SHA-256:
`A0A58E6C78E83CB91D49258413766626875C5CB36E7138F178C00F9B4E728C65`
Scope: audit, implementation directive, and proof gates. No broker execution.

## Bottom-line audit verdict

Use the Opus report as a hypothesis list, not as an implementation
specification. Its central strategic direction is sound: Dawnstrike needs a
retained intraday evidence spine, a causal replay of the strategy actually
used by AlphaOps, stronger cost and catalyst evidence, and statistically honest
validation. However, every one of its seven findings is either partly stale,
overstated, or missing a critical evidence boundary. Several literal remedies
would corrupt rather than improve the system.

The four most dangerous instructions are:

1. treating the stale checkout SQLite file as current operational truth;
2. flipping `target_derived_from_risk` to `true`, which reverses the current
   source lineage;
3. converting raw or retrospective signals into prospective V6 proof; and
4. requiring one-minute OHLC to produce zero path ambiguity.

Phase 8 is out of scope. Dawnstrike is a research/watchlist and simulated
paper-audit system. Luna must not add a broker SDK, order route, credentials,
or live execution task.

## State reconciliation

Opus audited `data/shadow_real.sqlite`, not the mounted operational state.

| Evidence | Checkout DB | Active mounted DB |
|---|---:|---:|
| Path | `C:\Users\MattFields\Dawnstrike\data\shadow_real.sqlite` | `C:\r\dawnstrike-state\shadow_real.sqlite` |
| SHA-256 | `BCEEE6FC1D889BC6025721096975D1D0B4E712EBFB0346024F5D897E856E7528` | `A78D0C28842C0831F6C9D810B3FD63269B1470734B09C08C09CA14F37EB807AA` |
| Size | 91,480,064 bytes | 160,616,448 bytes |
| User tables | 110 | 110 |
| Empty user tables | 64 | 34 |
| Historical signals | 234 / 28 dates | 258 / 32 dates |
| Signal outcomes | 10 | 14 |
| Complete sourced outcomes | 5 | 9 |
| Outcome interval | one minute | one minute |
| Halt rows | 0 | 41 |
| Notifications | stale | 137, all distinct event keys |
| Closed AlphaOps positions | 8 legacy V4 | 8 legacy V4 |
| V6 tables | 15, all empty | 15, 12 populated |

The active runtime at `C:\r\dawnstrike-runtime` was clean at the audited SHA.
The primary checkout was at the same SHA but already dirty with unrelated
calendar/dashboard work. That checkout must not be Luna's implementation lane.

Mounted V2 PaperOps is also real, not dormant. The active ledger contained
4,578 forward events, 42 fills, 42 openings, 22 closes, and nine open positions
across the daily V2 strategy catalog. That cohort is separate from official
AlphaOps and must remain separate.

The current code baseline passed:

- full Pytest with cache disabled;
- Ruff over the repository; and
- mypy over `intraday_scanner`.

The remediation is evidence hardening, not recovery from a currently broken
test suite.

## Finding-by-finding disposition

### F1 — Strategy/backtest mismatch: PARTLY TRUE

The important gap is real: official AlphaOps V5 has no reproducible,
point-in-time, quote/cost-aware intraday backtest. The existing V2 backtests are
daily research strategies and do not prove AlphaOps performance.

Corrections:

- Official paper policy is V5 prospectively from 2026-07-31, not V4.
- V2 daily strategies do run in mounted PaperOps; they are not all unused.
- The daily engine calls adjacent equity changes `daily_returns` and hardcodes
  252. Do not fix this by blindly using `sqrt(252 * 390)`. Intraday equity is
  for drawdown/tails; Sharpe and Sortino must use one exchange-session return
  per day and annualize that series by 252. Unsupported irregular sampling is
  `NOT_ANNUALIZABLE`.
- The correct build is a new event-driven intraday engine alongside the daily
  engine, with an adapter around the exact V5 decision contract.

### F2 — Daily outcome truth: MOSTLY FALSE

Mounted AlphaOps outcome capture already requests `1m` Yahoo/Alpaca bars,
validates regular-session coverage, finds first touch, and calculates post-entry
high/low plus MFE/MAE. The dormant daily Yahoo DataTruth fetcher is not the
mounted AlphaOps EOD resolver.

The real gaps are:

- legacy `paper_positions` retain null MFE/MAE;
- the trigger minute can contain pre-entry extrema;
- one-minute OHLC cannot order a stop and target touched in the same minute;
- missing one-minute intervals are not yet linked to symbol-specific halt
  windows;
- quote, trade, depth, latency, and attainable-fill evidence is incomplete;
- raw path artifacts are not retained as a canonical content-addressed spine.

The fix is to extract and harden the current resolver, not replace it with a
parallel implementation. Ambiguity must be labeled, counted, and preserved.

### F3 — Risk geometry: LEGACY DEFECT TRUE; CURRENT CONTRADICTION FALSE

The historical clusters reproduce: 165 legacy rows near 0.875R and 53 legacy
rows near 1.5R. They belong to different dates/configuration epochs.

The 53-row cohort does not store `target_derived_from_risk=false`. Current code
derives its target from the observed premarket range and correctly records
`target_derived_from_risk=false`. No stored row combines an approximately 1.5R
target with that false flag. Luna must never flip it to true or rewrite legacy
rows to claim provenance they did not record.

Current V5 already adds independent-target rejection, 0.25%-of-equity planned
risk sizing, a 10% symbol-notional cap, after-cost R, a 15% maximum stop, a
200-bps spread ceiling, and 50-bps-per-side modeled slippage. The remaining
valid research gap is volatility/structure-aware level construction. That must
be a versioned shadow challenger, not a silent rewrite of V5.

The BIYA arithmetic is internally reproducible, including the roughly -47.26%
simulated return, but it proves only the stored polling/fill model. It does not
prove a real fill, queue position, NBBO, or LULD path.

### F4 — Catalyst engine: OVERSTATED

Legacy coverage is genuinely poor: the historical cohort overwhelmingly says
`no_clear_catalyst`, and setup grades are C/D. The current headline classifier
is shallow, the news service keeps only the latest item inside a short window,
and SEC processing is primarily filing-metadata safety logic.

It is not 100% non-functional. The mounted flow collects news, runs deterministic
headline rules, resolves SEC submissions, persists SEC risk events, and gates
candidates. Its principal deficits are full-document evidence, point-in-time
multi-article retention, offering/dilution terms, explicit unknown/conflict
states, and calibrated coverage.

“Catalyst is the entire edge” and “S-3/424B5 inside 72 hours is the strongest
fade” are unvalidated hypotheses. S-3 registration alone does not establish a
takedown. Test catalyst feature groups and an avoid-long hypothesis out of
sample. Do not add a short/fade execution path.

The proposed `<10% no_clear_catalyst` gate is unsafe because it rewards forced
classification. Gate human-reviewed precision/recall, evidence coverage,
abstention calibration, and out-of-sample incremental value instead.

### F5 — Universe is adversarial: UNPROVEN AND STALE

The legacy `.env.example` profile is aggressive, but mounted AlphaOps uses a
dynamic Alpaca most-active/gainer universe and tighter V5/Alpha-cycle limits.
Eight pre-V5 paper trades and a 25% win rate are too small and too stale to prove
that the current universe is adversarial or dilution-dominated.

Treat the claim as a registered segmentation hypothesis. Compare results by
price, gap, dollar volume, float availability, offering state, source coverage,
and liquidity out of sample. Do not tune the universe to erase the eight losses.

### F6 — Learning loop/ML: STALE AND MATERIALLY WRONG

There are 15 `alpha_v6_*` tables, not 19. In active state, 12 are populated:
the V6 pipeline has run and correctly abstained. The latest model run is
`NOT_TRAINED_INSUFFICIENT_LABELS`; the dataset has zero eligible
benchmark-relative after-cost return rows, so there is no trained artifact.

Raw historical signals are not labels. The canonical active snapshot has 49
official selections, 32 of which require outcomes; no-trade and ranked/rejected
rows have different contracts. Retrospective replay can support research, but
it cannot count as prospective promotion evidence. The 100-label, 500-label,
and 60-date gates must not be weakened or bypassed.

### F7 — Cost model: DIRECTIONALLY TRUE, NUMBERS UNPROVEN

Fifty basis points per side is a fixed assumption, not an empirical
microstructure model. Current paper fills do not model depth, queue position,
partial fills, market impact, or symbol-linked halt/resume feasibility.

The report's “routine 100–300 bps” and BIYA halt assertions are not established
by retained quote/trade/status evidence. Build an observed-cost model plus
declared stress multipliers. A quote is NBBO only when the feed is consolidated;
IEX must remain labeled IEX. Quotes alone do not prove impact or queue fills.

### Phase 8 — Broker integration: PARKED

The report's Robinhood facts are now stale: Robinhood documents an authorized
Agentic Trading product that can place equity orders, and its current margin
account documentation says the old $25,000 PDT minimum is no longer required.
Those facts do not change Dawnstrike's product boundary. Broker execution is
not part of this build.

Official current references:

- [Robinhood Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
- [Robinhood day-trading and intraday-margin rules](https://robinhood.com/us/en/support/articles/pattern-day-trading/)

### Current provider facts that replace the report's price assumptions

Massive (formerly Polygon) currently lists Stocks Basic at $0 with two years of
history and minute aggregates; Starter at $29 with five years and flat files;
Developer at $79 with trades; and Advanced at $199 with historical quotes and
real-time data. Therefore `$29-$79` does not, by itself, prove the historical
quote/NBBO capability Opus assumes. Luna must probe the actual account and
license before backfill.

Alpaca currently documents Basic as real-time IEX and Algo Trader Plus as
consolidated US exchange coverage. Its documentation also says historical SIP
requests older than the recent-data boundary may be available without the paid
subscription. Again, the account probe—not the marketing tier name—is the
authority.

SEC submissions metadata is available through official real-time APIs and bulk
archives, but full-document collection must obey SEC fair-access rules.

Official current references:

- [Massive stocks pricing and entitlements](https://massive.com/pricing?product=stocks)
- [Alpaca Market Data API plans](https://docs.alpaca.markets/us/docs/about-market-data-api)
- [Alpaca IEX versus SIP FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)
- [SEC EDGAR APIs and update schedule](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

### Housekeeping corrections

- `app.py` is a real monolith at about 172 KB / 4,513 lines, but it is not on
  the evidence-spine critical path.
- `data` contains 21 SQLite files including the canonical checkout DB, not 20
  total. Inventory references before any archive; delete nothing in this build.
- There are nine `.pytest_*_tmp_*` directories plus `.pytest_cache`, not about
  fifteen. Do not clean user-owned state as part of this work.
- `intraday_scanner/notifications` is a compatibility facade over `notifiers`,
  not a duplicate implementation.
- `source_confidence` is partly a field-completeness heuristic mislabeled as
  confidence. Split completeness, source reliability, and reconciliation.

## Read-only baseline SQL

Luna must preserve the output of these queries against the Stage A immutable
snapshot. Open SQLite with URI `mode=ro`; never run them through a connection
that can write.

```sql
SELECT name
FROM sqlite_master
WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
ORDER BY name;

SELECT COUNT(*) AS rows,
       COUNT(DISTINCT market_date) AS days,
       MIN(market_date) AS first_date,
       MAX(market_date) AS last_date
FROM historical_signals;

SELECT strategy_id, strategy_version, cohort, decision,
       COUNT(*) AS rows,
       COUNT(DISTINCT substr(selected_at, 1, 10)) AS days
FROM signal_selections
GROUP BY strategy_id, strategy_version, cohort, decision
ORDER BY strategy_id, strategy_version, cohort, decision;

SELECT outcome_status, outcome_source,
       COUNT(*) AS rows,
       COUNT(DISTINCT signal_id) AS signals
FROM signal_outcomes
GROUP BY outcome_status, outcome_source
ORDER BY outcome_status, outcome_source;

SELECT name
FROM sqlite_master
WHERE type = 'table' AND name LIKE 'alpha_v6_%'
ORDER BY name;

SELECT label_family, learning_eligible,
       COALESCE(exclusion_reason, '') AS exclusion_reason,
       COUNT(*) AS rows
FROM alpha_v6_labels
GROUP BY label_family, learning_eligible, COALESCE(exclusion_reason, '')
ORDER BY label_family, learning_eligible, exclusion_reason;

SELECT outcome_status, learning_eligible, COUNT(*) AS rows
FROM alpha_v6_outcomes
GROUP BY outcome_status, learning_eligible
ORDER BY outcome_status, learning_eligible;

SELECT dataset_id, created_at, training_cutoff, row_count,
       dataset_hash_sha256
FROM alpha_v6_datasets
ORDER BY created_at;

SELECT position_id, market_date, ticker, status, quantity,
       opened_at, closed_at, entry_price, exit_price,
       stop_price, target_price, notional,
       realized_pnl, realized_return_pct,
       max_favorable_excursion, max_adverse_excursion
FROM paper_positions
ORDER BY opened_at;

SELECT intent_id, action, decision_time, decision_price,
       trigger_price, stop_price, target_price,
       quantity, notional, reason
FROM trade_intents
WHERE ticker = 'BIYA' AND market_date = '2026-07-20'
ORDER BY decision_time;

SELECT fill_id, side, fill_time, fill_price,
       quantity, gross_notional, slippage_bps
FROM paper_trade_fills
WHERE ticker = 'BIYA' AND market_date = '2026-07-20'
ORDER BY fill_time;

SELECT COUNT(*) AS halt_rows,
       COUNT(DISTINCT ticker) AS halt_tickers
FROM halt_events;

SELECT COUNT(*) AS rows,
       COUNT(DISTINCT event_key) AS distinct_event_keys
FROM notifications_sent;

SELECT event_key, channel, COUNT(*) AS rows
FROM notifications_sent
GROUP BY event_key, channel
HAVING COUNT(*) > 1;

SELECT s.market_date, s.ticker,
       (s.target_1 - s.entry_watch_level) /
       NULLIF(s.entry_watch_level - s.invalidation_level, 0) AS reward_risk,
       j.value AS target_derived_from_risk
FROM historical_signals AS s
LEFT JOIN json_tree(s.raw_payload_json) AS j
  ON j.key = 'target_derived_from_risk'
ORDER BY s.market_date, s.ticker;
```

---

# COPY/PASTE INTO LUNA — EXECUTE, DO NOT RETURN ANOTHER PLAN

You are Luna, the principal data-integrity, quant-research, and reliability
engineer for Dawnstrike. Execute the following directive in order. Do not merely
summarize it. Do not skip a gate to make progress appear complete.

## Mission

Build Dawnstrike's retained intraday evidence spine and use it to make official
AlphaOps outcomes, causal replay, cost/risk challengers, catalyst evidence, V6
research datasets, and diagnostic attribution reproducible and truth-safe.

The product remains research/watchlist plus simulated paper audit. It must not
connect to a broker or place an order. Do not claim the strategy is profitable.

## Fixed authority order

When evidence conflicts, use this order:

1. the exact clean isolated source worktree created below;
2. read-only snapshots of `C:\r\dawnstrike-runtime` and
   `C:\r\dawnstrike-state`;
3. immutable saved artifacts and database rows with hashes;
4. source code and tests on the audited SHA;
5. this directive;
6. the Opus report as historical hypothesis context only.

## Non-negotiable invariants

- Do not edit `C:\Users\MattFields\Dawnstrike`; it contains unrelated user
  changes.
- Do not edit `C:\r\dawnstrike-runtime`, the active SQLite DB, Task Scheduler,
  runtime secrets, or published artifacts during implementation.
- Do not push, deploy, publish, register tasks, migrate active state, or buy a
  data subscription without a new explicit operator authorization.
- Do not add a broker SDK, broker credential, order endpoint, order tool, live
  execution interface, or broker task. Keep all existing live-order locks.
- Never mutate/delete the eight legacy V4 positions or relabel them because
  they lost. Segment them as the pre-V5 baseline and keep them visible in that
  cohort.
- Preserve `target_derived_from_risk=false` for current range-derived targets.
  Never invent legacy provenance.
- Preserve daily V2 backtests/PaperOps and official AlphaOps as separate
  evidence cohorts.
- Missing, stale, disputed, ambiguous, unreconciled, unavailable, or future
  truth is null/ineligible with an explicit reason. It is never zero.
- Raw retrospective signals do not count as forward labels. Retrospective
  research eligibility and prospective promotion eligibility are separate.
- LLMs may extract factual claims with cited spans. They may not set scores,
  grades, policies, prices, targets, sizes, recommendations, or trades.
- No automatic strategy promotion or parameter update. Every challenger is
  shadow-only until registered evidence and manual approval exist.
- Do not optimize against BIYA or the other seven legacy trades as individual
  targets.

## Mandatory stop codes

Stop and report the exact evidence when any of these applies:

- `STOP_HANDOFF_DRIFT`: fetched `origin/main` is not the audited SHA.
- `STOP_DIRTY_ISOLATED_WORKTREE`: Luna's new worktree is not clean before edits.
- `STOP_BASELINE_REGRESSION`: a baseline gate fails before Luna changes code.
- `STOP_CAUSALITY_LEAK`: a feature, membership, level, or article is observed
  after its decision timestamp.
- `STOP_MIGRATION_REHEARSAL`: migration is not idempotent or recovery proof
  fails on a disposable copy.
- `BLOCKED_EXTERNAL_MARKET_DATA_ENTITLEMENT`: required feed, history, storage
  right, or credential is unavailable.
- `STOP_SCOPE_BROKER`: any requested action would add broker execution.
- `STOP_RELEASE_AUTHORITY`: a step would mutate active runtime/state or publish.
- `STOP_UNRELATED_FAILURE`: a pre-existing unrelated gate fails; quote it
  verbatim and do not rewrite tests to force closure.
- `STOP_RESEARCH_PROTOCOL_APPROVAL`: a performance/promotion evaluation needs
  a threshold that was not frozen and approved before outcomes were inspected.

An external provider block does not stop fixture-backed contract work. Finish
safe code/tests, mark live acquisition/backfill blocked, and do not weaken the
data contract.

## Execution ledger

Create `$evidenceRoot\luna_evidence_spine_execution_20260809.jsonl` after A006.
Append exactly one canonical JSON object per line after every numbered step.
Do not use a `.json` array that must be partially rewritten. Each record has:

- step ID;
- UTC start/end;
- status (`PASS`, `FAIL`, `BLOCKED`, `NOT_APPLICABLE`);
- exact command or operation;
- input/output hashes;
- changed files;
- test result;
- blocker/reason; and
- current commit SHA.

Do not include secret values or raw credentials. Keep this live ledger outside
Git. Committed audit packets contain only its SHA-256 and a sanitized summary.

Before every commit in this directive, run this review protocol:

```powershell
git status --short
git diff --name-only
git diff --check
```

Stage only the exact current-stage files listed in the execution ledger with
`git add -- <explicit paths>`, then run:

```powershell
git diff --cached --name-only
git diff --cached --check
```

Never use `git add .`, `git add -A`, or a directory-wide staging command. Never
stage a file that existed as an unrelated change before the current stage.

## Stage A — isolate and freeze authority

**A001.** Open PowerShell and run these assignments exactly:

```powershell
$sourceCheckout = 'C:\Users\MattFields\Dawnstrike'
$worktree = 'C:\r\dawnstrike-luna-evidence-spine-20260809'
$evidenceRoot = 'C:\r\dawnstrike-luna-evidence-20260809'
$branch = 'codex/luna-dawnstrike-evidence-spine'
$auditedSha = 'ba39a5353045b7d417936ed1aed0ee4802169759'
$activeDb = 'C:\r\dawnstrike-state\shadow_real.sqlite'
$activeRuntime = 'C:\r\dawnstrike-runtime'
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion -lt [version]'7.3') {
  throw 'PowerShell 7.3 or newer is required for reliable native-command fail-fast behavior'
}
$PSNativeCommandUseErrorActionPreference = $true
```

**A002.** Record, but do not alter, the dirty source checkout:

```powershell
git -C $sourceCheckout status --short
git -C $sourceCheckout rev-parse HEAD
```

**A003.** Fetch and enforce exact handoff identity:

```powershell
git -C $sourceCheckout fetch --prune origin
if ($LASTEXITCODE -ne 0) { throw 'STOP_HANDOFF_DRIFT git fetch failed' }
$originSha = (git -C $sourceCheckout rev-parse origin/main).Trim()
if ($LASTEXITCODE -ne 0) { throw 'STOP_HANDOFF_DRIFT rev-parse origin/main failed' }
if ($originSha -ne $auditedSha) { throw "STOP_HANDOFF_DRIFT origin/main=$originSha audited=$auditedSha" }
```

**A004.** Refuse to overwrite an existing path or branch:

```powershell
if (Test-Path -LiteralPath $worktree) { throw "Worktree path already exists: $worktree" }
$existingBranch = git -C $sourceCheckout branch --list $branch
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect existing branches' }
if ($existingBranch) { throw "Branch already exists: $branch" }
```

**A005.** Create the isolated lane and verify it:

```powershell
git -C $sourceCheckout worktree add -b $branch $worktree origin/main
if ($LASTEXITCODE -ne 0) { throw 'Worktree creation failed' }
$isolatedStatus = git -C $worktree status --short
if ($LASTEXITCODE -ne 0) { throw 'Worktree status failed' }
if ($isolatedStatus) { throw "STOP_DIRTY_ISOLATED_WORKTREE $isolatedStatus" }
$isolatedHead = (git -C $worktree rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Worktree HEAD check failed' }
if ($isolatedHead -ne $auditedSha) { throw "STOP_HANDOFF_DRIFT worktree=$isolatedHead" }
git -C $worktree merge-base --is-ancestor $auditedSha HEAD
if ($LASTEXITCODE -ne 0) { throw 'STOP_HANDOFF_DRIFT audited SHA is not an ancestor' }
```

Require empty `git status`, exact HEAD `$auditedSha`, and ancestry exit zero.

**A006.** Create the external evidence directory without deleting anything:

```powershell
if (Test-Path -LiteralPath $evidenceRoot) { throw "Evidence path already exists: $evidenceRoot" }
New-Item -ItemType Directory -Path $evidenceRoot | Out-Null
Set-Location -LiteralPath $worktree
```

Create the JSONL execution ledger now, then record A001 through A006 in order.

**A007.** Run the untouched baseline from `$worktree`:

```powershell
py -m pytest -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { throw 'STOP_BASELINE_REGRESSION pytest failed' }
py -m ruff check .
if ($LASTEXITCODE -ne 0) { throw 'STOP_BASELINE_REGRESSION Ruff failed' }
py -m mypy intraday_scanner
if ($LASTEXITCODE -ne 0) { throw 'STOP_BASELINE_REGRESSION mypy failed' }
py -m compileall -q intraday_scanner scripts
if ($LASTEXITCODE -ne 0) { throw 'STOP_BASELINE_REGRESSION compileall failed' }
node --check web/assets/dawnstrike.js
if ($LASTEXITCODE -ne 0) { throw 'STOP_BASELINE_REGRESSION Node syntax failed' }
git diff --check
if ($LASTEXITCODE -ne 0) { throw 'STOP_BASELINE_REGRESSION diff check failed' }
```

Stop on any failure and preserve its exact output.

**A008.** Snapshot active SQLite with the existing online-backup script. The
source remains read-only:

```powershell
py scripts\snapshot_sqlite.py --source-db $activeDb --target-db "$evidenceRoot\active-baseline.sqlite"
if ($LASTEXITCODE -ne 0) { throw 'Active SQLite snapshot failed' }
Get-FileHash "$evidenceRoot\active-baseline.sqlite" -Algorithm SHA256
Get-FileHash $activeDb -Algorithm SHA256
```

Require a `quick_check`-verified snapshot receipt from the script and matching
source/snapshot logical counts. Never commit the SQLite copy.

**A009.** Record immutable runtime inputs without printing secret values:

```powershell
$runtimeStatus = git -C $activeRuntime status --short
if ($LASTEXITCODE -ne 0) { throw 'Active runtime status failed' }
$runtimeHead = (git -C $activeRuntime rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Active runtime HEAD check failed' }
if ($runtimeStatus) { throw "STOP_HANDOFF_DRIFT active runtime dirty: $runtimeStatus" }
if ($runtimeHead -ne $auditedSha) { throw "STOP_HANDOFF_DRIFT runtime=$runtimeHead" }
Get-FileHash 'C:\r\dawnstrike-state\config\web_sources.yaml' -Algorithm SHA256
```

Export the five Dawnstrike scheduled-task XML definitions to `$evidenceRoot`
for hashing only. Use these exact task names:

```powershell
$taskNames = @(
  'Dawnstrike AlphaOps Morning',
  'Dawnstrike AlphaOps Monitor 5m',
  'Dawnstrike AlphaOps EOD Full Report',
  'Dawnstrike 10of10 Daily Finalize',
  'Dawnstrike AlphaOps V6 Weekly Training'
)
foreach ($taskName in $taskNames) {
  $safeName = $taskName -replace '[^A-Za-z0-9._-]', '_'
  Export-ScheduledTask -TaskName $taskName |
    Set-Content -LiteralPath "$evidenceRoot\$safeName.xml" -Encoding UTF8
  Get-FileHash "$evidenceRoot\$safeName.xml" -Algorithm SHA256
}
```

Do not modify task state or commit the XML.

**A010.** Execute the baseline SQL in this document's audit section against the
snapshot in SQLite read-only mode. Record table counts, official selections,
outcome statuses/sources, V6 label eligibility, datasets, paper positions,
fills, intents, halt rows, and notifications. Fail if the querying code can
write to the DB.

**A011.** Build tracked
`docs/audit/evidence/frozen_alphaops_cohorts_20260809.json` and its
`frozen_alphaops_cohorts_20260809.sha256` sidecar with three non-overlapping
groups:

1. official selections where the canonical contract requires an outcome;
2. official no-trade selections; and
3. a deterministically sampled, predeclared rejected/counterfactual cohort.

Include DB hash, source SHA, selection/signal IDs, decision timestamps, market
dates, strategy/config versions, and manifest hash. Do not call all 258 raw
signals official labels.

For the rejected/counterfactual cohort, freeze the sampling rule before reading
any outcome:

- sampling frame: historical signals not present in either official group,
  with signal ID, decision timestamp, entry, stop, target, and generated time
  no later than the snapshot cutoff;
- deduplication key: `(market_date, ticker, config_hash)`, keeping the
  lexicographically smallest signal ID;
- sample size: `min(50, deduplicated_frame_count)`;
- seed string:
  `dawnstrike-counterfactual-v1|<audited_sha>|<snapshot_sha256>`;
- rank key: SHA-256 of `<seed>|<signal_id>` ascending; and
- no outcome, post-decision price, or realized result may participate in frame,
  deduplication, or ranking.

Persist the frame count, deduplicated count, sample count, seed hash, ordered
membership IDs, exclusions, and algorithm version.

**A011a.** Before computing any new backfill, path replay, challenger, model, or
backtest result, create
`docs/research/alphaops_intraday_research_protocol_v1.yaml` and its
`alphaops_intraday_research_protocol_v1.sha256` sidecar. Freeze:

- evidence and dataset purposes;
- training minimum of 100 eligible return rows;
- gradient-boosting minimum of 500 eligible labels across 60 distinct dates;
- 98% eligible source/cost/benchmark coverage;
- independently approved forward-session and activated-trade promotion
  minimums, or `REQUIRES_OPERATOR_APPROVAL` if none was frozen previously;
- 95% market-date cluster-bootstrap interval and lower-bound rule;
- base, 1.5x, and 2x cost scenarios;
- primary split unit and embargo horizon;
- one-time holdout identity;
- concentration, drawdown, CVaR, capacity/participation, fold stability, and
  multiple-trial rules; and
- every unresolved threshold as `REQUIRES_OPERATOR_APPROVAL`.

The protocol must disclose all evidence already seen in the Opus audit and this
re-audit, including legacy trade outcomes, quoted V2 metrics, active counts, and
BIYA. Historical replay is therefore exploratory/OOS-by-time, not a pristine
holdout. Reserve post-snapshot future dates as the true untouched forward
holdout. Hash and commit the protocol before computing new performance. Luna may
implement code while approval is pending, but any result depending on an
unresolved threshold is `NOT_EVALUABLE_PENDING_PROTOCOL_APPROVAL`; Luna may not
select the threshold after viewing outcomes.

**A012.** Add and run `scripts/audit_opus_baseline.py` plus
`tests/test_audit_opus_baseline.py`. It must reproduce the snapshot inventory,
use SQLite URI `mode=ro`, emit deterministic
`docs/audit/evidence/opus5_baseline_20260809.json`, and refuse any DB without a
successful integrity check.

**A013.** Commit only the deterministic baseline script, test, sanitized JSON
receipt, and frozen research protocol:

```powershell
git status --short
git diff --check
git add scripts/audit_opus_baseline.py tests/test_audit_opus_baseline.py `
  docs/audit/evidence/opus5_baseline_20260809.json `
  docs/audit/evidence/frozen_alphaops_cohorts_20260809.json `
  docs/audit/evidence/frozen_alphaops_cohorts_20260809.sha256 `
  docs/research/alphaops_intraday_research_protocol_v1.yaml `
  docs/research/alphaops_intraday_research_protocol_v1.sha256
git diff --cached --check
git commit -m "audit: freeze evidence-spine baseline"
```

Do not add the DB, task XML, runtime config, secrets, or raw provider data.

## Stage B — repair lineage and confidence semantics

**B001.** Add the tracked immutable manifest
`docs/audit/evidence/legacy_policy_classification_20260809.json` plus
`legacy_policy_classification_20260809.sha256`. Use schema
`dawnstrike.legacy-policy-classification.v1`, canonical UTF-8 JSON with sorted
keys and compact separators, and fields for source DB hash, source SHA,
classifier version, generated-at UTC, membership signal IDs, inferred legacy
policy, inference evidence, and explicit unknown provenance. Classify the 53
fixed-1.5R rows as historical legacy policy; leave original payloads untouched.
Stage C migration 22 provides optional additive persistence for this manifest.

**B002.** Use `official_strategy_cohorts` and explicit strategy/config versions
to keep V4 historical, V5 official paper, V6 shadow, and V2 daily PaperOps
separate. Never create a generic blended “Dawnstrike returns” series.

**B003.** In `intraday_scanner/models.py`,
`intraday_scanner/providers/public_table_provider.py`, source adapters, and
feature serialization, introduce these independent fields:

- `field_completeness_score`;
- `source_reliability_prior`;
- `reconciliation_status`;
- `reconciliation_confidence_score`; and
- `evidence_confidence_version`.

**B004.** Preserve the old `source_confidence` field as a versioned compatibility
value for existing V4/V5 fixtures. Do not silently change a mounted champion's
decision from this cleanup. A new policy may use the split fields only as a
shadow challenger until parity and promotion gates pass.

**B005.** Add regression tests proving:

- completeness never claims independent verification;
- an authenticated single feed is not mislabeled reconciled;
- a provider failure is distinct from no evidence;
- legacy payloads round-trip unchanged; and
- current range-derived targets retain
  `target_derived_from_risk=false`.

**B006.** Add a regression that reconstructs current targets from the recorded
premarket-range formula and its policy version. Add a separate fixture proving
legacy fixed-RR rows do not acquire current provenance.

**B007.** Run focused tests, full Pytest, Ruff, mypy, and `git diff --check`.
Commit as:

```powershell
$stageFiles = @(
  'docs/audit/evidence/legacy_policy_classification_20260809.json',
  'docs/audit/evidence/legacy_policy_classification_20260809.sha256',
  'intraday_scanner/models.py',
  'intraday_scanner/providers/public_table_provider.py',
  'intraday_scanner/providers/alpaca_screener_provider.py',
  'intraday_scanner/alpha/feature_factory.py',
  'tests/test_evidence_confidence_semantics.py',
  'tests/test_scoring.py'
)
git add -- $stageFiles
git diff --cached --name-only
git diff --cached --check
git commit -m "refactor: make signal lineage and evidence confidence explicit"
```

## Stage C — extend the existing DataTruth layer to intraday evidence

Do not create a second top-level truth architecture. Extend
`intraday_scanner/v2/data_truth` and the existing provider/storage contracts.

**C001.** Confirm `CURRENT_SCHEMA_VERSION == 21`. If it changed after Stage A,
stop for handoff drift. Add only additive migrations beginning at 22 in
`intraday_scanner/storage/migrations.py`.

**C002.** Define frozen contracts in:

- `intraday_scanner/v2/contracts/data.py`;
- `intraday_scanner/v2/data/market.py`;
- `intraday_scanner/v2/data_truth/models.py`; and
- new `intraday_scanner/v2/data_truth/intraday.py`.

Required immutable types:

- `IntradayBar` for unadjusted OHLCV and VWAP;
- `TradePrint` with exchange/conditions/sequence when available;
- `MarketQuote` with feed, bid/ask, sizes, exchanges, and timestamp;
- `MarketStatusInterval` for halt/resume/LULD status;
- `CorporateActionRecord` with symbol mapping and effective timestamp;
- `IntradayArtifactManifest` with raw and normalized hashes; and
- `IntradayCoverageReceipt` with a provider/data-coverage status enum.

Every timestamp is UTC plus exchange-session identity. Every numeric price has
an explicit adjustment basis. Every source records provider, feed, entitlement,
request window, fetch time, code SHA, raw hash, normalized hash, and retention
status.

**C003.** Add migration 22 tables:

- `intraday_provider_capability_receipts`;
- `intraday_artifact_manifests`;
- `intraday_coverage_receipts`; and
- `legacy_policy_classifications`.

Use immutable IDs/content hashes, foreign-key identity where available,
append-only timestamps, unique constraints preventing duplicate evidence, and
JSON payloads only for genuinely extensible fields. Put indexed query fields in
real columns.

**C004.** Store raw compressed artifacts outside SQLite at the configured
`DAWNSTRIKE_INTRADAY_EVIDENCE_ROOT`, partitioned by provider/feed/kind/date/
symbol/content hash. SQLite stores indexes, lineage, hashes, and status—not
large blobs. Refuse persistence when provider terms/entitlement do not permit
retention.

**C005.** Add `intraday_scanner/storage/intraday_evidence_store.py` rather than
adding another thousand lines to `sqlite_store.py`. Expose the narrow facade
needed by existing services without breaking old call sites.

**C006.** Make writes idempotent by content hash and atomic by temporary file
plus rename. A conflicting artifact with the same identity and different hash
must be `SOURCE_CONFLICT`, never last-write-wins.

**C007.** Provider/data coverage statuses must include at least:

- `COMPLETE`;
- `PARTIAL_MISSING_INTERVALS`;
- `NO_DATA`;
- `KNOWN_HALT_GAPS`;
- `ENTITLEMENT_DENIED`;
- `SOURCE_CONFLICT`;
- `CORPORATE_ACTION_UNRESOLVED`;
- `HASH_MISMATCH`;
- `FUTURE_DATA_REJECTED`; and
- `DATA_INELIGIBLE`.

This enum answers only whether the required evidence exists. It must not contain
strategy results such as target-first, stop-first, or not-triggered. Those
belong exclusively to Stage E `path_truth_status`. No catch-all success status
may hide partial data.

**C008.** Rehearse migration 21-to-22 twice on two disposable copies of the
Stage A snapshot. Require schema 22, `quick_check=ok`, unchanged pre-existing
row counts/hashes, second-run idempotence, and successful reopen by the old
read paths. Do not migrate active state.

**C009.** Add tests for serialization, time zones/DST, early closes, immutable
hashes, conflict handling, migration, rollback-from-backup, and unchanged daily
DataTruth/PaperOps behavior.

**C010.** Run all gates and commit:

```powershell
$stageFiles = @(
  'docs/architecture/intraday_evidence_spine.md',
  'intraday_scanner/storage/migrations.py',
  'intraday_scanner/storage/sqlite_store.py',
  'intraday_scanner/storage/intraday_evidence_store.py',
  'intraday_scanner/v2/contracts/data.py',
  'intraday_scanner/v2/data/market.py',
  'intraday_scanner/v2/data_truth/__init__.py',
  'intraday_scanner/v2/data_truth/models.py',
  'intraday_scanner/v2/data_truth/intraday.py',
  'tests/test_intraday_evidence_contracts.py',
  'tests/test_intraday_evidence_store.py',
  'tests/test_intraday_evidence_migration.py'
)
git add -- $stageFiles
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add immutable intraday evidence contracts"
```

## Stage D — provider acquisition and entitlement proof

**D001.** Add a `HistoricalIntradayProvider` protocol in
`intraday_scanner/providers/base.py` covering capability probe, bars, trades,
quotes, corporate actions, and pagination. Individual capabilities may be
unavailable, but absence must be explicit.

**D002.** Extend `intraday_scanner/providers/alpaca_provider.py` rather than
creating a duplicate Alpaca client. Fix complete pagination and retain exact
`iex` versus `sip` feed identity.

**D003.** Add `intraday_scanner/providers/massive_market_data_provider.py` using
read-only market-data endpoints only. Support `MASSIVE_API_KEY` as primary and
`POLYGON_API_KEY` as a compatibility alias. Never log either value. Do not add
any trading API.

**D004.** Update `intraday_scanner/config.py`, `.env.example`,
`intraday_scanner/network_safety.py`, and `docs/PROVIDER_SETUP.md` with
capability names, feed identity, evidence-root path, timeouts, pagination,
rate-limit behavior, and secret-safe setup.

**D005.** Implement `scripts/probe_intraday_provider.py`. It must emit a
non-secret content-hashed receipt for:

- credential presence, never value;
- plan/entitlement response;
- earliest available bars/trades/quotes;
- delayed versus real-time status;
- IEX versus consolidated coverage;
- extended-hours coverage;
- delisted/symbol-change/corporate-action coverage;
- pagination limits;
- raw-data storage/retention permission, recorded from operator-provided
  entitlement metadata; and
- estimated request/byte volume, without purchasing anything.

**D006.** If no approved Massive key exists, do not create one or buy a plan.
Use fixtures to finish the adapter and report
`BLOCKED_EXTERNAL_MARKET_DATA_ENTITLEMENT` for Massive live proof.

**D007.** Probe the already configured Alpaca market-data runtime through the
existing secret-loading wrapper without printing environment values. Historical
SIP older than the provider's recency boundary may be available, while current
SIP and full quote capabilities depend on entitlement. Trust the probe, not an
assumption.

**D008.** Implement paginated acquisition with bounded retry/backoff, 429
handling, request receipts, checksum validation, atomic storage, and restart
from the last verified page. Do not silently substitute Yahoo or IEX for a
requested consolidated feed.

**D009.** Normalize exchange-calendar-aware regular and extended sessions.
Use `intraday_scanner/market_calendar.py`; do not assume every day has 390
minutes. A missing bar is not automatically a halt, and a halt is recognized
only from ticker/time-linked status evidence.

**D010.** Add recorded provider fixtures for pagination, rate limiting, partial
pages, delisted symbols, symbol changes, splits, early close, halt/resume,
duplicate prints, out-of-order timestamps, and insufficient entitlement.

**D011.** Gate provider completion on 100% accounting of the frozen cohort,
zero unclassified/hash-conflict/future rows, and retained artifact lineage for
every eligible row. Ambiguity may be nonzero and must be reported.

**D012.** Run all gates and commit:

```powershell
$stageFiles = @(
  '.env.example',
  'docs/PROVIDER_SETUP.md',
  'intraday_scanner/config.py',
  'intraday_scanner/network_safety.py',
  'intraday_scanner/providers/base.py',
  'intraday_scanner/providers/alpaca_provider.py',
  'intraday_scanner/providers/massive_market_data_provider.py',
  'intraday_scanner/storage/intraday_evidence_store.py',
  'intraday_scanner/v2/data_truth/intraday.py',
  'scripts/probe_intraday_provider.py',
  'scripts/backfill_intraday_evidence.py',
  'tests/test_intraday_provider_capabilities.py',
  'tests/test_alpaca_intraday_provider.py',
  'tests/test_massive_market_data_provider.py'
)
git add -- $stageFiles
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: acquire retained intraday market evidence"
```

If live acquisition is blocked, commit only the fixture-proven adapter and
capability receipt schema; do not claim the backfill is complete.

## Stage E — one canonical path-aware outcome engine

**E001.** Extract a pure deterministic resolver into
`intraday_scanner/alpha/path_replay.py` from the mounted logic in
`alpha_outcome_capture_service.py`. The service remains orchestration; it must
call the same resolver as historical replay, paper reconciliation, V6 labeling,
and tests.

**E002.** Freeze golden fixtures for current behavior before changing it:

- target first;
- stop first;
- same-minute both touched;
- trigger-minute/entry-bar ambiguity;
- gap through trigger;
- gap through stop;
- not triggered;
- missing minute;
- exact halt/resume interval;
- early close;
- split; and
- symbol change.

**E003.** Separate fact from policy:

- `path_truth_status` records what evidence proves;
- `conservative_policy_result` may resolve an ambiguous bar stop-first for a
  deterministic paper ledger;
- these fields may not overwrite one another.

The closed path-truth enum must include `RESOLVED_TARGET_FIRST`,
`RESOLVED_STOP_FIRST`, `SAME_MINUTE_AMBIGUOUS`, `ENTRY_BAR_AMBIGUOUS`,
`NOT_TRIGGERED`, `MISSING_DECISION_TIME`, `MISSING_LEVELS`, `MISSING_BARS`,
`KNOWN_HALT_WINDOW`, `CORPORATE_ACTION_UNRESOLVED`, `SOURCE_CONFLICT`, and
`DATA_INELIGIBLE`.

**E004.** Do not include the trigger bar's pre-entry extrema. If trade sequence
evidence cannot order entry and extrema, emit `ENTRY_BAR_AMBIGUOUS` and store
bounds/nulls rather than a fabricated exact MFE/MAE.

**E005.** For a verified activated path, persist entry policy/version, entry
time/price/source, first-touch status, stop/target touch times, MFE/MAE and
timestamps or honest bounds, time-to-touch, session exit, halt exposure,
quote/spread/cost lineage, and raw/normalized artifact hashes.

**E006.** A no-trigger row has null position-only MFE/MAE. A missing-data row is
not a no-trigger row. A provider-confirmed halt gap is not silently filled.

**E007.** Add migration 23 and an `alpha_path_replays` append-only table keyed
by cohort/selection/policy/artifact hash. Register counterfactual exit policies
before evaluation and keep their outputs separate from official outcomes.

**E008.** Keep legacy `paper_positions` immutable, including their null MFE/MAE.
Add `paper_position_excursion_reconciliations` in migration 23 and write verified
MFE/MAE, bounds, path replay ID, source hashes, and reconciliation receipt there.
Expose a read-only joined view/API for consumers. Do not populate no-trigger
rows and do not update the original eight positions.

**E009.** Backfill only the frozen official outcome-required cohort plus the
predeclared counterfactual sample. Tag legacy V4 replays
`retrospective_replay`. Do not convert all ranked candidates into official
outcomes.

**E010.** Introduce two orthogonal eligibility dimensions:

- `retrospective_research_eligible`; and
- `prospective_promotion_eligible`.

Retrospective rows may support hypothesis generation/training only after source,
benchmark, cost, and reconciliation gates pass. They contribute zero to the
forward-session/promotion minimum.

**E011.** Require parity among mounted EOD capture, historical replay, paper
reconciliation, and V6 label construction for identical inputs and policy
version.

**E012.** Run all gates and commit:

```powershell
$stageFiles = @(
  'intraday_scanner/alpha/path_replay.py',
  'intraday_scanner/services/alpha_outcome_capture_service.py',
  'intraday_scanner/services/alpha_paper_reconciliation_service.py',
  'intraday_scanner/alpha/v6/label_builder.py',
  'intraday_scanner/storage/migrations.py',
  'intraday_scanner/storage/intraday_evidence_store.py',
  'scripts/backfill_intraday_evidence.py',
  'tests/test_alpha_path_replay.py',
  'tests/test_alpha_outcome_capture_service.py',
  'tests/test_alpha_paper_reconciliation.py',
  'tests/test_alpha_v6_labels.py',
  'tests/test_intraday_evidence_migration.py'
)
git add -- $stageFiles
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: unify path-aware AlphaOps outcome truth"
```

## Stage F — backtest the exact official AlphaOps strategy

**F001.** Freeze the target as official `alphaops_v5`. Do not port V4 under a
generic name and do not mix V2 daily strategies into its results.

**F002.** Add a versioned strategy adapter at
`intraday_scanner/v2/strategies/alphaops_intraday.py` that calls the same V5
policy/decision contract used by mounted AlphaOps. Do not copy the formulas.

**F003.** Require point-in-time inputs for every decision: universe membership,
premarket snapshot, source evidence, float/market cap, news/catalyst, SEC/halt/
corporate-action state, quote freshness/spread, policy version, and decision
timestamp. Missing historical safety fields produce `DATA_INELIGIBLE`, not a
backfilled guess.

**F004.** Add `intraday_scanner/v2/backtest/intraday_engine.py` alongside the
daily engine. Do not destabilize `engine.py` or its daily outputs.

**F005.** Use an asynchronous event clock. Thin/halted symbols do not need an
identical timestamp grid. Process only events known at or before the event time.

**F006.** Model the causal sequence:

1. point-in-time universe and feature snapshot;
2. decision timestamp;
3. first eligible entry event after the decision;
4. quote-aware modeled fill;
5. stop, target, halt, time, or session-close exit;
6. costs and cash/position accounting; and
7. immutable result receipt.

Stage F uses the exact frozen V5 cost contract—50 bps per side plus the current
per-share commission—as a clearly labeled provisional baseline. It must report
`COST_MODEL_PROVISIONAL`. Quote-aware engine interfaces are implemented here,
but empirical calibration and adverse-cost promotion evaluation remain
`NOT_EVALUABLE_PENDING_EMPIRICAL_COST` until Stage G completes G009–G011.

**F007.** Support overlapping positions, portfolio cash/equity, symbol and total
exposure, liquidity/participation caps, session boundaries, corporate actions,
and fail-closed missing data.

**F008.** Split metrics correctly:

- intraday marked equity for drawdown, tail, and exposure;
- one session-close portfolio return per exchange session for Sharpe/Sortino,
  annualized at 252;
- elapsed-time CAGR when defined; and
- `NOT_ANNUALIZABLE` for an unsupported sampling series.

Never apply `sqrt(252*390)` as a blanket fix.

**F009.** Add timestamp-aligned benchmarks:

- cash/risk-free;
- SPY and IWM over the exact entry/exit horizon;
- a causal equal-weight eligible candidate-universe comparator; and
- sign inversion as a diagnostic only.

Any short benchmark must be separately feasibility-qualified for borrow,
locate, SSR, halt, and asymmetric fills. It is not an execution strategy.

**F010.** Use grouped chronological expanding walk-forward folds with market
date as the primary indivisible split unit. Purge/embargo overlapping holding
windows. Keep duplicate ticker-date observations together. Use ticker across
dates for concentration and robustness analysis; do not globally prevent a
ticker from appearing in different chronological folds. Maintain an immutable
trial registry and one untouched holdout evaluated once.

**F011.** Report cluster-bootstrap confidence intervals by market date,
after-cost benchmark excess, profit-factor interval, drawdown/CVaR/tail stress,
symbol/day concentration, turnover/capacity, fold/regime/source/liquidity
stability, and multiple-trial-adjusted Sharpe.

**F012.** Promotion-quality evidence requires all of:

- registered strategy and trial before evaluation;
- minimum forward sessions and minimum trades;
- at least 98% eligible source/cost/benchmark coverage;
- positive lower confidence bound for after-cost benchmark excess under base
  and adverse-cost stress;
- acceptable drawdown, tail, concentration, and capacity limits;
- all folds disclosed;
- untouched holdout used once; and
- explicit manual approval.

Failure keeps the challenger in research/shadow; it does not stop forward
observation and never enables a broker.

Every numeric limit comes from the hashed, pre-outcome Stage A research
protocol. If any required value remains `REQUIRES_OPERATOR_APPROVAL`, emit
`NOT_EVALUABLE_PENDING_PROTOCOL_APPROVAL`; do not choose it from the results.
Re-run the complete F012 evaluation after Stage G empirical-cost work passes.

**F013.** Add parity tests between the adapter and saved mounted V5 decisions,
plus no-lookahead, asynchronous-clock, cost, portfolio, benchmark, annualization,
halt, early-close, and corporate-action tests.

**F014.** Run all gates and commit:

```powershell
$stageFiles = @(
  'intraday_scanner/v2/strategies/__init__.py',
  'intraday_scanner/v2/strategies/alphaops_intraday.py',
  'intraday_scanner/v2/backtest/__init__.py',
  'intraday_scanner/v2/backtest/intraday_engine.py',
  'intraday_scanner/v2/backtest/intraday_metrics.py',
  'intraday_scanner/v2/paper_ops/experiment_registry.py',
  'tests/test_alphaops_intraday_adapter.py',
  'tests/test_intraday_backtest_engine.py',
  'tests/test_intraday_backtest_metrics.py',
  'tests/test_alpha_v6_no_lookahead.py'
)
git add -- $stageFiles
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add causal AlphaOps V5 intraday replay"
```

## Stage G — risk geometry and empirical cost challengers

**G001.** Preserve current V5 as champion. Add a named, versioned shadow level
challenger in `intraday_scanner/alpha/risk_geometry.py`.

**G002.** Add `atr_wilder()` alongside the existing SMA true-range ATR in
`intraday_scanner/v2/indicators/core.py`. Golden-test both definitions and never
silently change existing V2 strategy results.

**G003.** Build ATR only from completed causal bars. Record ATR method,
timeframe, session, cutoff timestamp, warmup count, source artifact hash, and
policy version. Reject stale, incomplete, zero-volume, insufficient-warmup, or
post-decision inputs.

**G004.** Do not use regular-session opening-range low or VWAP in a premarket
decision. If those inputs are part of the challenger, add a separate post-open
arming event after the configured window closes; the morning row remains
watch-only until the plan is frozen.

**G005.** Compute versioned structural stop candidates. Require `stop < entry`,
enforce the preregistered ATR-noise floor, and reject a defensible stop that
exceeds the maximum distance. Never clamp/move an invalid stop inward just to
make a trade pass.

**G006.** Estimate target policies from training-only, as-of joint first-touch
paths. Do not set price equal to an MFE percentile and do not calculate a
percentile on the full dataset. Require effective sample size, shrinkage,
uncertainty interval, fold stability, and a target frozen before entry.

**G007.** Reuse V5 sizing. Planned quantity is bounded by modeled all-in loss
per share, symbol-notional limit, and available simulated cash. Store planned
versus realized R. Never claim gaps/halts make realized loss impossible beyond
1R.

**G008.** Fix signed daily-P&L semantics in
`intraday_scanner/risk/policy.py`: positive gains may not become losses through
`abs()`. Distinguish blocking a new entry from liquidating an existing paper
position.

**G009.** Add an empirical cost component using observed quote/trade evidence:
spread, feed, bid/ask sizes, exchanges, latency assumption, participation,
commission/fees, modeled impact, and fill-feasibility status. Keep observed
cost, modeled impact, and 1.5x/2x stress scenarios separate.

**G010.** Call a quote NBBO only with SIP/consolidated evidence. IEX remains
IEX. Do not claim depth/impact from top-of-book alone. A halted exit occurs at
the first sourced tradable observation after resume, not inside the halt.

**G011.** Gate the challenger on complete provenance, frozen plan, causal
ATR/structure, independently estimated target, walk-forward after-cost lower
confidence bound above zero, and planned risk/notional limits. “Survives 200
bps” alone is not a promotion rule. Every effective-sample, stability,
uncertainty, risk, capacity, and concentration threshold comes from the hashed
Stage A research protocol; unresolved values make the verdict
`NOT_EVALUABLE_PENDING_PROTOCOL_APPROVAL`.

**G012.** Run all gates and commit:

```powershell
$stageFiles = @(
  'intraday_scanner/alpha/risk_geometry.py',
  'intraday_scanner/alpha/execution_cost.py',
  'intraday_scanner/alpha/v5_policy.py',
  'intraday_scanner/risk/policy.py',
  'intraday_scanner/v2/indicators/core.py',
  'tests/test_alpha_risk_geometry.py',
  'tests/test_alpha_execution_cost.py',
  'tests/test_alpha_v5_policy.py',
  'tests/test_risk_policy.py',
  'tests/test_v2_indicators.py'
)
git add -- $stageFiles
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add shadow risk and cost evidence challengers"
```

## Stage H — point-in-time catalyst evidence

**H001.** Extend `intraday_scanner/providers/sec_edgar_provider.py` to retain
CIK, accession, form, amendment status, SEC acceptance timestamp, filing date,
8-K items, primary-document URL, fetched/first-seen time, and immutable content
hash.

**H002.** Fetch and hash the primary filing document under SEC fair-access
rules. Store raw content outside SQLite and reference its path/hash. Never rely
only on form name or title when document terms are required.

**H003.** Add normalized filing facts with explicit unknowns: security type,
gross amount, price, share count, ATM capacity/remaining amount, warrant count/
strike/expiry, reverse split, and relevant offering/takedown terms. Verify
arithmetic deterministically.

**H004.** Replace latest-article-only behavior with all relevant point-in-time
articles, deduplicated by canonical URL/content hash and ordered by published
time and first-seen time.

**H005.** Use a multi-label taxonomy: event type, polarity, financing/dilution
mechanism, novelty, timing, source/coverage status, promotional/rumor status,
squeeze mechanics, and confidence. Keep `no_news`, `provider_failed`,
`insufficient_text`, `conflicting_sources`, and `unclassified` distinct.

**H006.** Add `intraday_scanner/ai/catalyst_claim_extractor.py` by reusing the
strict factual-extraction boundary from the Scenario extractor. Cache by source
hash plus prompt/schema/model version. Require exact evidence spans and reject
prompt injection/contract violations.

**H007.** The extractor may not output score, grade, probability, prediction,
trade direction, target, stop, size, or recommendation. Deterministic code alone
maps verified facts to registered research features.

**H008.** Add migration 24 tables `catalyst_evidence_events` and
`catalyst_claim_extractions`, with append-only content hashes and point-in-time
availability. A post-decision filing is new information, not retroactive input.

**H009.** Register S-3/424B5-inside-72-hours as an avoid-long research feature,
segmented by security type and actual takedown terms. Do not add a fade/short
route.

**H010.** Create a frozen human-reviewed validation set. Gate on source-window
coverage, extraction precision/recall by label, abstention calibration, conflict
handling, category effective sample sizes, and out-of-sample incremental lift.
Do not impose a maximum `no_clear` percentage. Freeze the reviewed-set identity
and exact precision/recall/coverage/effective-N thresholds in the Stage A
protocol before evaluating extractor output; unresolved values make this gate
pending operator approval.

**H011.** Add tests for amendments, multiple filings, S-3 without takedown,
424B5 terms, 8-K items, warrants, reverse splits, conflicting articles,
provider failure, no news, publication/first-seen causality, cache identity,
injection, and no-action output schema.

**H012.** Run all gates and commit:

```powershell
$stageFiles = @(
  'docs/architecture/catalyst_evidence.md',
  'intraday_scanner/ai/catalyst_claim_extractor.py',
  'intraday_scanner/providers/sec_edgar_provider.py',
  'intraday_scanner/services/candidate_news_service.py',
  'intraday_scanner/services/catalyst_evidence_service.py',
  'intraday_scanner/storage/migrations.py',
  'intraday_scanner/storage/catalyst_evidence_store.py',
  'tests/test_catalyst_claim_extractor.py',
  'tests/test_catalyst_evidence_service.py',
  'tests/test_sec_edgar_safety.py',
  'tests/test_intraday_evidence_migration.py'
)
git add -- $stageFiles
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: retain point-in-time catalyst evidence"
```

## Stage I — activate V6 research honestly

**I001.** Extend existing V6 components; do not replace them:

- `intraday_scanner/alpha/v6/label_builder.py`;
- `intraday_scanner/alpha/v6/dataset_builder.py`;
- `intraday_scanner/alpha/v6/training.py`;
- `intraday_scanner/alpha/v6/validation.py`; and
- `intraday_scanner/services/alpha_v6_learning_service.py`.

**I002.** Carry source artifact hashes, path-replay ID, benchmark hash, observed/
modeled cost identity, evidence cohort, and both research/promotion eligibility
dimensions into outcomes, labels, datasets, and model receipts.

**I002a.** Add additive migration 25 for the indexed V6 evidence-lineage and
eligibility columns required by I002. Preserve existing payloads and old read
paths; do not rewrite historical label values.

**I003.** Generate return labels only when complete sourced outcome,
independent reconciliation, after-cost return, benchmark alignment, and causal
decision identity exist. Do not convert activation/fill-feasibility labels into
return labels.

**I004.** Preserve minimums: at least 100 eligible return rows before return
model training; at least 500 labels and 60 dates before gradient boosting.
Retrospective rows may satisfy a separately named research-training dataset
minimum but never the forward/promotion minimum.

**I005.** Keep purged/embargoed date-grouped walk-forward validation, minimum
common-fold predictions, cost/drawdown/CVaR/concentration/calibration/capacity/
stability gates, immutable trial registry, and one-time holdout enforcement.
Do not reduce governance to “two folds.”

**I006.** Add catalyst as a candidate feature block. Compare full, no-catalyst,
catalyst-only, and shuffled/negative-control ablations. Do not declare catalyst
dominant unless untouched out-of-sample evidence says so.

**I007.** Keep automatic promotion false, manual review required, V6 shadow
only, and broker execution false regardless of model result.

**I008.** If label gates remain unmet, the correct output is
`NOT_TRAINED_INSUFFICIENT_LABELS` with exact exclusions—not a reduced threshold.

**I009.** Run all V6 tests, full gates, and commit:

```powershell
$stageFiles = @(
  'intraday_scanner/alpha/v6/models.py',
  'intraday_scanner/alpha/v6/label_builder.py',
  'intraday_scanner/alpha/v6/dataset_builder.py',
  'intraday_scanner/alpha/v6/training.py',
  'intraday_scanner/alpha/v6/validation.py',
  'intraday_scanner/services/alpha_v6_learning_service.py',
  'intraday_scanner/storage/migrations.py',
  'intraday_scanner/storage/intraday_evidence_store.py',
  'tests/test_alpha_v6_labels.py',
  'tests/test_alpha_v6_dataset_splits.py',
  'tests/test_alpha_v6_training.py',
  'tests/test_alpha_v6_no_lookahead.py',
  'tests/test_alpha_v6_holdout_service.py',
  'tests/test_intraday_evidence_migration.py'
)
git add -- $stageFiles
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: bind V6 research to reconciled intraday evidence"
```

## Stage J — deterministic diagnostic attribution

**J001.** Extend `intraday_scanner/services/alpha_attribution_service.py`; do
not replace its existing observational-limit behavior.

**J002.** Add migration 26 tables:

- `trade_attribution_cases`; and
- `trade_attribution_factors`.

Require one immutable case per reconciled closed trade and zero-to-many factors.
Do not force exactly one cause.

**J003.** Factor status is one of `observed_defect`, `supported_contributor`,
`suspected`, `unknown`, or `not_applicable`. Store evidence links/hashes,
evaluator version, confidence basis, and registered counterfactual policy.

**J004.** Use only frozen decision-time risk/catalyst inputs for setup
diagnostics. Realized post-entry ATR cannot prove the original level was wrong.

**J005.** Execution attribution requires intent/order timestamps, quote/trade
feed identity, sizes, halt/LULD state, latency, and modeled-versus-observed cost.
Yahoo polling plus flat slippage is insufficient.

**J006.** Replace “variance/good trade that lost” with
`unexplained_within_predeclared_model_distribution`. A single trade cannot prove
causal correctness or randomness.

**J007.** Do not update a parameter from an individual trade. Aggregate only
after preregistered minimum effective N, uncertainty intervals, sequential/
multiple-test controls, and walk-forward/holdout confirmation. Emit a shadow
remediation candidate requiring human review. All numeric rules must already be
frozen in the Stage A research protocol; otherwise emit
`NOT_EVALUABLE_PENDING_PROTOCOL_APPROVAL`.

**J008.** Gate attribution reports on separate complete/partial/unknown
coverage, no missing-to-zero coercion, no unsupported unique causal claim, and
no automatic policy mutation.

**J009.** Run all attribution tests, full gates, and commit:

```powershell
$stageFiles = @(
  'intraday_scanner/services/alpha_attribution_service.py',
  'intraday_scanner/storage/migrations.py',
  'intraday_scanner/storage/attribution_evidence_store.py',
  'tests/test_alpha_attribution_service.py',
  'tests/test_trade_attribution_evidence.py',
  'tests/test_intraday_evidence_migration.py'
)
git add -- $stageFiles
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add evidence-linked diagnostic attribution"
```

## Stage K — final proof, review packet, and stop before release

**K001.** Re-run the exact CI-equivalent gates:

```powershell
py -m pip check
py -m pytest -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts
node --check web/assets/dawnstrike.js
git diff --check
```

**K002.** Run the security gates from `.github/workflows/ci.yml`:

```powershell
py -m pip_audit -r requirements.lock
py -m bandit -r intraday_scanner scripts -ll -b config/security/bandit-baseline.json
py -m cyclonedx_py environment --pyproject pyproject.toml `
  --output-reproducible --output-file "$evidenceRoot\sbom.cdx.json"
Get-FileHash "$evidenceRoot\sbom.cdx.json" -Algorithm SHA256
```

Parse every `scripts/*.ps1` file with the PowerShell parser and run the tracked
secret-baseline check without printing secrets.

**K003.** Rehearse migrations 21-to-26 twice on fresh copies of the Stage A
snapshot. Require `quick_check=ok`, deterministic schema and counts, idempotent
second initialization, old-read compatibility, and recovery from the untouched
snapshot. Never point this rehearsal at active state.

**K004.** Re-run frozen current-behavior parity for:

- V2 daily backtests and PaperOps;
- mounted AlphaOps V5 decisions;
- official cohort identity;
- current one-minute outcome behavior;
- public/dashboard contracts; and
- live-order hard locks.

**K005.** Run an adversarial no-lookahead audit. Search every new join and
feature for availability timestamps. A feature observed after decision is a
hard failure even if performance improves.

**K006.** Produce tracked sanitized packets
`docs/audit/evidence/luna_evidence_spine_final_20260809.json` and
`docs/audit/evidence/luna_evidence_spine_final_20260809.md` containing:

- starting SHA, implementation HEAD before the proof commit, and that
  implementation HEAD's pre-proof tree hash;
- all commits and changed files;
- baseline and final gate results;
- migration/recovery proof;
- provider/feed/entitlement capability receipts without secrets;
- cohort manifest/count/hash;
- artifact coverage and all ambiguity/missing/conflict statuses;
- replay parity and backfill counts by evidence cohort;
- V5 backtest/benchmark/cost coverage and uncertainty, without profitability
  claims beyond measured evidence;
- risk/cost/catalyst challenger status;
- V6 label exclusions, dataset counts, folds, and holdout state;
- attribution complete/partial/unknown counts;
- proof that no broker/order surface was added;
- exact external blockers; and
- explicit statement that active runtime/state/tasks/publication were not
  changed.

Include the K006 checkpoint SHA-256 of the external JSONL execution ledger and
the hashes of task XML, DB snapshot, and SBOM—not their private/raw contents.
Resolve the two non-self-referential implementation identities before writing
the packet:

```powershell
$implementationHead = (git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to resolve implementation HEAD' }
$implementationTree = (git rev-parse "${implementationHead}^{tree}").Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to resolve implementation tree' }
```

**K007.** Commit the final sanitized proof packet:

```powershell
$stageFiles = @(
  'docs/audit/evidence/luna_evidence_spine_final_20260809.json',
  'docs/audit/evidence/luna_evidence_spine_final_20260809.md'
)
git add -- $stageFiles
git diff --cached --name-only
git diff --cached --check
git commit -m "docs: record evidence-spine completion proof"
if ($LASTEXITCODE -ne 0) { throw 'Final proof commit failed' }
$proofCommitSha = (git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to resolve final proof commit SHA' }
```

Do not modify the committed packet after this commit. Record K007 and later
steps, including `$proofCommitSha`, only in the external JSONL ledger and Luna's
final response. A packet cannot contain the SHA of the commit that contains the
packet itself.

**K008.** Verify the branch is clean and every phase is represented by an
intentional commit:

```powershell
$finalStatus = git status --short
if ($LASTEXITCODE -ne 0) { throw 'Final worktree status failed' }
if ($finalStatus) { throw "STOP_DIRTY_ISOLATED_WORKTREE final status: $finalStatus" }
git log --oneline --decorate $auditedSha..HEAD
git diff --stat $auditedSha..HEAD
git diff --check $auditedSha..HEAD
```

Hash the final external JSONL ledger after this verification and report that
hash in Luna's final response; do not rewrite the committed packet to insert it.

**K009.** Stop with status `READY_FOR_OPERATOR_REVIEW`. Do not push, deploy,
migrate active state, run a live backfill into active SQLite, change Task
Scheduler, or publish.

## Separate release gate — not authorized by this directive

If and only if the operator later explicitly authorizes release, Luna must
start a new release run that:

1. fetches and revalidates current `origin/main` and the approved commit;
2. snapshots/hash-checks active SQLite and exports task XML;
3. rehearses every migration twice on disposable current-state copies;
4. deploys one clean exact SHA to an isolated staged runtime;
5. runs all runtime doctors and no-broker/order-lock tests;
6. obtains explicit approval before active migration or task changes;
7. makes a recoverable exact-state change with rollback receipts;
8. verifies observation, notification, outcome, and paper-ledger counts without
   fabricating a trade or return; and
9. leaves every challenger shadow-only until its evidence and manual-promotion
   gates pass.

## Definition of done

The implementation is complete only when Stages A through K have passing proof
or an honest external entitlement block, all safe fixture-backed work is done,
the isolated branch is clean, daily V2 and mounted V5 parity are preserved,
ambiguity/missing truth remains explicit, no forward gate uses retrospective
counts, and no broker or active release mutation occurred.

“Code exists,” “tests should pass,” “234 signals backfilled,” “ML activated,”
“strategy fixed,” or “ready for live trading” are not acceptable completion
claims.
