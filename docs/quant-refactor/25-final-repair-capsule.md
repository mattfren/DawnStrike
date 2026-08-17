# Final Audit Repair Capsule

Date: 2026-08-16
Owner: Luna implementation; Sol architecture and adjudication
State: `CAPSULE_READY`
Repair budget: one comprehensive implementation attempt, one bounded correction

## Controlling evidence

- Independent audit:
  `docs/quant-refactor/24-final-independent-audit.md`
- Evidence index:
  `docs/quant-refactor/evidence/final-audit-20260816/evidence-index.md`
- Audit verdict: `HASH_DRIFT`
- Unresolved audit severities: 2 CRITICAL, 6 HIGH, 2 MEDIUM

No audit finding may be silently waived. This package must resolve the software
findings and produce exact evidence for Sol adjudication. It must not claim
empirical edge, open a real holdout, promote a strategy, place an order, call a
provider, deploy, or mutate the active database.

## C-00 adjudication and required isolation repair

The active database changed at `2026-08-16T12:26:00.982574Z`. Subsequent
read-only inspection establishes:

- The sole newest persisted timestamp is
  `scenario_model_registry.created_at = 2026-08-16T12:26:00Z` for
  `dawnstrike-news-scenario-v1:gpt-5.6-terra`.
- Installed Dawnstrike Task Scheduler history shows no task ran at that time,
  and the Task Scheduler operational log contains no Dawnstrike run event in
  the surrounding window.
- The Windows PowerShell operational record at exactly `12:26:00Z` contains the
  repository `scheduler_doctor_service._query_scheduled_tasks` command.
- The current Codex rollout proves the only long-running repository command at
  that timestamp was the independent audit's exact full-pytest child, PID
  `25464`, started at `09:38:00Z` and terminated at its bounded audit cutoff.
- Candidate source identity remained invariant throughout.

Adjudication: the write is attributable with high confidence to an
insufficiently isolated full-suite test path, not an installed scheduled task.
The exact individual node was not timestamped, so the repair must identify it
rather than assume it.

Required repair:

1. Reproduce the active-path resolution in a bounded diagnostic run against a
   disposable sentinel or access guard, never by permitting another active
   write.
2. Make every test and observer gate incapable of resolving
   `C:\r\dawnstrike-state\shadow_real.sqlite` unless an explicitly marked
   operator test opts in. Fail the test session before access, not after a
   mutation.
3. Add regression coverage for observer/build/scheduler/scenario paths and a
   full-suite active-state before/after identity gate.
4. Treat current active identity
   `3ec2ffd2b83181ee14b918b88c87beac5c4831e28bd893e718d38f1acb69805c`,
   length `198836224`, schema `26`, as the post-incident baseline. Do not
   restore or alter it.

## C-01 canonical return-truth repair

Port or equivalently implement the already-audited canonical return-truth
contract from the sibling isolated lane
`C:\r\dawnstrike-harvest-v1-20260809` (same base HEAD) into this candidate.
The integration must:

- require exactly one authenticated entered-paper receipt;
- bind intent identity, causal decision/entry time, observation/source/bar
  hashes, path identity, and official cohort identity;
- require the canonical `dawnstrike.path_entry_receipt.v1` contract;
- enforce halt, gap, ambiguous-path, and post-entry censoring;
- fail legacy, missing, aliased, duplicated, retrospective, and hash/time
  mutated evidence closed;
- remove the label builder's `LEGACY_CONTRACT` eligibility fallback;
- mount capture through label construction and learning eligibility;
- preserve research-only/no-broker/no-promotion boundaries.

Use seeded intent matrices, adversarial hash/time mutations, and mounted
capture-to-label tests. Do not copy another lane's conclusions without
revalidating the code and its dependencies in this worktree.

## H-02/H-03 end-to-end mission closure

Implement the missing software requirements as one disabled-by-default mounted
research path:

