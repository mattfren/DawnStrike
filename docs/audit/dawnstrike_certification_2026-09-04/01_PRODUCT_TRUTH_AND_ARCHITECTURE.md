# 01 — Product truth and architecture

Commit `14ca714f`.

## Product contract (reconstructed from repository evidence)

| Element | Established truth |
|---|---|
| **Target user** | A single technical operator running an intraday research loop on their own Windows machine |
| **Primary problem** | Which premarket movers deserve attention today, why, and did yesterday's reasoning actually hold up |
| **Intended outcome** | A ranked, explained watchlist plus an avoid list, monitored intraday, resolved into recorded outcomes that feed a gated learning loop |
| **Operating modes** | Offline/CSV research · historical backtest · forward paper research · live-data decision support |
| **Inputs** | Screener CSV inbox, allowlisted public tables (stockanalysis.com, tradingview.com), Alpaca market data, manual outcome CSVs, SEC/RSS/Nasdaq halt feeds |
| **Outputs** | `ranked_candidates.csv`, `top_explosive.csv`, `avoid_list.csv`, `scan_summary.json`, SQLite records, Telegram watchlist/status messages, Streamlit dashboard, a static public dashboard |
| **Explicit non-goals** | Order execution, broker routing, custody, financial advice |
| **Safety boundary** | Every artifact stamped `"research_only": true` and `"broker_execution_enabled": false` |

**Verified, not assumed.** There is no broker SDK, order route, or execution credential anywhere in
the tree. `README.md` states it plainly: *"It does not place trades and does not provide financial
advice."* Live receipts agree. **Real-money execution is `OUT_OF_SCOPE`** and its absence is not
scored as a defect.

### Claims the product makes, and whether they hold

| Claim | Status |
|---|---|
| "Ranks high-volatility premarket momentum names" | `PASS` |
| "Separates candidates from avoid/do-not-touch names" | `PASS` |
| "Supports offline paper audits" | `PASS` (with a default-entry-mode caveat — see 03) |
| "Stores everything in SQLite" | `PASS` |
| "Checks saved names every 5 minutes" (README) | **`FAIL`** — never executed once |
| "Missing outcomes remain pending and are never counted as zero" | `PASS` — verified in live data |
| "Fewer than 20 real shadow days shown as insufficient sample" | `PASS` — verified in the dashboard |
| "Does not place trades" | `PASS` |

The one materially false documented claim is the 5-minute monitoring cadence. `docs/AUTOMATION.md`
and `README.md` describe a schedule that has not run. Fixed under WP-1; the documentation is
accurate again *once deployed*, so it should not be reworded — it should be made true.

## Authority hierarchy

**Authoritative now:** `README.md`, `docs/DAWNSTRIKE_EXPLAINED.md`, `docs/OPERATOR_MANUAL.md`,
`docs/OPERATOR_RUNBOOK.md`, `docs/ARCHITECTURE.md`, `docs/TECHNICAL_ARCHITECTURE.md`,
`docs/DATA_CONTRACT.md`, `docs/operations/*` (all refreshed 2026-09-02),
`docs/quant-refactor/agent-operating-protocol.md`.

**Historical — must not be cited as current:**

- `docs/quant-refactor/32-final-independent-certification.md` (2026-08-17, `CERTIFICATION: READY`).
  Superseded: it recorded 3,166 passing tests; the suite is now 5,150 with 61 failures. Its own
  boundary section already disclaims empirical edge, promotion and deployment — that disclaimer was
  correct and remains so.
- `docs/audit/dawnstrike_opus5_audit_and_luna_execution_directive_2026-08-09.md` and
  `docs/remediation/DAWNSTRIKE_SOTA_REMEDIATION.md` (both untracked in the primary checkout, both
  audited `ba39a535`). **440 commits stale.** Several of their headline claims — daily-bar outcome
  resolution, 0.875 R:R clusters, "catalyst engine 100% non-functional" — describe a tree that no
  longer exists. They should be archived with a staleness banner, not deleted: their strategic
  direction was sound even where the specifics expired.

## Architecture and data flow (production path)

```
Windows Task Scheduler
  └─ run_alphaops_morning.ps1        08:00 CT
       ├─ web_collection / csv inbox / public tables ─▶ normalized_source_rows
       ├─ universe + premarket enrichment (Alpaca)    ─▶ candidates, raw_snapshots
       ├─ scoring.py + formula.py                     ─▶ ranked_candidates, avoid_list
       ├─ alpha_cycle_service._attach_authenticated_alpaca_structure ─▶ entry/stop/target legs
       ├─ plan_constructor + v5_policy + alert_gate    ─▶ alpha_signals, historical_signals
       ├─ writes alpha_cycle.json  ◀── validated by Test-DawnstrikeAlphaCycleArtifact
       └─ Telegram watchlist
  └─ run_alphaops_monitor.ps1        every 5 min      ✗ FAILED 344× — WP-1
       ├─ market calendar gate
       ├─ Record-MissedMonitorIntervals ─▶ monitor_interval_gaps
       ├─ Test-DawnstrikeAlphaCycleArtifact  ◀── the break
       ├─ cli alpha-monitor    (never ran)
       └─ cli trade-watch      (never ran) ─▶ trade_intents, paper_positions  [both empty]
  └─ run_alphaops_eod / daily_finalize  15:15 / 17:30
       ├─ alpha-capture-outcomes ─▶ signal_outcomes        [empty]
       ├─ alpha-paper-reconcile  ─▶ paper_account_daily_ledger
       ├─ alpha-learn            ─▶ strategy_learning_labels [empty]
       └─ publication ─▶ public_snapshot_versions ─▶ Vercel  (withheld when degraded)
```

