# Command log

Every command that produced audit evidence or a remediation change. Dates are 2026-09-04.
Read-only inspection commands with no bearing on a finding are omitted.

## Baseline capture (read-only)

```bash
git rev-parse HEAD                      # ba39a535… (primary checkout)
git rev-parse origin/main               # 14ca714f…
git rev-list --count main..origin/main  # 440
git rev-list --count origin/main..main  # 0
git diff --stat main origin/main        # 1486 files, 547081 insertions, 5960 deletions
git worktree list                       # 33 worktrees
git stash list                          # 4 stashes (untouched)
```

```powershell
# Production runtime identity
Push-Location C:\r\dawnstrike-runtime; git rev-parse HEAD   # b7220890…
git status --porcelain                                      # clean
git merge-base --is-ancestor b7220890 origin/main           # exit 0
git merge-base --is-ancestor ba39a535 b7220890              # exit 0

# Scheduled tasks
Get-ScheduledTask | Where-Object { $_.TaskName -like "*Dawnstrike*" }
Get-ScheduledTaskInfo -TaskName "Dawnstrike AlphaOps Monitor 5m"   # LastResult 267009
Get-ScheduledTaskInfo -TaskName "Dawnstrike AlphaOps Morning"      # LastResult 2
Get-ScheduledTaskInfo -TaskName "Dawnstrike Delayed SIP Capture"   # LastResult 2147942402
```

Audit worktree created (nothing else modified):

```bash
git worktree add --detach C:/r/dawnstrike-cert-20260904 14ca714f
git checkout -b codex/dawnstrike-cert-remediation-20260904
```

## Test suite — baseline at origin/main

```powershell
py -m pytest --collect-only -q -p no:cacheprovider     # 5,150 across 256 files
py -m pytest -q -p no:cacheprovider --durations=40 -rsxX
# -> 5,083 passed / 61 failed / 6 skipped / exit 1
# -> evidence/full_pytest_origin_main.txt
```

Failure-family isolation:

```bash
grep -E "^E   " full_pytest_origin_main.txt | sort | uniq -c | sort -rn
# 35x AssertionError: assert '4.15.0' == '4.14.2'
grep -nE "==4\.14\.2" requirements.lock                 # requirements.lock:15  anyio==4.14.2
py -m pip list | grep -i "4\.15\.0"                     # anyio 4.15.0
grep -rn "Approved lock-contract interpreter hash changed" scripts/
# scripts/runtime_activation_lock.ps1:38,387
sed -n '4p' scripts/runtime_activation_lock.ps1         # pin ef8f5102…
```

```powershell
(Get-FileHash (py -c "import sys;print(sys.executable)") -Algorithm SHA256).Hash
# 85B71D8C…  (Python 3.13.15, built 2026-08-05)  != pinned ef8f5102…
```

## Phase Two — clean-environment reproducibility

```powershell
py -3.13 -m venv C:\r\dawnstrike-cleanenv-20260904
& C:\r\dawnstrike-cleanenv-20260904\Scripts\python.exe -m pip install -e ".[dev]"
# INSTALL_EXIT=0 ; resolved anyio 4.15.0 (lock pins 4.14.2)
& C:\r\dawnstrike-cleanenv-20260904\Scripts\python.exe -m pytest `
    tests/test_dawnstrike_python_bootstrap.py -q -p no:cacheprovider -k "requirement or dependency or lock"
# -> FAILED with the identical 4.15.0 vs 4.14.2 assertion. Systemic, not machine-local.
```

## Live pipeline forensics (read-only)

```bash
# Stage outcomes, live DB, mode=ro&immutable=1 + PRAGMA query_only=ON
select stage_name,status,count(*) from daily_run_stages group by stage_name,status;
#   intraday_monitor FAILED 344 | morning_collection FAILED 5 | ranking_delivery FAILED 5
#   eod_outcome_capture FAILED 4 | paper_reconciliation FAILED 4 | alpha_learning FAILED 4
#   publication COMPLETE 4

select market_date,status,count(*) from monitor_interval_gaps group by market_date,status;
#   79 MISSED_INTERVAL/day for 08-31..09-03, 35 on 09-04. Zero observed.

ls C:/r/dawnstrike-state/logs | grep -c "alpha_monitor-"   # 0 (only *_calendar / *_heartbeat)
ls C:/r/dawnstrike-state/logs | grep -c "trade_watch-"     # 0
```

Root cause reproduced against the real production artifacts:

```powershell
. 'C:\r\dawnstrike-runtime\scripts\alpha_cycle_artifact.ps1'
Test-DawnstrikeAlphaCycleArtifact `
  -ArtifactPath 'C:\r\dawnstrike-state\outputs\alpha_cycle\2026-09-03\alpha_cycle.json' `
  -MarketDate '2026-09-03' -ReleaseSha 'b7220890…'
# THROW: AlphaOps cycle artifact core universe is not READY; full core coverage is unavailable.

