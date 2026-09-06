# 05 — Remediation program

Baseline frozen before any product-code change: `evidence/baseline_manifest.json`,
`evidence/finding_register_auditor.json`, `evidence/full_pytest_origin_main.txt`,
`evidence/live_db_forensics.txt`, `evidence/test_suite_analysis.md`,
`evidence/clean_build_reproducibility.md`, `evidence/fill_methodology.md`.

Remediation branch: `codex/dawnstrike-cert-remediation-20260904` from `14ca714f`.
**Nothing committed, nothing pushed.** The primary checkout, the production runtime and the live
database were never modified.

## Dependency order

```
WP-1  monitor artifact contract  ── unblocks ──▶ WP-6 outcomes ──▶ WP-7 learning ──▶ edge evidence
WP-2  dashboard render guard        (independent)
WP-3  prior-day target geometry     (independent, but must land before WP-6 produces entries)
WP-4  dependency baseline        ── gates ──▶ green suite ──▶ certification
WP-5  interpreter-hash rotation  ── gates ──▶ green suite + runtime upgrade
WP-8  production fill fidelity      (must land before any edge claim from WP-6 data)
```

WP-1 is first because every downstream stage is starved by it. Fixing metrics or fill fidelity
before WP-1 would be optimising a pipeline that never runs.

---

## WP-1 — Monitor must accept the artifact the morning stage publishes `P0` — **DONE**

**Defect.** `run_alphaops_monitor.ps1` called `Test-DawnstrikeAlphaCycleArtifact` with neither
`-AllowCoreShortfall` nor `-RequireCoreCoverage`. With `core_universe_status = DATA_UNAVAILABLE`
the validator's `coreCoverageRequired` expression evaluates true and throws, so the monitor set
`alpha_cycle_artifact_invalid` and exited 2 on **every** cycle.

**Evidence.** 344 `intraday_monitor` FAILED rows; 79 `MISSED_INTERVAL` per day ×4 days + 35;
zero `alpha_monitor-*` / `trade_watch-*` logs ever written. Reproduced against the real artifact:

```
2026-08-31 : strict=REJECT | withAllowCoreShortfall=ACCEPT (research_candidates=0)
2026-09-01 : strict=REJECT | withAllowCoreShortfall=ACCEPT (research_candidates=0)
2026-09-02 : strict=REJECT | withAllowCoreShortfall=ACCEPT (research_candidates=0)
2026-09-03 : strict=REJECT | withAllowCoreShortfall=ACCEPT (research_candidates=0)
```

**Root cause.** Two callers of one shared validator disagreed. `run_alphaops_morning.ps1:229-236`
passes `-AllowCoreShortfall` with an explicit comment that a core outage is *lane-local* and the
mover lane stays publishable. The monitor omitted it — while consuming only
`research_candidate_count` and `research_symbols`, never core membership.

**Change.** `scripts/run_alphaops_monitor.ps1` — added `-AllowCoreShortfall` plus a comment
explaining why the monitor must share the morning stage's contract.

**Out of scope.** Loosening the validator itself. The validator is correct; only the caller was wrong.

**Tests added** (`tests/test_alpha_cycle_artifact_ps1.py`):
`test_monitor_stage_accepts_lane_local_core_shortfall`,
`test_morning_stage_retains_a_lane_local_core_shortfall_path`,
`test_core_data_unavailable_artifact_survives_the_monitor_switch_set`.

**Proof the tests catch the defect.** Reverted the fix; the new tests failed with the exact
production error `AlphaOps cycle artifact core universe is not READY; full core coverage is
unavailable.` Restored; they pass.

**Regression.** `test_alpha_cycle_artifact_ps1 + asof_identity + safe_reserve + scheduler_fail_closed
+ daily_orchestrator` → 39 passed, exit 0.

**Rollback.** `git checkout scripts/run_alphaops_monitor.ps1` and delete the three tests.

**Residual external gate.** Production proof needs one live session: an `alpha_monitor-<date>.stdout.log`
and an *observed* (not `MISSED`) interval. See operator packet OP-1.

---

## WP-2 — Restore the dashboard's only render guard `P1` — **DONE**

**Defect.** `AppTest.from_file("app.py")` resolves relative to the calling file under
streamlit 1.63.0, so it looked for `tests/app.py` and raised `FileNotFoundError`. The single
end-to-end dashboard test had stopped running.

