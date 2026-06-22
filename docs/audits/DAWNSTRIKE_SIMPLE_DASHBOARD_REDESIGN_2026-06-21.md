# Dawnstrike Simple Dashboard Redesign - 2026-06-21

## Summary

Redesigned the Streamlit dashboard around a simple operator flow:

- `Today`
- `Picks`
- `Calendar`
- `Performance`
- `System`

The default `Today` tab now answers the core question quickly: whether there is
a clean watchlist, the main pick, the top three names, the levels that matter,
what is risky or missing, and what to do next.

This pass did not change broker, order, or trading behavior. Dawnstrike remains
research/watchlist only and does not place orders.

## Files Changed

- `app.py`
  - Replaced the default dashboard tab structure with `Today`, `Picks`,
    `Calendar`, `Performance`, and `System`.
  - Added the simplified `Today`, `Picks`, `Performance`, and `System` views.
  - Moved dense technical/admin views into the `System` area and advanced
    expanders.
  - Reworked calendar summaries and day drilldowns into plain-English cards.
- `intraday_scanner/dashboard/components.py`
  - Added reusable display components: status banner, main pick card, top-three
    cards, next-steps panel, risk summary, compact picks/avoid tables, outcome
    reminder, evidence card, source status card, and calendar day card.
- `intraday_scanner/dashboard/display_text.py`
  - Added layman label translations for technical scanner labels and source
    labels.
- `intraday_scanner/dashboard/data_loader.py`
  - Added display-ready objects: `latest_status`, `main_pick`, `top_three`,
    `risk_summary`, `next_steps`, `missing_outcomes`,
    `performance_summary`, `system_health`, and simplified calendar day rows.
  - Treated no-trade AlphaOps rows as no-pick display state instead of
    watchlist picks.
  - Treated an empty/new DB as no data/no edge instead of a red source failure.
- `tests/test_streamlit_app.py`
  - Updated AppTest tab expectations for the simplified tab structure.
- `tests/test_simple_dashboard.py`
  - Added offline dashboard display-contract tests.
- `tests/test_historical_calendar.py`
  - Made the source safety read use UTF-8 so UI emoji text does not fail on
    Windows cp1252 defaults.

## Before / After

Before:

- Main page exposed too many dense cards, raw scanner tables, and technical
  internal labels.
- Raw phrases such as `LOW_CONFIDENCE`, `INSUFFICIENT_SAMPLE`, and
  `No-Trade Reason: Clean` could reach operator-facing surfaces.
- The avoid list and technical AlphaOps/source internals competed with the
  actual daily decision.

After:

- `Today` starts with a single status banner, main pick, top three watchlist
  cards, next steps, risk summary, and a research-only footer.
- `Picks` uses compact readable tables; full avoid/debug details are hidden by
  default.
- `Calendar` shows day cards, outcome-needed status, top pick, and audited
  return only when outcome data exists.
- `Performance` answers "Is this working yet?" and warns when there are fewer
  than 20 audited real days.
- `System` holds run controls, source/admin diagnostics, logs, history, and
  advanced settings.

## New Tab Structure

- `Today`: simple daily operating screen.
- `Picks`: readable watchlist, avoid list, notifications, and advanced raw
  details.
- `Calendar`: historical day status and outcome tracking.
- `Performance`: evidence and return summaries from audited outcome data.
- `System`: data sources, automation, run controls, logs, history, and
  diagnostics.

## Layman Label Translations

Examples now translated for dashboard display:

- `LOW_CONFIDENCE` -> `Low confidence`
- `INSUFFICIENT_SAMPLE` -> `Not enough history yet`
- `NO_EDGE` -> `No clear edge`
- `unknown_float` -> `Float unknown`
- `no_previous_close` -> `Previous close missing`
- `url_table_unverified` -> `Free web source - verify manually`
- `halt_status_unverified` -> `Halt status not checked`
- `sec_risk_unverified` -> `SEC risk not checked`
- `previous_close_unavailable` -> `Previous close missing`
- `premarket_range_unavailable_price_used` ->
  `No premarket range; using current price`
- `intelligence_gap_and_crap_risk` -> `Low-quality gap risk`
- `gap_below_min` -> `Gap too small`
- `source conflict` -> `Data sources disagree`
- `Clean` -> `No hard risk flags`
- `web_url` -> `Unverified free web data`

For an empty or clean no-pick reason, the dashboard now says:

`No hard risk flags, but confidence was not high enough.`

## Tests Run

- `py -m pip install -e ".[dev]"`
  - PASS
- `py -m pytest -p no:cacheprovider`
  - PASS: `164 passed in 30.38s`
- `py -m ruff check .`
  - PASS
- `py -m mypy intraday_scanner`
  - PASS: `Success: no issues found in 91 source files`
- `py -m compileall intraday_scanner app.py tests`
  - PASS
- `git diff --check`
  - PASS; only CRLF normalization warnings from Git on Windows.

## Dashboard Smoke Result

- Restarted Streamlit on port `8502`.
- `curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8502/`
  returned `200`.
- Browser smoke result:
  - URL: `http://127.0.0.1:8502/`
  - Title: `Dawnstrike`
  - `Dawnstrike` visible: PASS
  - `Today` visible: PASS
  - `Top 3 Watchlist` visible: PASS
  - `What To Do Next` visible: PASS
  - No-orders footer visible: PASS
  - Traceback / duplicate Streamlit element error visible: PASS, none found

## No-Trading Safety Result

Command run:

```powershell
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation|market_order|limit_order|execute_trade|orders_enabled" intraday_scanner app.py scripts tests docs README.md
```

Result:

- Broad search matched only safety tests and documentation/audit text that
  prohibits order execution or records prior safety-search commands.
- Implementation-only search over `intraday_scanner app.py scripts` returned
  no matches.
- No broker order API, trading client, order submission path, market/limit order
  helper, auto-trading path, or buy/sell recommendation implementation was
  added.

## Remaining Limitations

- The dashboard still depends on persisted scan/outcome data. It does not
  invent returns when outcomes are missing.
- Performance evidence remains intentionally conservative until at least 20
  audited real market days are collected.
- Raw/debug details still exist, but are kept under `System` or advanced
  expanders so the default operator flow stays simple.
