You are normalizing manually collected outcome prices into Dawnstrike's manual
outcome CSV.

Rules:
- Output CSV only. No markdown table, notes, or commentary.
- Do not invent missing prices.
- Leave unknown fields blank.
- Prices must come from my broker, screener, or chart notes, not from your memory.
- `entry_time` must be the actual timestamp after the saved recommendation.
- If a value is missing, leave it blank; Dawnstrike will mark that metric
  unavailable instead of treating it as zero.

Output exactly these columns, in this order:

```csv
date,ticker,entry_time,entry_price,price_1m,price_5m,price_15m,lunch_price,close_price,high_after_entry,low_after_entry,halted,source,notes
```

Paste my manual outcome notes below and return the CSV.
