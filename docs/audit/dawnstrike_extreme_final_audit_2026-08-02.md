# Dawnstrike extreme hardening audit — 2026-08-02

## Verdict

The candidate is now materially harder to fool locally. It is still **not a
proven return engine, production release, or promotion candidate**. The code
fails closed where durable real-world evidence is absent; that is the correct
state.

## Verified local hardening

| Surface | Evidence-backed result |
|---|---|
| Alert/watchlist ticker text | `is_alertable_notification_candidate` recomputes `dawnstrike-alert-gate-v2.0.0`; generic notifier and web collection paths use it. Stale caller flags, manual confirmation, no-trade reason, weak/missing evidence, and mismatched gate metadata cannot pass. |
| Daily versus weekly learning | Daily monitor may score only with a frozen artifact and write drift/calibration/coverage evidence. Weekly training owns frozen-cutoff model competition. Neither path can auto-promote. |
| Model competition | Every permitted family is compared on common leak-free OOF folds. Conservative after-cost benchmark-excess lower bound is primary; materiality, tail, cost-stress, calibration, interval, capacity, concentration, and stability failures reject a challenger. A research winner cannot alter V5 or serving policy. |
| Failure learning | Cross-version attribution now covers V4/V5/V6, PaperOps, and sampled rejects with eligibility, missing/excluded counts, coverage, activation, after-cost/benchmark-excess, MFE/MAE, path, uncertainty, and lineage. Inadequate facts remain `unattributed_insufficient_evidence`. |
| Performance truth | `dawnstrike.account-comparison.v1` persists V5/V6/cash/SPY/IWM only from authoritative account ledgers and same-session sourced benchmarks. A V6 signal/outcome is never converted into a synthetic equity curve. Missing V6 account truth returns `WAITING_FOR_AUTHORITATIVE_V6_ACCOUNT_LEDGER` and null metrics. |
| Product | The bounded Research UI includes the account-comparison status/card without inventing metrics. Existing Calendar behavior remains in place. |
| Code safety | Migration 19 adds an immutable, input-hashed comparison receipt. Full regression includes account, alert, attribution, model-competition, canonical-performance, V6-shadow, and rendered-dashboard coverage. |

## Local proof completed on this candidate

| Gate | Result |
|---|---|
| `py -m pytest -q` | 438 passed |
| `py -m ruff check .` | passed |
| `py -m mypy intraday_scanner` | passed; 210 source files |
| `py -m compileall -q intraday_scanner scripts app.py` | passed |
| `node --check web/assets/dawnstrike.js` | passed |
| `git diff --check` | passed |
| `py -m pip check` | passed |
| `py -m bandit -r intraday_scanner scripts -ll` | no medium/high issues |
| `py -m pip_audit -r requirements.lock` | no known vulnerabilities |
| `detect_secrets` against tracked files and baseline | passed |
| PowerShell parser over every `scripts/*.ps1` | passed |

Bandit reported 51 low-severity findings and 14 existing `#nosec` annotations;
none were medium/high. They remain visible for periodic review and are not
evidence of a live release.

## Live-state facts observed before this candidate is released

- Production health was HTTP 200 but readiness was HTTP 503. Health is not
  release evidence.
- Public deployment source SHA was
  `692e785cf8304a8045e88ab221dc644d4eb2e9e7`, not this candidate.
- Durable state `C:\r\dawnstrike-state\shadow_real.sqlite` passed
  `quick_check` but had schema 13, 254 `alpha_signals`, 10 `signal_outcomes`,
  no V6 decision/outcome tables, and no benchmark observations.
- The current candidate advances the application schema to 19. No durable DB,
  runtime, scheduler, source configuration, Vercel deployment, or Telegram
  path was changed by this hardening pass.
- The existing completed-loss sample is five gross signal-level research
  closes, not an account-return cohort: SLND -1.3438%, VIVK -20.2778%, VRRM
  -10.8696%, NUWE -24.8305%, XRX -0.2173%; mean -11.5078%. It is far too small
  and contaminated for retrospective strategy tuning.

## Remaining blockers — do not bypass

1. **Approved real data.** An accountable owner must supply provider terms,
   identity, key/entitlement, and an actual dated point-in-time universe. The
   template/config fixture is not production data.
2. **Safe state transition.** A schema 13-to-19 copy-on-write rehearsal,
   backup hash, restart/idempotence proof, and rollback rehearsal are required
   before the only durable DB is migrated.
3. **Authoritative V6 account ledger.** V6 comparison and any promotion metric
   remain unavailable until real paper-account, cash-flow, cost, SPY, and IWM
   rows exist on aligned sessions.
4. **Scheduler evidence.** Daily monitor and weekly trainer require separately
   registered, non-overlapping tasks and an unattended receipt chain. Windows
   credentials must be entered only in the local credential dialog.
5. **Release proof.** Build a preview from the exact merged SHA, then verify
   source/build/data binding, readiness 200, headers, Calendar, all five
   viewports, console/accessibility behavior, logs, and rollback. Promote only
   that same deployment.
6. **Forward evidence.** Do not claim alpha or improve thresholds around the
   five losses. V6 stays shadow-only until at least 60 real forward sessions,
   100 valid sourced closed paper labels, 98% eligible coverage, complete
   cost/benchmark truth, positive conservative after-cost benchmark-excess
   evidence, stress/holdout gates, and explicit human approval.

## Bottom line

The software can now say “no clean edge,” “missing,” or “waiting” honestly.
That is a prerequisite for reliable research. What remains is a controlled
data, operations, release, and forward-sample program—not a hidden coding
shortcut to higher return rates.
