# Dashboard Guide

Start the dashboard:

```powershell
py -m streamlit run app.py --server.port 8502
```

Open:

```text
http://127.0.0.1:8502/
```

The dashboard is research-only. It does not place orders.

## Today

Use `Today` first.

### Status Banner

The top banner tells you the main state:

- `Clean Watchlist Found`: there are watchlist names to review manually.
- `Watch Only / Needs Confirmation`: there are names, but they need extra
  confirmation.
- `No Clean Edge Today`: risk/data/score filters did not support a watchlist.
- `Outcome Data Needed`: saved picks are missing outcome CSV rows.
- `Data Source Problem`: latest source check failed or returned unusable data.

### Main Pick

The main pick card shows the number one watchlist name if one exists.

Important fields:

- Ticker
- Company
- Setup label
- Plain-English decision
- Price
- Watch Level
- Exit Line
- Target
- Confidence
- Data Quality
- Main risk

`Watch Level` is the price area to watch manually. It is not a buy command.
`Exit Line` means the setup failed below that area.

### Top 3 Watchlist

Shows exactly three compact cards. Each card shows:

- ticker
- opportunity score
- watch level
- exit line
- main risk
- label: Strong Watch, Watch Only, Risky, or Avoid

No 10-column raw table is shown on the default tab.

### What To Do Next

This checklist shows:

- scan ran
- data source worked
- Telegram sent
- watch levels manually
- add outcome file after close when needed

If outcomes are missing, it points to:

```text
data\inbox\outcomes\outcomes_YYYY-MM-DD.csv
```

### Risk Summary

Shows:

- avoid count
- top avoid reason
- data warning count
- missing outcome count

Use this to know whether the day is clean or messy.

## Picks

The `Picks` tab shows readable tables.

Readable watchlist columns:

- Rank
- Ticker
- Setup
- Score
- Gap
- Price
- Watch Level
- Exit Line
- Target
- Confidence
- Main Risk

The avoid list shows the top five by default:

- Ticker
- Why avoid?
- Gap
- Volume
- Risk

Use `Show full avoid list` only when you need more detail.

Latest notifications show the latest saved notices without exposing secrets.

Raw scanner/debug details live under `Advanced details`.

## Calendar

The `Calendar` tab is for historical accountability.

Top cards show:

- days tracked
- days with outcomes
- top3 total return
- best day
- worst day
- outcome needed count

Day cards show:

- date
- status
- top pick
- top3 return if audited
- outcome-needed badge when missing

Statuses:

- `No data`: no saved scan or AlphaOps data for that day.
- `No trade`: no-clean-edge/no-trade decision was saved.
- `Picks pending`: picks exist but outcomes are missing.
- `Partial outcomes`: some but not all outcomes exist.
- `Audited`: outcome data exists and audited returns can be shown.
- `Data problem`: source failure/status problem.

Missing outcomes stay pending. They are not counted as zero.

## Performance

The `Performance` tab answers: "Is this working yet?"

Cards include:

- real days tracked
- audited days
- top1 return
- top3 return
- win rate
- worst drawdown
- evidence status

If audited days are fewer than 20, the dashboard says:

```text
Not enough evidence yet. Collect at least 20 real market days.
```

Do not treat fixture/sample returns or incomplete outcomes as proof of a
profitable strategy.

Collapsible tables show:

- setup performance
- source performance
- risk flag impact

## System

Use `System` for technical/admin work:

- source status
- database path
- run controls
- 5-minute check
- backtest/audit controls
- history
- advanced settings
- raw diagnostics

This tab is intentionally more technical. It is not the first tab to use during
normal operation.

## Label Translation

| Technical label | Operator meaning |
| --- | --- |
| Alpha Score | Opportunity score |
| Source Reliability | Data quality |
| No-Trade | No clean edge |
| Missing Outcomes | Need outcome CSV |
| Insufficient Sample | Not enough history yet |
| Public web data | Unverified free source |
| Broker Watch | Watch Level |
| Risk Line | Exit Line |
| Expected Return | Paper Estimate |
| No-Trade Reason | Why No Pick? |

## What Action Should You Take?

Dawnstrike can tell you what to watch, what failed, and what data is missing.
It cannot decide for you. The operator action is always manual:

1. Read the watchlist.
2. Verify the source/data quality.
3. Review watch level, exit line, and target.
4. Make any broker decision yourself outside Dawnstrike.
5. Import outcomes after close.
6. Let the evidence build.
