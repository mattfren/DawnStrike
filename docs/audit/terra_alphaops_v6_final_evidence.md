# Terra AlphaOps V6 implementation evidence

Status: **implementation complete on the isolated V6 release commit; production release deliberately gated**
Date: 2026-08-01

## Delivered controls

- Clean V6 branch/worktree created from deployed SHA `692e785cf8304a8045e88ab221dc644d4eb2e9e7`.
- Durable-state SQLite snapshot captured before migration; SHA-256 recorded in `terra_alphaops_v6_initial_audit.md`.
- Every scheduled Python stage now emits a native-process receipt with exit code, duration, stdout/stderr paths, and hashes.
- Missing real source configuration is a recorded failed stage, not a silent fallback to example configuration.
- Scheduler diagnosis now exits nonzero unless all tasks are locally verified. Task definitions require a password-logon identity with network and encrypted-secret access, start-when-available, and battery-safe execution. S4U is explicitly rejected because Windows denies S4U access to the network and encrypted files.
- Public build outputs remove absolute runtime/state roots and artifact verification rejects host-path disclosure.
- Calendar remains a primary public view and V6 evidence is displayed as a shadow challenger with explicit forward thresholds.
- V6 ledger migrations are additive. Decision input lineage, versioned universe membership, cost model, safety vetoes, outcomes, model runs, datasets, predictions, drift reports, walk-forward evaluation, and proposed experiments are separate immutable records.
- V6 uses an after-cost net excess label, strict expanding-window validation, conditional activation/return analysis, lower-confidence-bound utility, adverse-tail penalty, deterministic rejected-candidate sampling, and source-backed SPY/IWM benchmark coverage.
- A production-contract scan now fails closed before it creates V6 shadow decisions if any candidate is absent from the dated versioned universe. The V6 decision ledger does not treat an unknown universe member as eligible.
- Missing data remains null/ineligible. The legacy edge calibrator no longer manufactures zero-return samples.
- No broker execution, automated order placement, LLM scoring, or automatic strategy promotion was added.

## Verification completed

- Focused V6/universe/benchmark/outcome suite: 24 passed.
- Cross-service regression suite: 76 passed.
- `ruff check .`: passed.
- `mypy intraday_scanner`: passed (204 source files).
- `compileall intraday_scanner app.py`: passed.
- PowerShell parser validation: passed for all modified runners and task registration scripts.
- Full `pytest`: **602 passed in 313.89s**.
- Local Streamlit rendered QA passed at desktop (1440px), tablet (834px), and mobile (390px): Calendar was present, no console or framework-overlay errors occurred, and no horizontal page overflow occurred. A mobile tab-bar clipping defect was fixed during this proof.
- A clean-SHA static-public build bound Calendar and `data/v6-learning.json` to the isolated V6 release commit. It exited `2` as designed because all required daily source/outcome stages are degraded or missing. Public-artifact verification correctly refused promotion with only `snapshot_not_publishable` and `readiness_not_publishable`; it did not report an artifact path leak.
- The current-branch `scheduler-doctor` run against the actual runtime returned exit `2` and `BLOCKED_EXTERNAL`; it identified all four tasks as `Interactive`, battery-unsafe, and previously failed. The deployed old runtime still exits `0` for the same condition, proving the fail-closed scheduler fix is not yet deployed.

## Release gate still open

Production remains correctly unready. The actual runtime has no `config/web_sources.yaml`; only the placeholder example is available. A production source contract needs a real accountable user-agent contact, configured primary and independent quote providers, and must pass `web-source-doctor`. It also needs a dated, source-backed small-cap universe snapshot registered with lineage, ticker history, listing status, and corporate-action fields. Finally, task registration needs an approved password-logon Windows identity that can access the network, encrypted secrets, Telegram, the runtime, and the durable state root. Until all three prerequisites exist, no live data collection, trustworthy return label, readiness-200 publication, or claimed performance improvement is possible.

## Performance truth

`PERFORMANCE_STATUS=WAITING_FOR_FORWARD_EVIDENCE`

V6 needs at least 60 forward sessions and 100 valid, closed, sourced, after-cost paper labels before human review can even consider a change. It makes no promise of returns and never promotes itself.
