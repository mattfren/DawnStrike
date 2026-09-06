# 04 — Innovation and market benchmark

**Coverage warning — read first.** This phase is `PARTIAL`. Five competitor categories were planned;
**one completed** (real-time scanners / premarket gapper screeners, 8 products, sources fetched
2026-09-04). Backtesting platforms, AI-assisted trading research, trading journals / outcome
tracking, and monitoring-alerting were **not researched** — those agents were lost to session
limits. The scores below are therefore anchored on one category plus direct code evidence, and the
`competitive_evidence` component is scored down accordingly. Do not treat this as a complete
market benchmark.

## Method

Current sources only, fetched 2026-09-04; vendor pricing pages treated as authoritative over
third-party comparison pages. Where a page could not be fetched, that is recorded rather than
filled from memory.

## Category 1 — Real-time scanners and premarket gapper screeners (completed)

| Product | Verified price (2026-09-04) | Relevant strength |
|---|---|---|
| Trade Ideas (Standard/Premium) | $127 / $254 per mo | 500+ alert conditions; **OddsMaker** event-backtests scanner alerts reporting win rate, profit factor, avg winner/loser, max drawdown, and identifies which filters add vs. detract value |
| StocksToTrade | $179.95/mo | Broad retail scanning suite |
| Scanz | $89 / $199 / $999 per mo | 400+ real-time filters, 4am–8pm, unlimited saved scans/alerts |
| TrendSpider | $82 / $137 / $183 per mo | Automated technical analysis, backtesting |
| BlackBoxStocks | $59 / $89 per mo | Real-time options/equities flow alerts |
| Finviz Elite | $39.50/mo | Real-time premarket screening 4:00–9:30am ET, API + export |
| Benzinga Pro | ~$37–197/mo *(pricing pages returned HTTP 403; unverified)* | Real-time newsroom + audio squawk — genuine catalyst attribution |
| MOMO Pro (Mometic) | ~$27–67/mo *(pricing page 404; partially verified)* | Gap scanner over 10,000 symbols at sub-150ms with an integrated LLM explainer |

### Category verdict

**On scanning, Dawnstrike has no case.** It is beaten on every measurable dimension by products
costing $27–199/month: breadth (10,000 symbols vs. a CSV inbox and public HTML tables), latency,
uptime, cloud/mobile delivery, charting, and broker integration. Dawnstrike runs on one Windows
machine and its intraday monitor failed 100% of attempts for five consecutive trading days.

**On explainability, the gap is narrower but Dawnstrike still does not lead.** Every competitor
answers "why did this rank" somehow — tautologically (Finviz, Scanz: it matched your filter),
journalistically (Benzinga: a real newsroom catalyst), or statistically (Trade Ideas OddsMaker:
filter-contribution analysis). Dawnstrike's per-pick reasoning is richer than a filter match but
weaker than a newsroom.

**On evidence discipline, Dawnstrike is genuinely differentiated.** One concrete, verifiable
contrast: Trade Ideas' own Holly AI records page is a curated showcase of dated *winning* trades
with no win rate, no trade count, no aggregate P/L and no drawdown — and its headline counters
rendered as `0 Winning Holly AI Signals` when fetched on 2026-09-04. Dawnstrike, by contrast,
refuses to publish its dashboard at all when upstream is degraded (`deployment_url: null`), records
what it *failed* to observe as explicit `MISSED_INTERVAL` receipts, and tells the user
*"Evidence is insufficient until at least 20 real market days are audited."*

No product in the researched set records its own non-observation. That is the real wedge.

## Differentiator classification

