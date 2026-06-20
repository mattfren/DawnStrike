# Manual Uploads

Manual uploads let you test Dawnstrike before paying for data.

Use:

- `templates/manual_premarket_snapshot_template.csv`
- `templates/manual_outcomes_template.csv`
- `templates/chatgpt_screener_to_snapshot_prompt.md`
- `templates/chatgpt_outcomes_to_csv_prompt.md`

For raw screener exports, prefer the automation importer in
`docs\SCREENER_AUTOMATION.md`. It accepts common exported aliases such as
`Symbol`, `Last`, `Prev Close`, `Pre-Market Volume`, `News`, and `URL`, then
writes the canonical snapshot for you.

## Snapshot Rules

The canonical snapshot columns are:

```csv
ticker,company,previous_close,premarket_price,premarket_high,premarket_low,premarket_volume,dollar_volume,gap_pct,float_shares,market_cap,spread_pct,short_float_pct,has_news,catalyst_headline,catalyst_url,current_halt,recent_offering,reverse_split_90d,source,as_of_timestamp
```

If `dollar_volume` is blank and price/volume exist, Dawnstrike calculates it.
If `gap_pct` is blank and previous close/price exist, Dawnstrike calculates it.

Missing enrichment fields stay unknown. Do not fabricate float, short interest,
halt status, offering status, reverse splits, or catalysts.

Raw screener automation also stores the raw file path and import timestamp on
normalized rows so dashboard and audit screens can trace where the data came
from.

## Outcome Rules

Manual outcome columns are:

```csv
date,ticker,entry_time,entry_price,price_1m,price_5m,price_15m,lunch_price,close_price,high_after_entry,low_after_entry,halted,source,notes
```

Outcome rows must match a saved recommendation and must not be timestamped before
that recommendation. Missing prices are marked unavailable for that metric; they
are not treated as zero.
