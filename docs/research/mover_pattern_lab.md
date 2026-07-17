# Mover Pattern Lab operator guide

## Purpose and boundary

The Mover Pattern Lab is Dawnstrike's deterministic research and paper-audit
workflow for intraday mover hypotheses. It freezes what was knowable at a market
cutoff, retains every strategy decision, records simulated signals, reconciles
those signals against retained bars, and reports each strategy day by day.

It does **not** submit orders, connect to a broker, automate trading, promise a
return, or provide personalized investment advice. A `paper signal` is an
immutable research observation, not an instruction to trade.

The current frozen catalog contains:

| Strategy identity | Display name | Frozen observation |
|---|---|---|
| `mover_opening_drive_rvol_v1@v1.0` | Opening Drive + Same-Clock RVOL | Completed opening drive near its range high, above VWAP, with time-aligned volume and liquidity gates |
| `mover_verified_catalyst_gap_hold_v1@v1.0` | Verified Catalyst Gap Hold | Split-adjusted catalyst gap holding its opening range and VWAP with pre-cutoff catalyst lineage |

Changing a threshold or rule requires a new version. Historical results from a
changed rule must never be silently attached to an existing strategy identity.
The research rationale is in
[`mover_pattern_hypotheses.md`](mover_pattern_hypotheses.md).

## Evidence modes

Every snapshot, decision, signal, and reconciled observation carries one mode:

- `forward_observation`: evidence genuinely system-received at the live cutoff.
  Core records the authoritative process receipt between the cutoff and five
  minutes after it, rejects bars or context after the cutoff, and binds the
  receipt to input hashes. Only complete, after-cost forward evidence can be
  considered by the manual review gates.
- `historical_replay`: deterministic reconstruction for debugging and research.
  Its metrics are reported separately and it can never promote a strategy.

Do not run a historical file after close and label it forward. One forward build
should contain one cutoff, because one capture timestamp is bound to one live
observation. A replay may include several cutoffs.

## Required inputs

The header-only templates are in `examples/mover_pattern_lab`.

1. `bars.csv` contains timezone-aware OHLCV rows whose timestamps mean
   **bar close**. With five-minute bars, the exact regular-session grid begins
   `09:35`, `09:40`, `09:45`, and continues at five-minute intervals. Naive
   timestamps, missing grid rows, duplicates, non-positive volume, and invalid
   OHLC relationships are rejected.
2. `context.csv` contains point-in-time universe, spread, corporate-action,
   halt, conflict, and catalyst truth. The selected context row must be no more
   than five minutes old at the cutoff.
3. The immediately preceding published market session must have a complete bar
   set through its official close. Otherwise `previous_close` remains missing
   and strategies skip.
4. Same-clock RVOL requires the configured number of complete prior sessions
   through the same clock time. Missing baseline truth is not zero RVOL.

`spread_pct` is in percentage points. `0.50` means 0.50%.

Forward universe and verified-catalyst provenance must use an existing JSON
artifact ref:

```text
sha256:<64-character canonical-JSON fingerprint>:<absolute JSON path>
```

The file must still exist, parse as JSON, and reproduce the stated canonical
fingerprint. Put each ref in the semicolon-delimited `source_refs` field. The
forward `universe_source_ref` must be one of those refs. A verified catalyst
also requires its URL and `catalyst_artifact_ref` in `source_refs`.

## Install and initialize

From the repository root:

```powershell
py -m pip install -e ".[dev]"
py -m intraday_scanner.v2.mover_pattern_lab init `
  --output-root data\v2_mover_pattern_lab
```

The default output root is `data/v2_mover_pattern_lab`. Initialization writes
the frozen catalog and feature-contract registries. It does not install a
schedule.

Audit retained legacy data before relying on it:

```powershell
py -m intraday_scanner.v2.mover_pattern_lab audit `
  --db-path data\shadow_real.sqlite `
  --output-root data\v2_mover_pattern_lab
