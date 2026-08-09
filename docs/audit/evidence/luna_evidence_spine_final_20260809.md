# Luna evidence spine final proof

Status: `READY_FOR_OPERATOR_REVIEW`

The implementation is isolated at `C:\r\dawnstrike-luna-evidence-spine-20260809` on branch `codex/luna-dawnstrike-evidence-spine`. Starting source SHA: `ba39a5353045b7d417936ed1aed0ee4802169759`. The implementation HEAD before this proof packet is `2427daaef1adb9a76485aa33d2d75b499d288adb`, tree object `873cf79c41df2bc48535367a01acb6195cef73bf`. The proof commit SHA is intentionally not in this packet.

Final proof gates passed: 863 no-cache tests in 769.88 seconds, 263 targeted parity tests, pip check, Ruff, mypy across 238 source files, compileall, Node syntax, PowerShell parser, Bandit medium/high, and git diff check.

Migration rehearsals from schema 21 to 26 passed on two fresh copies and one recovery copy with `quick_check=ok`, idempotent second initialization, deterministic counts, and preserved old reads. The untouched Stage A snapshot remained schema 21 with SHA-256 `6CB1D052C1E0F7FB6C5416E21E6A6A330E8CEA615B12B43C1F206523F33C9893`.

The retained evidence path is honest and bounded: the fixture cohort verified one page, one artifact, and one coverage receipt. The Massive consolidated provider is blocked by external entitlement (`credential_present=false`); no live network probe, live backfill, or raw retention was claimed. Active path-replay and intraday-artifact counts remain zero in the audited snapshot.

Frozen cohort counts are 258 historical signal rows, 171 deduplicated sampling rows, 53 legacy-policy members, 17 official no-trade rows, 29 official outcome-required rows, and 50 counterfactual samples. The protocol and cohort hashes are recorded in the JSON packet; raw cohort members are not copied here.

V5 parity and challenger contracts remain research-only. The modeled cost is 50 bps per side plus current commission and remains provisional pending empirical fills. Risk, liquidity, ATR, causal-exit, catalyst, and V6 challengers are not automatically promoted. V6 remains `NOT_TRAINED_INSUFFICIENT_LABELS`, with purged/embargoed walk-forward and untouched-holdout gates preserved. Catalyst ablations are registered as full, no-catalyst, catalyst-only, and shuffled negative-control; no catalyst-dominance claim is made.

Trade attribution is diagnostic only. A single reconciled trade is labeled `unexplained_within_predeclared_model_distribution`; factor coverage remains separate as complete/partial/unknown, missing truth is not zero, and aggregate remediation is `NOT_EVALUABLE_PENDING_PROTOCOL_APPROVAL`. No unique causal claim or automatic policy mutation is emitted.

External blockers remain: five GitPython advisories from `pip_audit`, and pre-existing findings reported by the tracked secret baseline. No broker/order surface, active runtime/state, scheduler, deployment, or publication was changed. No profitability or live-readiness claim is made.

The K006 execution-ledger checkpoint SHA-256 is `30F688A5FDF177784004CA486DD7E1A4A8E1730C3061D58AB991702319D69A73`. The JSON packet records the sanitized task XML, database snapshot, SBOM, protocol, cohort, provider, fixture, commit, and changed-file hashes.
