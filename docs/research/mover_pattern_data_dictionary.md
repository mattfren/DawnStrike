# Mover Pattern Lab data dictionary

This dictionary defines the operator-facing semantics for Mover Pattern Lab
inputs and principal outputs. Blank, absent, or unverifiable facts remain
`null`. Missing truth must never be encoded as zero, `false`, or `clear`.

## Bars CSV

| Field | Type | Meaning and validation |
|---|---|---|
| `symbol` | string | Uppercase US-equity symbol accepted by the shared OHLCV loader. |
| `timestamp` | timezone-aware ISO 8601 | **Bar close**, never bar open. Naive values are rejected. For five-minute RTH data, exact closes are `09:35`, `09:40`, `09:45`, … in `America/New_York`; `09:30` is not a completed bar. |
| `open` | positive number | First observed price in the interval ending at `timestamp`. |
| `high` | positive number | Must be at least `open`, `low`, and `close`. |
| `low` | positive number | Must be no greater than `open`, `high`, and `close`. |
| `close` | positive number | Last observed price in the interval ending at `timestamp`. |
| `volume` | positive number | Interval volume. Zero/negative volume is rejected as non-executable truth. |

The declared `bar_interval_minutes` controls the required grid. Gaps and
duplicate timestamps fail closed. Early closes follow the published market
calendar. The immediately prior published session—not merely the latest row in
the file—must be complete through its official close to supply
`previous_close`.

## Context CSV

| Field | Type | Meaning and validation |
|---|---|---|
| `market_date` | `YYYY-MM-DD` | Published US-equity trading session. |
| `symbol` | string | Symbol to join with bars. |
| `context_observed_at` | aware datetime | When this exact context row was observed. Must be at/before the cutoff and no more than five minutes old. |
| `universe_selected_at` | aware datetime | When the symbol entered the prospective universe. Must be at/before the cutoff. |
| `universe_source_ref` | string | Source that proves prospective universe membership. In forward mode this must be a retained `sha256:<fingerprint>:<absolute-json-path>` ref and appear in `source_refs`. EOD winner rank is forbidden. |
| `universe_selection_method` | enum | One of `premarket_screen`, `scheduled_universe`, `prior_session_watchlist`, or `live_intraday_scan`. |
| `spread_pct` | non-negative number/null | Bid-ask spread in **percentage points**. `0.50` means 0.50%. Unknown is null. |
| `split_adjusted` | bool/null | Whether price history was explicitly adjusted consistently for splits. `false` vetoes; null skips. |
| `reverse_split_days` | integer/null | Optional age of a known reverse split. Descriptive only; it does not prove the lookback is clear. |
| `reverse_split_lookback_clear` | bool/null | `true` only when a retained source explicitly verifies no reverse split in the 91-day strategy lookback. `false` vetoes; null skips. |
| `recent_offering_days` | integer/null | Optional age of a known offering/dilution event. Descriptive only. |
| `offering_lookback_clear` | bool/null | `true` only when a retained source explicitly verifies no offering/dilution event in the 31-day lookback. `false` vetoes; null skips. |
| `halt_state` | string | `clear` only when explicitly verified. `unknown`/`unverified` skips. Any non-clear risk state vetoes. |
| `source_conflict` | bool/null | `true` if retained sources disagree; vetoes. `false` must mean sources were actually reconciled. Null skips. |
| `catalyst_verified` | bool/null | Whether a real point-in-time catalyst was verified. The catalyst strategy requires `true`; unknown is not false. |
| `catalyst_published_at` | aware datetime/null | Original publication time. Must be no later than both `context_observed_at` and the feature cutoff. |
| `catalyst_source_url` | URL/string | Original authoritative source location. Required when catalyst is verified and retained in `source_refs`. |
| `catalyst_source_type` | string | Source class, such as an SEC filing or issuer release. Required when catalyst is verified. |
| `catalyst_artifact_ref` | string | Immutable JSON artifact ref required when catalyst is verified; must appear in `source_refs`. |
| `source_refs` | semicolon-delimited strings | All source lineage retained for the row. Hashed refs use `sha256:<canonical-json-fingerprint>:<absolute-json-path>`. |

Forbidden prospective input names include `close_return_pct`, `daily_high`,
`daily_low`, `eod_rank`, `final_change_pct`, `final_return_pct`, `future_high`,
`future_low`, `outcome`, and `outcome_return_pct`.

## Build-level fields