```

An audit exit code of `2` means blocked truth, not a software success disguised
as zero return. Legacy EOD labels, fixture-contaminated rows, or outcomes with
insufficient lineage stay ineligible.

## Daily forward workflow

### 1. Scan at the cutoff

Capture the real bar/context inputs at one live cutoff and run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\mover-pattern-lab\run_daily.ps1 `
  -Stage Scan `
  -BarsCsv data\inbox\mover_lab\bars_2026-07-20_0945.csv `
  -ContextCsv data\inbox\mover_lab\context_2026-07-20_0945.csv `
  -MarketDate 2026-07-20 `
  -Cutoffs 09:45 `
  -EvidenceMode forward_observation
```

The JSON result contains `signals_path`, `decisions_path`, and the immutable
`scan_manifest_path`. Retain that output. A day with zero signals may still be a
valid fully evaluated day; inspect decision reasons before treating it as such.
If reconciliation starts in a new shell, resolve the immutable paths from the
latest pointer rather than passing the pointer itself to analysis:

```powershell
$scan = Get-Content data\v2_mover_pattern_lab\manifests\paper_scan_latest.json |
  ConvertFrom-Json
$scan.signals_path
$scan.run_manifest_path
```

Equivalent direct commands:

```powershell
py -m intraday_scanner.v2.mover_pattern_lab build-snapshots `
  --bars-csv data\inbox\mover_lab\bars_2026-07-20_0945.csv `
  --context-csv data\inbox\mover_lab\context_2026-07-20_0945.csv `
  --date 2026-07-20 `
  --cutoffs 09:45 `
  --bar-interval-minutes 5 `
  --bar-timestamp-semantics bar_close `
  --evidence-mode forward_observation `
  --output-root data\v2_mover_pattern_lab

py -m intraday_scanner.v2.mover_pattern_lab paper-scan `
  --snapshots data\v2_mover_pattern_lab\snapshots\prospective_2026-07-20_<hash>.jsonl `
  --expected-market-dates 2026-07-20 `
  --output-root data\v2_mover_pattern_lab
```

`--expected-market-dates` is mandatory. It makes an expected session with no
snapshot visible as `not_evaluated` instead of silently dropping it.
The scan wrapper therefore retains an empty snapshot ledger and still creates a
paper-scan manifest when snapshot build fails closed; its top-level status stays
`blocked`, while the expected date remains visible for reconciliation/calendar
reporting.

### 2. Reconcile after the session

After the published session closes, provide the complete bar-close grid and the
exact signal/scan-manifest paths returned above:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\mover-pattern-lab\run_daily.ps1 `
  -Stage Reconcile `
  -SignalsPath data\v2_mover_pattern_lab\signals\signals_<hash>.jsonl `
  -ScanManifest data\v2_mover_pattern_lab\manifests\paper_scan_<hash>.json `
  -BarsCsv data\inbox\mover_lab\bars_2026-07-20_full_session.csv
```

Equivalent direct commands:

```powershell
py -m intraday_scanner.v2.mover_pattern_lab reconcile `
  --signals data\v2_mover_pattern_lab\signals\signals_<hash>.jsonl `
  --bars-csv data\inbox\mover_lab\bars_2026-07-20_full_session.csv `
  --bar-interval-minutes 5 `
  --bar-timestamp-semantics bar_close `
  --notional-per-trade 1000 `
  --slippage-bps 10 `
  --fee-bps 1 `
  --output-root data\v2_mover_pattern_lab

py -m intraday_scanner.v2.mover_pattern_lab analyze `
  --scan-manifest data\v2_mover_pattern_lab\manifests\paper_scan_<hash>.json `
  --reconcile-manifest data\v2_mover_pattern_lab\manifests\reconcile_<hash>.json `
  --output-root data\v2_mover_pattern_lab