# Same call with -AllowCoreShortfall, all four available days:
# 2026-08-31 strict=REJECT | withAllowCoreShortfall=ACCEPT (research_candidates=0)
# 2026-09-01 strict=REJECT | withAllowCoreShortfall=ACCEPT
# 2026-09-02 strict=REJECT | withAllowCoreShortfall=ACCEPT
# 2026-09-03 strict=REJECT | withAllowCoreShortfall=ACCEPT
```

Forward paper record and inverted plans:

```sql
select market_date,status,trade_count,target_status from paper_account_daily_ledger order by market_date;
-- 25 trading days: 21 MISSING, 4 NO_TRADE, trade_count 0 throughout, target_status PENDING

select ticker,market_date,entry_watch_level,target_1,was_alerted,signal_label,no_trade_reason
from historical_signals where target_1 <= entry_watch_level;
-- LIDR 1.8090/1.17, GPRO 1.5879/0.88, PPBT 3.0150/1.68 — all was_alerted=0, 'SEC risk not checked'
```

## Functional verification

```bash
# Documented offline scan, twice, into separate then identical paths
py -m intraday_scanner.cli scan --snapshot sample_data/premarket_snapshot_sample.csv \
   --out-dir <scratch>/scanA --db-path <scratch>/a.sqlite --persist --print
# ranked=4 avoid=4 top=NOVA, exit 0
# Determinism: every ranked_candidates.csv column identical except config_hash
#   differing paths  -> e464382b3509 / b35f68c75312 / 2e59adc5acdf
#   identical paths  -> 1885ac5b81d9 twice
```

```python
# Dashboard renders correctly (the test, not the app, was broken)
AppTest.from_file(str(Path('app.py').resolve()), default_timeout=90).run()
# exception=ElementList()  error=ElementList()  db_created=False
# tabs=['Today','Picks','Calendar','Performance','System']
# warning: "Evidence is insufficient until at least 20 real market days are audited."
```

```python
# Expectancy on zero history
estimate_expectancy(candidate_rows, [])
# sample_size=0  model_basis='score prior only'  expected_return_pct=4.99
# win_probability_pct=100.0  confidence_pct=18.0  band -14.88..24.86
```

## Remediation

```
WP-1  scripts/run_alphaops_monitor.ps1                  +8 -1   (-AllowCoreShortfall)
WP-2  tests/test_streamlit_app.py                       +9 -1   (absolute app path)
WP-3  intraday_scanner/services/alpha_cycle_service.py  +8      (prior_high guard)
      tests/test_alpha_cycle_artifact_ps1.py            +75     (3 monitor-contract tests)
      tests/test_alphaops_prior_day_target_geometry.py  +159    (new file, 3 tests)
```

Each fix proven to be caught by its own test:

```powershell
# WP-1
git stash push -- scripts/run_alphaops_monitor.ps1
py -m pytest tests/test_alpha_cycle_artifact_ps1.py -k "core_shortfall or lane_local or monitor_switch_set"
# -> FAILED with 'AlphaOps cycle artifact core universe is not READY' (the production error)
git stash pop; # -> 4 passed

# WP-3
git stash push -- intraday_scanner/services/alpha_cycle_service.py
py -m pytest tests/test_alphaops_prior_day_target_geometry.py
# -> 2 FAILED (both gap-up cases); the control case passed in both states
git stash pop; # -> 3 passed
```

Regression and quality gates:

```powershell
py -m pytest tests/test_alpha_cycle_artifact_ps1.py tests/test_alpha_cycle_asof_identity.py `
  tests/test_alpha_cycle_safe_reserve.py tests/test_scheduler_fail_closed.py `
  tests/test_daily_orchestrator.py -q -p no:cacheprovider          # 39 passed, exit 0

py -m pytest tests/test_alphaops_market_structure_plan.py tests/test_alphaops_intraday_adapter.py `
  tests/test_alpha_v5_policy.py tests/test_alpha_run_contracts.py `
  tests/test_luna_structural_tier_producer.py `
  tests/test_alphaops_prior_day_target_geometry.py -q -p no:cacheprovider   # 145 passed, exit 0

py -m pytest tests/test_streamlit_app.py -q -p no:cacheprovider     # 1 passed, exit 0
py -m ruff check <changed files>                                    # All checks passed
py -m mypy intraday_scanner/services/alpha_cycle_service.py         # Success: no issues
git diff --check                                                    # clean
```

## Final verification

```powershell
# Frozen final-state hashes recorded, then a full suite run from that exact state
py -m pytest -q -p no:cacheprovider -rsxX
# -> evidence/final_pytest_remediated.txt
```

## Preservation confirmations

- Primary checkout `C:\Users\MattFields\Dawnstrike`: **not modified**.
- Production runtime `C:\r\dawnstrike-runtime`: **not modified**.
- Live database `C:\r\dawnstrike-state\shadow_real.sqlite`: **never opened for write**. All reads
  used `file:…?mode=ro&immutable=1` with `PRAGMA query_only=ON`.
- Scheduled tasks: **not modified, not disabled, not invoked**.
- Secrets: only key *names* were read (`sed -E 's/=.*/=<redacted>/'`); no value was read or logged.
- Git: **0 commits, 0 pushes**, no worktree removed, no stash dropped, no branch deleted.
- Developer Python environment: **not modified** — the reproducibility test used a throwaway venv.