| Claim | Classification | Reasoning |
|---|---|---|
| Auditable refusal-to-overclaim (missing ≠ zero, `MISSED_INTERVAL` receipts, refuse-to-publish-degraded) | **MEANINGFULLY DIFFERENTIATED** | No researched competitor records non-observation or withholds publication on degraded upstream. Verified in live artifacts, not marketing |
| Human-gated learning loop (lessons require approval before affecting future scans) | **DIFFERENT BUT UNPROVEN** | Gates exist in code; zero lessons have ever existed, so the behaviour is uncertified |
| Explicit AVOID / do-not-touch list as a first-class output | **MEANINGFULLY DIFFERENTIATED** | Competitors filter *in*; none researched publishes a reasoned exclusion list. Verified working (OFFER/SUBP/THIN/HALT) |
| Content-addressed evidence, run contracts, frozen model artifacts | **DIFFERENT BUT UNPROVEN** | Institutional-grade discipline, unusual at this tier; delivers no user value until the loop closes |
| Walk-forward ML with promotion gates (v6) | **DIFFERENT BUT UNPROVEN** | Gates are strict (≥60 sessions, ≥100 trades, ≥98% coverage, frozen holdout). **All 15+ v6 tables are empty — never trained on real data** |
| Premarket gapper scanning | **BEHIND MARKET** | Finviz Elite at $39.50/mo does it better, faster, everywhere |
| Composite proprietary score | **COMMODITY** | Every scanner has one. A proprietary score is not innovation |
| Catalyst detection | **BEHIND MARKET** | Benzinga's newsroom is categorically better than headline keyword rules |
| Telegram alerting | **COMMODITY** | Ubiquitous |
| Streamlit dashboard | **COMMODITY** | Competitors ship better UIs |
| 101 CLI subcommands | **INNOVATION THEATER** | Surface area is not capability; it is a discoverability tax |
| Backtesting | **UNKNOWN** | Category not researched |
| Trading journal / outcome tracking | **UNKNOWN** | Category not researched — and this is where the learning-loop wedge would be contested |

## Innovation Potential — **59 / 100**

| Component | Max | Score | Basis |
|---|---:|---:|---|
| User-outcome novelty | 25 | 15 | "A research tool that proves what it did and did not see" is a real, unmet outcome |
| Technical / workflow novelty | 20 | 14 | `MISSED_INTERVAL` receipts, refuse-to-publish-degraded, content-addressed evidence, approval-gated lessons |
| Potential defensibility | 20 | 10 | Accumulated outcome data could compound — but it is local, single-user, and currently empty |
| Strategic differentiation | 20 | 12 | Credible position *adjacent* to scanners rather than against them |
| Compounding value | 15 | 8 | The loop would compound if it ever closed |

## Proven Innovation — **16 / 100**

| Component | Max | Score | Basis |
|---|---:|---:|---|
| Working implementation | 20 | 8 | Scan, dashboard, persistence, publication gating verified; the core loop is broken |
| Demonstrated user value | 20 | 2 | 25 forward days, 0 trades, 0 closed loops |
| Quantitative evidence | 20 | 0 | No outcome data exists at all |
| Competitive evidence | 15 | 3 | 1 of 5 categories researched |
| Reliability / repeatability | 15 | 2 | 100% stage failure for 5 days; documented install is not reproducible |
| Defensible adoption advantage | 10 | 1 | Single-user, local, Windows-only, no lock-in |

The 59 → 16 gap is the honest summary: **a good thesis, real engineering integrity, and almost
nothing proven.**

## Answers to the required questions

**Strongest credible wedge.** Auditable refusal-to-overclaim. Not the AI, not the score — the fact
that this system records its own blind spots and declines to publish when it cannot see. Nothing in
the researched field does that.

**Three most defensible capabilities.** (1) Non-observation accounting (`MISSED_INTERVAL`,
missing ≠ zero); (2) fail-closed publication; (3) the reasoned AVOID list.

**Three most promising but unproven.** (1) Approval-gated learning loop; (2) walk-forward v6 with
frozen holdouts; (3) content-addressed evidence lineage enabling exact replay.

**What competitors already do better.** Scanning breadth/latency/uptime (Finviz, Scanz, MOMO);
catalyst attribution (Benzinga); alert-outcome backtesting (Trade Ideas OddsMaker — which already
does a rigorous version of what Dawnstrike aspires to); UI and cross-platform delivery (all).

**Commodity kill list — stop investing here.** Composite proprietary score; Telegram formatting;
Streamlit chrome; breadth-chasing on scanning; further CLI subcommands.

**Innovation theater.** The 101-subcommand CLI surface. Four parallel strategy stacks (v4, v5, v6,
v2) where the best position engine has no production caller. Volume of governance documentation
(195 files) outpacing demonstrated outcomes.

**Minimum proof to convert the wedge.** Publish a rolling public record that includes every
*non*-observation and every no-trade day alongside outcomes — the artifact no competitor produces.
That requires ≥20 and ideally ≥60 completed forward sessions, which requires WP-1 deployed.

**Fastest route to being hard to replace.** Stop competing on scanning; buy that layer (Finviz Elite
at $39.50/mo has an API and export). Redirect entirely to the evidence layer: be the system of
record for *what a trader decided and what actually happened*, including the gaps. Competitors will
not copy non-observation accounting, because it makes their marketing worse.