| Field | Type | Meaning |
|---|---|---|
| `bar_timestamp_semantics` | enum | Must be `bar_close`. There is no implicit/default open-time interpretation. |
| `bar_interval_minutes` | integer | Declared interval from 1 to 30 minutes. Must match reconciliation. |
| `cutoffs` | ET clocks | Snapshot cutoffs inside RTH. A snapshot requires a bar closing exactly at each cutoff. |
| `evidence_mode` | enum | `forward_observation` or `historical_replay`. Propagates through the complete evidence chain. |
| `source_captured_at` | aware datetime/null | Mandatory for forward evidence; between observed bar close and five minutes after cutoff, on the same ET market date. Optional replay metadata cannot turn replay into forward evidence. |
| `expected_market_dates` | list of dates | Required by paper scan. Creates explicit not-evaluated calendar rows when expected sessions lack snapshots. |

## Derived snapshot fields

| Field | Meaning |
|---|---|
| `snapshot_id` | Stable identity over symbol, date, cutoff, retained bar/context fingerprints, evidence mode, and capture time. |
| `observed_at` | Latest retained completed bar close at the cutoff. |
| `feature_cutoff_at` | Hard time boundary for every prospective feature. |
| `price` | Close of the cutoff bar. |
| `previous_close` | Official close from the immediately preceding published market session; null if unavailable. |
| `session_open` | Open of the first retained RTH interval. |
| `gap_pct` | `(session_open / previous_close - 1) * 100`; null when either input is missing. |
| `opening_range_high` / `opening_range_low` | High/low across completed intervals from the open through 09:45 ET. |
| `opening_range_complete` | True only when the exact grid through 09:45 is complete. |
| `running_vwap` | Volume-weighted typical price of retained RTH intervals through cutoff. It is a calculation, not proof of alpha. |
| `cumulative_volume` | RTH volume through cutoff. |
| `cumulative_dollar_volume` | Sum of typical price times interval volume through cutoff. |
| `same_clock_rvol` | Current RTH cumulative volume divided by median cumulative volume through the same clock over prior complete sessions. Null if baseline is insufficient. |
| `source_refs` | Complete retained lineage, including automatically generated bar/context JSON artifacts. |

## Decision and signal fields

| Field | Meaning |
|---|---|
| `decision` | `signal`, `rejected`, or `skipped`. Every frozen strategy produces a retained decision for every snapshot. |
| `reason` | Stable high-level explanation for the decision. |
| `missing_features` | Required truth absent at the cutoff. A missing feature causes a skip, not a failed trade. |
| `vetoes` | Hard safety/data or setup conditions that rejected the observation. |
| `signal_id` | Stable paper-signal identity over strategy/version/snapshot. |
| `strategy_semantics_fingerprint` | Fingerprint binding the signal to the exact frozen rule semantics. |
| `signal_at` | Snapshot cutoff/bar close. |
| `entry_rule` | Simulated next-bar-open rule; never an order instruction. |
| `stop_price` / `target_price` | Frozen simulated outcome boundaries. |
| `research_only` | Always true. |
| `broker_execution_enabled` | Always false. |

The session registry permits only the first signal for one
strategy/version/symbol/market-date identity across runs. Later matches are
retained as skipped decisions.

## Reconciliation fields

| Field | Meaning |
|---|---|
| `status` | `closed`, `not_entered`, or a `pending_*` state. Pending/non-closed rows retain null return. |
| `entry_at` | Start timestamp of the next interval whose open supplies the simulated fill. |
| `entry_source_bar_at` | Close timestamp of that source interval. With five-minute bars it equals `entry_at + 5 minutes`. |
| `exit_at` / `exit_source_bar_at` | Retained simulated exit time and source bar close. |
| `gross_return_pct` | Simulated return before explicit scenario costs. |
| `net_return_pct` | Simulated after-cost percentage return for closed observations only. |
| `notional_per_trade` | Paper notional scenario. It is not deployed capital. |
| `slippage_bps` / `fee_bps` | Explicit scenario assumptions. Different assumptions produce distinct observation identities. |
| `total_cost` | Recomputed simulated dollar cost under those assumptions. |
| `bars_evidence_sha256` | Fingerprint of the exact retained outcome-bar sequence. |
| `source_bar_sequence_complete` | Whether the required contiguous same-session grid was retained. |
| `observation_id` | Stable identity over the full reconciliation row, including costs and interval assumptions. |

## Analysis and calendar semantics

