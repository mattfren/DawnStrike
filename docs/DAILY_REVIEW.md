# Dawnstrike Daily Review

Dawnstrike Daily Review is the post-market truth check. It compares the names
Dawnstrike saved during the day with the day mover list and the imported outcome
rows. It is research/watchlist and paper-validation only. No orders are placed.

## What It Answers

- What Dawnstrike picked.
- Why each name was picked.
- What happened after the pick.
- Which daily top movers were caught, missed, avoided, or not available in data.
- Which picks still need outcomes before returns can be calculated.
- What lessons should become learning backfeed proposals.

## Commands

Collect or import top movers:

```powershell
py -m intraday_scanner.cli collect-daily-movers --date YYYY-MM-DD --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\daily_movers --persist --print
```

Run the review:

```powershell
py -m intraday_scanner.cli daily-review --date YYYY-MM-DD --db-path data\shadow_real.sqlite --out-dir outputs\daily_review --persist --print
```

Create learning proposals:

```powershell
py -m intraday_scanner.cli daily-review-learn --date YYYY-MM-DD --db-path data\shadow_real.sqlite --out-dir outputs\daily_review --persist --print
```

Print or send the compact day-review message:

```powershell
py -m intraday_scanner.cli daily-review-telegram --date YYYY-MM-DD --db-path data\shadow_real.sqlite --notify console
```

## Review Categories

- `CAUGHT_WINNER`: a daily top mover was in Dawnstrike Top1/Top3/Top5.
- `MISSED_WINNER`: a daily top mover was not picked.
- `AVOIDED_WINNER`: a daily top mover was blocked by the avoid list.
- `FALSE_POSITIVE`: a Dawnstrike pick had a negative or failed outcome.
- `CORRECT_AVOID`: an avoided name was weak or failed.
- `OUTCOME_NEEDED`: a pick has no outcome row yet.
- `NO_DATA`: the day has no usable mover or outcome data.

## Outcome Rules

Missing prices stay missing. They are not treated as zero. Return precision is
only calculated where outcome rows exist. If outcomes are missing, Dawnstrike
creates an outcome template under:

```text
outputs\daily_review\outcomes\outcomes_YYYY-MM-DD.csv
```

## Operator Dashboard

Open the operator dashboard and review `Today`, `Review`, `Calendar`, and
`Performance`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\open_command_center_production.ps1
```

The dashboard shows review status, top movers, pick review, missed winners,
outcome needs, and historical review trends in plain English from saved
operating data.
