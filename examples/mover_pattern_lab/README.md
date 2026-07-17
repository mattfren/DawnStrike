# Mover Pattern Lab input templates

These files contain headers only. They are schemas, not sample or production
market data, and they deliberately contain no invented values.

`bars_template.csv` uses timezone-aware **bar-close** timestamps. With the
default five-minute interval, the regular-session grid begins at `09:35` ET and
continues `09:40`, `09:45`, and so on through the requested cutoff. `09:30` is
the open of the first interval, not a bar-close row. Every expected bar must be
present. The immediately preceding published market session must be present
through its official close for `previous_close`; the same-clock RVOL baseline
requires at least the configured number of complete prior sessions.

`context_template.csv` may contain multiple rows for one symbol/day. The lab
uses the latest `context_observed_at` no later than the cutoff, and that row must
be no more than five minutes old. All timestamps require an explicit UTC offset
or `Z`.

Use blank values for unknown spread, split, offering, halt, conflict, or
catalyst facts. Never write `false` or `clear` merely because a source did not
report a risk. Unknown required safety truth produces a skipped decision and a
`null` return.

`spread_pct` is measured in **percentage points**: `0.50` means a 0.50% spread,
not a decimal ratio of 0.50 and not 50 basis points encoded as `50`.

`reverse_split_lookback_clear=true` means a retained source explicitly verified
no reverse split in the strategy's 91-day lookback. Likewise,
`offering_lookback_clear=true` means a retained source explicitly verified no
offering/dilution event in the 31-day lookback. The optional day-count fields
describe known events; they do not substitute for a verified-clear result.

When `catalyst_verified=true`, publication time, source URL, source type, and
`catalyst_artifact_ref` are all required. The publication must have been known
by `context_observed_at` and the cutoff. The artifact ref has the form
`sha256:<canonical-json-sha256>:<absolute-json-path>` and must also appear in
the semicolon-delimited `source_refs` field.

Allowed `universe_selection_method` values:

- `premarket_screen`
- `scheduled_universe`
- `prior_session_watchlist`
- `live_intraday_scan`

An EOD winner list or closing rank is not a prospective universe and is
rejected.

For `forward_observation`, `universe_source_ref` must also be an immutable
`sha256:<hash>:<path>` JSON artifact ref and must appear in `source_refs`.
Historical replay may use honest historical lineage, but its results remain
separate from forward evidence.

## All-candidate study templates

The three JSON templates are intentionally empty; fill them only with retained
truth. Empty templates fail closed and cannot generate a study.

`universe_denominators_template.json` is a JSON array (JSONL is also accepted at
runtime). It needs one object per exact date/cutoff:

```json
{
  "market_date": "YYYY-MM-DD",
  "feature_cutoff_at": "timezone-aware cutoff",
  "expected_symbols": ["sorted", "unique", "symbols"],
  "source_ref": "sha256:<raw-or-canonical-hash>:<absolute-path>",
  "expected_symbols_complete": true
}
```

Set `expected_symbols_complete=true` only when the retained source proves the
entire prospective universe at that cutoff. A partial watchlist must say
`false`.

`split_assignments_template.json` maps every supplied `snapshot_id` exactly
once to `discovery`, `validation`, or `locked_test`:

```json
{
  "assignments": {
    "<snapshot_id>": "discovery"
  }
}
```

Freeze this file before reading outcomes. Do not move dates between splits as
new rows arrive.

`descriptive_eod_movers_template.json` is optional and accepts an array/JSONL
of realized EOD rows. Each row requires `market_date`, `symbol`, `mover_rank`,
`expected_row_count`, `dataset_role="descriptive_eod_movers"`,
`source_snapshot_kind="realized_eod_gainers"`, `source_complete=true`,
`list_coverage_complete`, and a hash-valid `source_ref`. Every rank from 1
through `expected_row_count` must be present under one source ref before a
highest-mover-versus-control comparison is eligible.