Read surfaces: `app.py` (Streamlit, 4,828 lines, 5 tabs) and `web/` (static public dashboard).

## Parallel stacks — which one is real

A significant source of confusion. Four generations coexist:

| Stack | Role | On production path? |
|---|---|---|
| `alpha/` v4 | legacy research cohort (`alphaops_signal_research`) | partially — still scored |
| `alpha/` v5 | **official forward paper policy** | **Yes** |
| `alpha/v6/` | walk-forward ML with frozen artifacts and promotion gates | trained/scheduled, **all tables empty** |
| `v2/` (`paper_ops`, `backtest`, `strategies`) | research lab | **No** — no production caller |
| `v2/opportunity/` | newer validation/metrics layer | partially; some gates unreachable |

The most consequential instance: `v2/paper_ops/position_management.py` is the best quantitative
component in the repository and has **zero production callers** (see 03). This is a genuine
duplicate-implementation problem, not merely unused research code — the *worse* implementation is
the one in production.

## Historical incomplete-work reconciliation

Each item from the mission brief, resolved against this commit.

| # | Item | Current status | Prior evidence |
|---|---|---|---|
| 1 | Product-goal metadata still `paused` | **CLOSED** — no `product_goal`/`goal_status` field exists in code or config. "paused" survives only as prose in `docs/quant-refactor/04-execution-log.md` describing *superseded* increments | STALE |
| 2 | WP005-B frozen with seven matching hashes | **CLOSED** — `wp005-b-durable-gate-20260815.md` records **eight** frozen targets, each pre/post-gate SHA-256 identical | VALID but miscounted in the brief |
| 3 | Durable **646**-test packet prepared but not launched | **CLOSED** — the packet is **656** tests, and it **ran**: `656 passed, exit 0, 9102.9s`. "646" is a transcription error | MISLEADING |
| 4 | WP005-C paused | **CLOSED** — `19-wp005-increment-c-sol-audit.md`: `WP005-C ACCEPTED` | STALE |
| 5 | WP002 evidence overlapping WP005 evidence | **UNKNOWN** — not reachable within this audit's scope | — |
| 6 | Repair loop returning routine failures through the architecture authority | **UNKNOWN** — process artifact, not verifiable from code | — |
| 7 | Restart packet not fully reconstructible | **UNKNOWN** | — |
| 8 | Live paper functionality only partial | **CONFIRMED, worse than described** — it is not partial, it is zero: 25 forward days, 0 trades | VALID |
| 9 | Real-money readiness failed | **`OUT_OF_SCOPE`** — excluded by the authoritative contract; not a defect | — |
| 10 | Paper audit uses first eligible open, not the true breakout fill | **CONFIRMED** — `entry_mode` defaults to `open` (`config.py:70,302`, `paper_audit.py:19`). A correct `breakout` mode exists but is not the default | VALID |
| 11 | Expected-return / confidence sample-size capped or unproven | **CONFIRMED + extended** — capping and tiering are honest, but `win_probability_pct` returns **100.0** on zero evidence (never rendered; see 03) | VALID |
| 12 | Live scans need credentials **and** a manual symbol list | **PARTIALLY CONFIRMED** — Alpaca credentials are configured and the mover lane runs from public tables, but the **core universe requires a manifest** that production does not supply, yielding `core_universe_status: DATA_UNAVAILABLE` — the upstream condition behind WP-1 | VALID |
| 13 | Full-market mover discovery absent | **CLOSED** — `services/mover_discovery_service.py` exists and the mover lane produced real candidates on 2026-09-03 (TLYS, SMMT, AVAV, NTSK, DLTH, BIAF, SNOW, CHPT) | STALE |
| 14 | Outcomes remain pending instead of finalized | **CONFIRMED** — every outcome table is empty; `target_status: PENDING` on all 25 ledger days | VALID |
| 15 | Lessons exist without being approved and applied | **CONFIRMED, differently** — there are no lessons at all. `strategy_learning_labels`, `alpha_learning_runs`, `learning_backfeed_events` are all empty. The approval gate cannot be validated against real data | VALID |

**Dependency chain:** items 8, 14 and 15 are all downstream of the same root cause (WP-1). Item 12
is its upstream condition. Fixing WP-1 is the single change that reopens the chain.

## Not fully mapped (declared)

A dedicated dead-code/duplicate-implementation sweep and a full capability matrix across all 362
modules and 101 CLI subcommands were planned but not completed — the subagent passes were lost to
session limits. What is recorded above is what I verified directly.

The known-unmapped surface is large: 101 CLI subcommands, 97 scripts, 195 documents. The
duplicate-stack analysis above is grounded in call-graph checks I ran myself
(`evaluate_position_management`, `position_management`, `trade_watcher_service`), not in a complete
inventory.