py -m intraday_scanner.v2.mover_pattern_lab verify `
  --output-root data\v2_mover_pattern_lab
```

Analysis requires the immutable scan and reconciliation run manifests. It does
not accept an arbitrary filtered trade file. Those manifests bind the exact
snapshots, decisions, signals, bars, costs, and observations used in the report.
Do not substitute a mutable `*_latest.json` pointer.

The reconcile wrapper returns `calendar_html_path`. Open that retained file to
click a date and inspect the strategy-level status and percentage return. A
blank/not-evaluated day remains visibly null rather than looking like 0%.

## Historical replay workflow

Replay is explicit and isolated:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\mover-pattern-lab\run_scan.ps1 `
  -BarsCsv data\research\bars.csv `
  -ContextCsv data\research\context.csv `
  -MarketDate 2026-07-20 `
  -Cutoffs 09:45,10:00,12:00,15:00 `
  -EvidenceMode historical_replay
```

Do not pass `SourceCapturedAt` merely to make a replay look forward. Replay
trades appear only under historical replay metrics and cannot satisfy forward
promotion gates.

## All-candidate and highest-mover study

Strategy signals alone are a selected population. Use `study-candidates` to
label **every retained prospective candidate snapshot** under the same
next-bar-open policy, including candidates that a strategy skipped or rejected.
The study records 5/15/30/60-minute returns, official-close returns, MFE, MAE,
candidate ranks, missing bars, coverage, and discovery-only correlations.

The study requires:

- the exact retained snapshot JSONL from snapshot build;
- a full outcome bar-close CSV;
- one caller-attested universe denominator for every exact date/cutoff;
- a frozen snapshot-ID assignment covering every supplied snapshot exactly
  once with `discovery`, `validation`, or `locked_test`;
- optionally, a complete descriptive EOD-gainers list with immutable source
  lineage.

Run it after close:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\mover-pattern-lab\run_daily.ps1 `
  -Stage StudyCandidates `
  -BarsCsv data\inbox\mover_lab\bars_2026-07-20_full_session.csv `
  -SnapshotsPath data\v2_mover_pattern_lab\snapshots\prospective_2026-07-20_<hash>.jsonl `
  -UniverseDenominators data\inbox\mover_lab\universe_denominators_2026-07-20.json `
  -SplitAssignments data\research\mover_lab\frozen_split_assignments.json `
  -DescriptiveEodMovers data\inbox\mover_lab\realized_eod_gainers_2026-07-20.json
```

Equivalent direct command:

```powershell
py -m intraday_scanner.v2.mover_pattern_lab study-candidates `
  --snapshots data\v2_mover_pattern_lab\snapshots\prospective_2026-07-20_<hash>.jsonl `
  --bars-csv data\inbox\mover_lab\bars_2026-07-20_full_session.csv `
  --universe-denominators data\inbox\mover_lab\universe_denominators_2026-07-20.json `
  --split-assignments data\research\mover_lab\frozen_split_assignments.json `
  --descriptive-eod-movers data\inbox\mover_lab\realized_eod_gainers_2026-07-20.json `
  --bar-interval-minutes 5 `
  --bar-timestamp-semantics bar_close `
  --slippage-bps 10 `
  --fee-bps 1 `
  --output-root data\v2_mover_pattern_lab