| Value | Meaning |
|---|---|
| `forward_observation` metrics | Closed, sourced, after-cost forward paper observations. Only these may enter manual review gates. |
| `historical_replay_metrics` | Replay results shown separately; never promotion evidence. |
| `not_evaluated` + null return | Expected day missing snapshots/data, or required truth caused any skip. It is not 0%. |
| evaluated no setup + 0% | Complete strategy-day evaluation emitted no position. Zero is a paper-book no-position return, not an imputed asset outcome. |
| closed strategy-day return | Sourced after-cost aggregate for that frozen strategy book on that date. |
| pending | Signal exists but required outcome truth is incomplete; return remains null. |

Analysis validates exact content-addressed scan and reconciliation manifests.
It rejects arbitrary subsets, mismatched signal ledgers, modified artifacts, and
unregistered strategy semantics. Trade-level metrics and day-level clustered
metrics are separate. Automatic strategy creation and automatic promotion are
disabled.

## All-candidate study inputs

`universe_denominators` accepts a JSON array or JSONL with one row per exact
date/cutoff:

| Field | Meaning |
|---|---|
| `market_date` | Trading session matching the cutoff's ET date. |
| `feature_cutoff_at` | Timezone-aware exact cutoff, aligned to the declared interval. |
| `expected_symbols` | Nonempty, sorted, unique full prospective universe at that cutoff. |
| `source_ref` | Hash-valid retained raw or canonical-JSON artifact proving the denominator. |
| `expected_symbols_complete` | Explicit bool; true only when the source proves the list is complete. |

`split_assignments` is a JSON object whose `assignments` map covers every
supplied `snapshot_id` exactly once. Allowed values are `discovery`,
`validation`, and `locked_test`. Freeze the mapping before reading outcomes;
new observations must not move old rows between splits.

Optional descriptive EOD mover rows use:

| Field | Meaning |
|---|---|
| `market_date` | Realized session date. |
| `symbol` | Realized mover symbol. |
| `mover_rank` | Positive rank. A complete list has every rank from 1 through expected count. |
| `expected_row_count` | Same positive list size on every row for the day. |
| `dataset_role` | Exactly `descriptive_eod_movers`; never signal eligible. |
| `source_snapshot_kind` | Exactly `realized_eod_gainers`. |
| `source_complete` | True only when each row's source observation is complete. |
| `list_coverage_complete` | Explicit bool proving the list as a whole is complete. |
| `source_ref` | One hash-valid retained artifact shared by the complete list. |

## All-candidate study outputs

| Field | Meaning |
|---|---|
| `status` | `complete` or a `pending_*` status for every supplied snapshot. Pending returns remain null. |
| `evidence_mode` | The single `forward_observation` or `historical_replay` mode shared by every supplied snapshot and outcome. Mixed ledgers are rejected. |
| `entry_at` / `entry_bar_close_at` | Next interval start and its source bar close. |
| `after_cost_return_5m_pct`, `15m`, `30m`, `60m` | Fixed-horizon outcome labels under one declared cost policy. |
| `after_cost_close_return_pct` | Same candidate policy through official session close. |
| `mfe_pct` / `mae_pct` | Maximum favorable/adverse excursion over retained post-entry same-session bars. |
| `candidate_return_rank` | Rank within the complete candidate population for the exact date/cutoff. Null when population truth is incomplete. |
| `eod_mover_matched` / `eod_mover_rank` | Descriptive join result; null/unavailable unless the EOD list proves its role and coverage. |
| `snapshot_coverage_pct` | Observed snapshot count divided by denominator count. |
| `complete_outcome_coverage_pct` | Complete candidate outcome count divided by denominator count. |
| `all_candidate_coverage_complete` | True only when every denominator group proves complete snapshots and complete outcomes. |
| `general_mover_research_data_complete` | Manifest gate that mirrors complete all-candidate research coverage, regardless of mode. |
| `forward_learning_eligible` | True only when research coverage is complete and the study's single mode is `forward_observation`. Replay is always false. |
| `mover_control_comparisons` | Highest-mover versus nonmatched-control summaries; applicable only with complete universe outcomes and complete EOD list. |
| `discovery_correlations` | Feature correlations over all candidate snapshots in the frozen discovery split, never an auto-created strategy. |

Candidate-study results are research labels, not performance claims. Their run
manifest binds snapshots, bars, denominators, split assignments, optional EOD
list, costs, and every emitted artifact.
