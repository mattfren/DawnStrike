# Telegram Notifications

Telegram messages are compact research/watchlist notifications. They do not
place orders, hold broker credentials, expose secrets, or tell you to buy or
sell.

## Message Types

### Dawnstrike Alpha Watch

What it means:

- AlphaOps found clean enough watchlist names.
- The message shows the top one to three picks, Alpha score, edge bucket, watch
  level, exit line, target, confidence, setup, and risk.

What to do:

- Open the dashboard.
- Review the source/data quality.
- Manually watch the levels.
- Decide outside Dawnstrike whether you want to do anything.

What not to do:

- Do not treat "Watch Level" as a buy command.
- Do not chase a vertical move just because a ticker appears.
- Do not ignore risk text or data quality warnings.

Outcome file needed?

- Yes, if the system saved picks and you want the model to learn from the day.

Example:

```text
Dawnstrike Alpha Watch
8:15 CT | Edge: HIGH | 3 picks
1) NOVA - Alpha 81.0 | HIGH
   Trigger $5.40 | Invalid $4.85 | Target $6.25
   Confidence INSUFFICIENT_SAMPLE | Setup grade:A|gap:hot_gap
No orders placed. Research only.
```

### Dawnstrike Alpha Check

What it means:

- AlphaOps did not find a clean enough edge.
- This is a valid no-trade/no-watchlist result.

What to do:

- Do not force a pick.
- Check `web-source-doctor` if the reason is source/data related.
- Use a manual CSV fallback if public sources failed.

What not to do:

- Do not assume the app failed just because it sent no-clean-edge.

Outcome file needed?

- Usually no, unless there were saved picks from another run that still need
  outcomes.

Example:

```text
Dawnstrike Alpha Check
No clean edge today.
Reason: Every clean candidate has low source confidence.
Next: Wait for source confirmation before alerting.
No orders placed. Research only.
```

### Dawnstrike Alert

What it means:

- A risk/monitor condition needs manual review.
- Example causes include invalidation, momentum failure, news/filing risk, or
  other caution events.

What to do:

- Review the ticker manually.
- Compare current price to watch level, exit line, and target.

What not to do:

- Do not treat the alert as an automated exit order.

Outcome file needed?

- Yes, if the ticker was part of a saved watchlist and the day needs outcome
  tracking.

Example:

```text
Dawnstrike Alert
NOVA - CAUTION
Reason: invalidated
Action: manual review
No orders placed.
```

### Outcome Data Needed

What it means:

- Dawnstrike saved picks, but outcome rows are missing.
- Returns stay pending until imported.

What to do:

- Add a CSV to `data\inbox\outcomes\outcomes_YYYY-MM-DD.csv`.
- Run import, audit, learn, and report commands.

What not to do:

- Do not treat missing outcomes as zero.
- Do not treat missing outcomes as proof the strategy worked or failed.

Outcome file needed?

- Yes.

Example:

```text
Outcome Data Needed
Save:
data\inbox\outcomes\outcomes_2026-06-21.csv

Tickers:
NOVA, RIFT, MOON

Needed:
entry, 1m, 5m, 15m, lunch, close, high, low
```

### Dawnstrike Shadow Results

What it means:

- The system summarized saved shadow/audited outcomes.
- If fewer than 20 real audited days exist, the message is still an early
  evidence warning.

What to do:

- Review the report and dashboard Performance tab.
- Keep collecting outcomes.

What not to do:

- Do not claim profitability from sample fixtures or too few days.

Outcome file needed?

- Not for the message itself, but missing outcomes reduce evidence quality.

Example:

```text
Dawnstrike Shadow Results
Top1 avg: n/a
Top3 avg: n/a
Top5 avg: n/a
Win rate: n/a
Sample: 7 days - insufficient sample
No orders placed. Research only.
```

### Dawnstrike Accuracy

What it means:

- The attribution engine summarized the historical signal ledger.
- Top1 and Top3 are paper/scenario returns from imported outcomes.
- Evidence stays `Not enough history yet` until at least 20 audited days exist.

What to do:

- Review `outputs\return_attribution` and the dashboard Historical Calendar.
- Import missing outcome files if the message says `Outcome Data Needed`.

What not to do:

- Do not treat scenario returns as actual executed trades.
- Do not count recommended returns unless a saved exit signal exists.

Command:

```powershell
py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist --notify telegram
```

Example:

```text
Dawnstrike Accuracy
Audited days: 5 / 20 needed
Top1: +3.20%
Top3: +1.10%
Win rate: 60.00%
Missing outcomes: 2
Evidence: Not enough history yet.
```

## Test Commands

Dry-run without secrets:

```powershell
py -m intraday_scanner.cli telegram-test --dry-run --db-path data\shadow_real.sqlite
```

Real Telegram send:

```powershell
py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite
```

Force only the Telegram test event through dedupe:

```powershell
py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite --force
```

Dry-run and real-send tests use separate event keys. `--force` bypasses only the
test-event dedupe key. Tokens and chat IDs are not printed or persisted in
notification payloads.

## Important Terms

- Watch Level: price area to watch manually. Not a buy command.
- Exit Line: area where the research setup failed. Not an automated stop order.
- Target: first paper/research target level.
- Confidence: evidence/data confidence, not certainty.
- No clean edge: the system did not find a clean enough watchlist.
- Public web data: unverified free source data.

## Safety Rule

Every Telegram message is research-only. Dawnstrike does not trade.