```

The EOD list is descriptive only and can never create a prospective signal.
A highest-mover-versus-control comparison is applicable only when the
prospective universe has 100% snapshot/outcome coverage **and** the EOD source
proves a complete, contiguous ranked list. Partial lists remain unavailable,
not negative controls. Correlations use the frozen discovery assignment only;
the command never creates or promotes a strategy automatically.

One candidate study may contain exactly one `evidence_mode`; mixed forward and
replay ledgers are rejected. The manifest exposes
`general_mover_research_data_complete` for complete coverage and the stricter
`forward_learning_eligible`, which is true only when that complete evidence is
also `forward_observation`.

## Outcome semantics

- Simulated forward entry is the first complete interval whose grid-aligned
  open is not earlier than the authoritative source receipt. The entry
  timestamp identifies that interval's start; `entry_source_bar_at` identifies
  its close timestamp.
- Exit is same-session and follows the frozen stop/target/EOD logic. Slippage
  and fees are explicit scenario assumptions, not claims of executable fills.
- `closed` has a sourced after-cost return.
- `not_entered` means no valid next-bar entry under the contract.
- `pending_*` means required outcome truth is not yet available. Its return is
  `null`.
- A calendar cell is `not_evaluated`/`null` if the expected day lacks data or
  any required truth caused a skip.
- A fully evaluated day with no emitted setup may have a 0% paper-book return.
  That zero means no position, not an imputed outcome.

The report keeps trade-level and day-level metrics separate. Manual review uses
frozen chronological splits, clustered day-book inference, source coverage,
after-cost expectancy, and lower confidence bounds. Automatic promotion remains
disabled even if every numerical gate passes.

## Output map

| Directory | Retained evidence |
|---|---|
| `manifests/` | Frozen catalog, feature contract, paper-scan run manifests, reconciliation run manifests, latest pointers |
| `source_artifacts/` | Content-addressed bar/context/outcome evidence |
| `snapshots/by_id/` | Immutable point-in-time snapshots |
| `decisions/by_id/` | Every strategy evaluation, including skip/reject/no-setup reasons |
| `signals/by_id/` | Immutable simulated paper signals |
| `signals/session_registry/` | First-signal claim for each strategy/version/symbol/session |
| `trades/by_observation/` | Reconciliation observations; different cost scenarios remain different observations |
| `reports/` | Immutable analysis JSON/Markdown and clickable daily strategy calendar HTML plus JSON/CSV |
| `reports/candidate_studies/` | All-candidate study, coverage, mover/control comparisons, discovery correlations |
| `trades/candidate_outcomes/` | One independent outcome label for every supplied candidate snapshot |
| `qa/verify_latest.json` | Evidence-integrity checks; not a performance endorsement |

## When no paper signals appear

Inspect `rejected_path` from snapshot build and `decisions_path` from paper scan.
Common fail-closed causes are:

- context missing, stale, or timestamped after the cutoff;
- naive/missing/duplicate bar-close timestamps;
- incomplete same-clock baseline or missing immediately prior session close;
- unknown spread, split adjustment, halt, source-conflict, reverse-split, or
  offering status;
- a verified catalyst missing publication, URL, type, or immutable artifact;
- a forward universe source without an immutable retained JSON artifact;
- a hard risk veto or ordinary setup rule not met;
- the session registry already contains the first signal for that identity.

For candidate-study gaps, inspect `all_candidate_coverage_complete`, the
coverage CSV, `missing_symbols`, and `pending_reason`. A partial denominator or
incomplete EOD list must not be upgraded to a valid control population.

Do not weaken a gate merely to manufacture activity. The product goal is a
complete, comparable paper record—including honest no-signal and not-evaluated
days—not a forced daily pick.

## Credentials, providers, and scheduling

The repository does not include paid-data credentials or secretly infer missing
risk facts. Without a real point-in-time provider for bars, spreads, halts,
corporate actions/offerings, universe selection, and catalyst artifacts, forward
operation fails closed. Public current-gainer pages and end-of-day winner lists
are descriptive research inputs, not prospective signal evidence.

The manual PowerShell scripts run one requested stage and print JSON. The
separate configured operator in
[`../operations/mover_pattern_daily_workflow.md`](../operations/mover_pattern_daily_workflow.md)
adds per-cutoff state, cumulative analysis, durable Telegram delivery, and a
preview-first Windows Task Scheduler registrar. Registration occurs only with
its explicit `-Apply` switch. Neither path registers a broker job or enables
orders. Real provider inputs and Telegram credentials remain operator-supplied.