**Important nuance.** The **application is not broken.** Driving `AppTest` with an absolute path
renders all five tabs with no exception, does not create the database, and emits the correct
warnings. Only the test was stale.

**Change.** `tests/test_streamlit_app.py` — resolve `app.py` absolutely from the repo root;
timeout 30→90s.

**Verification.** `py -m pytest tests/test_streamlit_app.py` → 1 passed, exit 0.

**Rollback.** `git checkout tests/test_streamlit_app.py`.

---

## WP-3 — A prior-day target must never sit below the entry `P0` — **DONE**

**Defect.** `_attach_authenticated_alpaca_structure` assigned
`output["target_1"] = prior_high` unconditionally under basis `prior_day_resistance`, with no check
that the prior-day high is above the entry. On a gap up — the product's entire premise — yesterday's
high is by definition below today's premarket high, so the published first target lands below both
entry and stop.

**Evidence — live production rows in `historical_signals`:**

| Ticker | Date | Entry | `target_1` | Δ |
|---|---|---:|---:|---:|
| LIDR | 2026-09-01 | 1.8090 | 1.17 | −35% |
| GPRO | 2026-09-01 | 1.5879 | 0.88 | −45% |
| PPBT | 2026-09-02 | 3.0150 | 1.68 | −44% |

**Realized user impact was nil, by luck, not design.** All three carry `was_alerted: 0` and
`signal_label: NO CLEAN EDGE`, blocked by the *unrelated* gate `no_trade_reason: "SEC risk not
checked"`. Had SEC risk verified, these were alertable with a target 45% below entry. Latent
critical, not realized critical.

**Change.** `intraday_scanner/services/alpha_cycle_service.py` — added `or prior_high <= high` to
the existing geometry guard, so the structure is refused exactly as an absent observation is and the
constructor emits `NO_VALID_PLAN` rather than an inverted plan.

**Out of scope.** Inventing a replacement target (ATR- or risk-derived). That is a strategy change
requiring its own versioned research packet, not an audit fix.

**Test added.** `tests/test_alphaops_prior_day_target_geometry.py` — control case (prior high above
entry → bound normally), the exact LIDR gap-up geometry, and the equal-to-entry boundary.

**Proof the tests catch the defect.** Reverted the fix → both gap-up tests failed; the control test
passed in both states. Restored → 3 passed.

**Regression.** `market_structure_plan + intraday_adapter + v5_policy + run_contracts +
structural_tier_producer + new file` → 145 passed, exit 0.

**Rollback.** `git checkout intraday_scanner/services/alpha_cycle_service.py` and delete the test file.

---

## WP-4 — Dependency baseline `P1` — `BLOCKED_EXTERNAL` (your decision)

**Defect.** The documented install (`pip install -e ".[dev]"`) resolves `anyio 4.15.0` while
`requirements.lock:15` pins `anyio==4.14.2`. The dependency-contract tests assert exact equality, so
**35 of the 61 failures come from this one package.** Proven systemic in a fresh venv, not local to
this machine.

**Why I did not fix it.** There are two valid resolutions and both are baseline decisions that are
yours, not an auditor's:

- **(a) Hold the baseline** — install from the lock: `pip install --require-hashes -r requirements.lock`
  then `pip install -e . --no-deps`, and update README / WINDOWS_SETUP / OPERATIONS.
- **(b) Advance the baseline** — regenerate the lock to admit `anyio 4.15.0`
  (`py -m piptools compile --generate-hashes --output-file requirements.lock requirements.in`) and
  re-run the suite.

Do **not** weaken the contract test. The test is correct; the install procedure is what diverges.

**Acceptance.** A clean venv built by the documented procedure passes
`tests/test_dawnstrike_python_bootstrap.py` with zero version-mismatch assertions.

---

## WP-5 — Approved-interpreter identity rotation `P1` — `BLOCKED_EXTERNAL` (your decision)

**Defect.** `scripts/runtime_activation_lock.ps1:4` pins
`ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1`. The host interpreter is
`85B71D8C6EC1905935F74BE0C9869AAE198D00E98F39DF699EC66F9C5A84CECD` (Python 3.13.15, built
2026-08-05). ~16 tests fail with `Approved lock-contract interpreter hash changed.`

