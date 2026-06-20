You are normalizing a manually copied premarket screener table into Dawnstrike's
canonical snapshot CSV.

Rules:
- Output CSV only. No markdown table, notes, or commentary.
- Do not invent missing values.
- Leave unknown fields blank.
- Use the source table as the source of truth for prices and volume.
- If `premarket_price` and `premarket_volume` exist, calculate `dollar_volume`.
- If `previous_close` and `premarket_price` exist, calculate `gap_pct`.
- If source is missing, set `source` to `manual_upload`.
- If timestamp is missing, ask me for the timestamp instead of guessing.
- Do not fabricate float, market cap, short float, halt status, offering status,
  reverse split status, catalyst headline, or catalyst URL.

Output exactly these columns, in this order:

```csv
ticker,company,previous_close,premarket_price,premarket_high,premarket_low,premarket_volume,dollar_volume,gap_pct,float_shares,market_cap,spread_pct,short_float_pct,has_news,catalyst_headline,catalyst_url,current_halt,recent_offering,reverse_split_90d,source,as_of_timestamp
```

Paste my copied screener rows below and return the canonical CSV.