1. An explicit producer adapter supplies causal provider/universe evidence to
   the shared opportunity pipeline and appends the immutable result only to an
   explicit, non-active research database. The producer is false by default,
   cannot infer the active path, and has no broker or promotion authority.
2. Add the AlphaOps V5 delegating strategy adapter and byte-semantic parity
   tests over the shared rule implementation.
3. Add an injected point-in-time catalyst evidence adapter with observed-at,
   available-at, source identity, payload hash, and causal cutoff checks.
   Missing evidence remains unavailable; it is never synthesized.
4. Add structured stage telemetry with status, bounded failure codes, counts,
   and duration. Duration may be observational metadata but must not alter
   deterministic decision identity.
5. Add evidence-identity keyed cache reuse/invalidation with exact provider
   call-count tests. No cross-cutoff or cross-source reuse is allowed.
6. Add explicit current and historical adapters over the same core rules with
   causal-time and byte-equivalent parity tests.
7. Prove the mounted call graph from an explicit operator/CLI surface through
   producer, pipeline, durable append, read-only replay, and projection.

No provider/network call is permitted in verification. Use injected fixtures
and a disposable schema-30 database only. Active schema remains 26.

## H-05/H-06 and medium closure

- Resolve all eight Bandit B608 findings using static SQL or a narrow validated
  identifier helper. A suppression is allowed only with an adjacent precise
  safety justification and tests proving identifiers cannot be caller text.
- Review all 52 exact tracked-file detect-secrets findings. Update the baseline
  only for verified false positives; never blanket exclude candidate source,
  tests, or evidence and never preserve a real credential.
- Reconcile daily publish fixtures with the required opportunity projection
  artifact while retaining fail-closed artifact verification.
- Reproduce and eliminate the validation-persistence order dependency.
- Make the actual CI workflow complete within its declared bounds by sharding
  deterministic long tests or setting justified per-job timeouts. Do not simply
  skip the expensive validation collections.
- Add the missing canonical WP006 evidence index/packet and explicitly record
  the authorized WP007 execution-log supersession without rewriting sealed
  history.

## Ledgers and Git boundary

Luna must not self-close requirements/findings and must not commit, stage,
push, deploy, or modify installed tasks. It may add a proposed exact evidence
matrix mapping all 63 requirements and 14 prior findings for Sol adjudication.
H-01 is closed only after Sol commits the complete repaired candidate and
verifies it from the commit.

## Mandatory gates

All source/test identities must be frozen before the final gates. Capture raw
stdout/stderr, command, UTC start/end, elapsed time, exit code, environment,
and a sealed evidence manifest.

At minimum:

- focused canonical return-truth and mounted capture-to-label tests;
- focused producer/V5/catalyst/telemetry/cache/current-historical tests;
- the three audit-observed pytest node IDs;
- daily publication/build/readiness/verifier collections;
- opportunity persistence, pipeline, validation, robustness, and projection
  collections;
- SQLite read-only/observer/test-isolation collections;
- exact Bandit CI command;
- exact tracked-file detect-secrets CI command;
- Ruff, mypy, compileall, pip check, Node syntax, all PowerShell syntax,
  `git diff --check`, import/network/broker/promotion firewalls;
- every CI shard, covering all 1,909 collected nodes exactly once with zero
  failures, skips, xfails, xpasses, missing nodes, or duplicates;
- active database before/after URI `mode=ro`, `PRAGMA query_only=ON`, hash,
  length, mtime, schema, quick-check, and sidecar proof.

## Terminal worker result

Return only `PASS_CANDIDATE_FOR_SOL_ADJUDICATION`, `REPAIR_REQUIRED`,
`BLOCKED`, `INFRA_FAILURE`, or `HASH_DRIFT`, with exact artifacts, repair-cycle
count, and unresolved critical/high defects. Synthetic evidence must remain
explicitly non-empirical and non-promotional.