**This control is working as designed.** It refuses an unapproved interpreter. The defect is the
absence of a *rotation procedure*, not the check. It is also the most probable reason the production
runtime is still pinned at `b7220890` rather than tracking `origin/main`.

**Why I did not fix it.** Rotating an approved interpreter hash is a trust decision. An auditor
silently widening a security boundary would be exactly the wrong act.

**Required.** A documented rotation runbook: verify the new interpreter's Authenticode signer and
thumbprint, record the new SHA-256 with provenance, update the pin, and re-run the activation suite.

---

## WP-6 — Close one full outcome loop `P1` — pending WP-1 deployment + calendar time

Blocked purely by time, not code. Needs live sessions after WP-1 lands.

**Acceptance.** ≥1 completed outcome with a sourced fill, and non-zero
`signal_outcomes` / `paper_positions`.

---

## WP-7 — Learning loop end-to-end `P2` — pending WP-6

Cannot be validated against real data while every learning table is empty. The *gates* were
reviewed; the *behaviour* cannot be certified without data.

---

## WP-8 — Production fill fidelity `P1` — open, not attempted

**Defect.** Production `trade_watcher_service.py` decides on a single polled five-minute spot price
and cannot see intrabar excursion; the correct path-accurate, lookahead-guarded, same-bar-conservative
engine (`v2/paper_ops/position_management.py`) has **no production caller**. Separately, offline
paper audit defaults to `entry_mode="open"` — unconditional entry — while the product's stated rule is
*"Watch only if price confirms above X."*

**Why not attempted here.** This changes what the product *measures*, which is a strategy-definition
decision and an escalation trigger under the operating protocol — not an audit-scope fix.

**Required.** Either route production exits through `evaluate_position_management` against retained
one-minute bars, or explicitly label production paper results as poll-resolution estimates with an
accounted intrabar blind spot. Default paper audit to `breakout`, and make
`_breakout_entry_index` refuse a non-positive trigger instead of silently returning index 0.

---

## Raised but NOT independently verified — do not action yet

A subagent sweep raised 12 statistical findings (deflated-Sharpe unit mismatch saturating at 1.0;
Sortino computed from the standard deviation among losing returns; hardcoded `sqrt(252)`; no minimum
sample gate; profit-factor sentinel of 1,000,000) and 10 scan findings. **Their adversarial
verification pass did not complete.** I independently confirmed exactly two of them — the inverted
target (WP-3) and the `config_hash` path contamination — and independently disproved the implied
severity of a third (`win_probability_pct = 100.0` on zero evidence is real, but it is never
rendered on any surface: it is absent from `EXPECTANCY_COLUMNS` and from both per-ticker render
sites).

The remainder are **hypotheses with cited line numbers, not findings.** They sit on the promotion
path rather than the live path, and every V6 evaluation table is currently empty, so nothing is
being mis-reported to a user today. They should be verified before any is actioned.

---

## Operator packet OP-1 — production proof of WP-1

**Resolves:** the WP-1 live gate and, transitively, the Verdict A blocker.

1. Update `C:\r\dawnstrike-runtime` to a commit containing the WP-1 fix.
   **Re-register the scheduled tasks in the same step** — `origin/main`'s monitor adds three
   *mandatory* parameters (`ExpectedSha`, `LaunchManifestPath`, `LaunchManifestSha256`) that the
   currently registered task does not supply, so an upgrade without re-registration breaks the task.
2. On the next trading day, after 08:35 CT, confirm:
   - `C:\r\dawnstrike-state\logs\alpha_monitor-<date>.stdout.log` **exists** (it never has);
   - `monitor_interval_gaps` gains rows with a status other than `MISSED_INTERVAL`;
   - `daily_run_stages` shows `intraday_monitor` with a status other than `FAILED`.
3. Capture those three artifacts as evidence.

**Failure interpretation.** If `alpha_cycle_artifact_invalid` persists, the artifact is failing a
*different* validator assertion — re-run the reproduction in `evidence/` to identify which. If a new
error code appears, the monitor is now progressing past the gate and the next stage is the real
blocker, which is the intended outcome.

**Cleanup.** None. No fixture or synthetic data is introduced at any point.
