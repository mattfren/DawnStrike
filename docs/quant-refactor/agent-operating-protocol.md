# Dawnstrike quant-refactor agent operating protocol

Protocol version: `bootstrap-to-ds-heavy-v1`

Activated: 2026-08-12

Applies to: the paused Dawnstrike quant-refactor program in
`C:\r\dawnstrike-quant-refactor-20260811`

## 1. Authority and precedence

This protocol governs coordination, ownership, evidence, and handoff. It does
not amend the target architecture, accept an increment, close a requirement or
finding, authorize a release, or relax any safety boundary.

When sources disagree, use this order:

1. the user's current controlling instruction and explicit stop or scope limit;
2. repository source, content hashes, durable command artifacts, and directly
   inspected read-only state;
3. Sol-adjudicated accepted audit records and the requirements/audit ledgers;
4. `00-current-state-audit.md`, `01-target-architecture.md`, and the current
   Sol-issued work-package decision;
5. this protocol;
6. the active task capsule and later capsule deltas;
7. worker summaries, commentary, older plans, and external workflow guidance.

Accepted Sol audits are immutable historical evidence. A later protocol,
capsule, failure, or rerun may add a new record but may not rewrite the WP005
prompt or an accepted audit. A conflict, missing authority, or unverifiable
state fails closed and returns to Sol for adjudication.

The worktree may intentionally be dirty. Its declared branch, HEAD, status,
protected paths, and start hashes are part of the expected state; cleanliness
must never be invented by resetting or discarding existing work.

## 2. Roles and decision rights

| Role | Owns | Must not do |
| --- | --- | --- |
| Main/Sol | Knowledge architecture, package boundaries, dependency order, task distribution, targeted adversarial audit, requirement and finding adjudication, official status, and user communication | Re-enter routine implementation, debugging, test-repair, or documentation loops; auto-close requirements from worker reports |
| Luna explorer | Bounded read-only project exploration, dependency and contract mapping, contradiction discovery, and decision-ready briefs from declared or frozen state | Edit files, Git state, runtime state, or make architecture/acceptance decisions |
| Luna executor | Capsule-local implementation, debugging, focused tests, self-check, and up to two routine repair cycles | Edit outside the owned write set, change protected contracts without authority, or claim acceptance |
| Luna gate operator | Run the exact focused, broad, affected, and static gates over frozen sources; preserve durable process and exit evidence | Edit source/tests/docs during a gate lease, substitute commands, or count a lost-terminal run |
| Luna verifier/reconciler | In a separate Luna task, validate frozen hashes, exact command and collection, durable logs and exit artifacts, process termination, scope, limitations, and evidence coherence before Sol review | Repair production code, mutate frozen sources, adjudicate requirements/findings, or replace missing proof with inference |
| Luna documentation reconciler | After Sol's decision, draft or append only the authorized durable records and reconcile the end-session evidence packet | Rewrite accepted audits or ledgers, stage/commit/push, or turn evidence into an official acceptance claim |

One Luna task may carry executor and gate-operator context serially when the
capsule says so. The independent verifier/reconciler must be a separate Luna
task and must receive frozen references and hashes, not the executor's full
transcript. Sol remains the only adjudicator and the only user-facing authority.

## 3. Governed state machine

The top-level state sequence is:

```text
PAUSED
  -> LUNA_EXPLORER_READ_ONLY
  -> SOL_ARCHITECTURE_DECISION
  -> CAPSULE_ISSUED
  -> LUNA_WRITER_LEASE
  -> SOURCE_FROZEN
  -> LUNA_GATE_LEASE
  -> LUNA_VERIFIER_LEASE
  -> SOL_ADJUDICATION
  -> LUNA_DOC_RECONCILIATION
  -> ACCEPTED_HOLD

SOL_ADJUDICATION -> REPAIR_CAPSULE -> CAPSULE_ISSUED
SOL_ADJUDICATION -> BLOCKED

LUNA_GATE_LEASE
  -> GATE_FAILED_OWNED
  -> LUNA_WRITER_LEASE
  -> SOURCE_FROZEN
  -> LUNA_GATE_LEASE
```

State meanings and transitions:

- `PAUSED`: no mutable lease and no unadjudicated implementation starts.
- `LUNA_EXPLORER_READ_ONLY`: Luna explores only the declared state and returns
  a compact planning or knowledge-delta brief.
- `SOL_ARCHITECTURE_DECISION`: Sol resolves scope, contracts, ownership,
  dependencies, invariants, and acceptance criteria.
- `CAPSULE_ISSUED`: Sol sends one self-contained, guidance-rich capsule. A new
  task spawn uses `fork_turns=none`; the full Sol transcript is not replayed.
- `LUNA_WRITER_LEASE`: exactly one mutable lease exists. The executor owns only
  the capsule's `OWNED_WRITE_SET`, including assigned tests or documentation.
- `SOURCE_FROZEN`: the writer is released; the exact branch, HEAD, status,
  owned files, relevant neighbors, and test files are hashed. No writer exists.
- `LUNA_GATE_LEASE`: one gate operator runs only the exact frozen commands and
  writes durable evidence. No source edit is permitted.
- `GATE_FAILED_OWNED`: a nonarchitectural failure inside the capsule's owned
  production surface returns directly to the same capsule's Luna executor. The
  failed gate attempt is sealed before the mutable lease is reacquired.
- `LUNA_VERIFIER_LEASE`: a separate Luna validates evidence and scope against
  the frozen state. It does not repair the candidate.
- `SOL_ADJUDICATION`: Sol performs only the targeted adversarial inspection
  needed for material uncertainty, then accepts, rejects, narrows, blocks, or
  issues a repair capsule. Luna evidence cannot change ledger status itself.
- `LUNA_DOC_RECONCILIATION`: after Sol's decision, Luna records the authorized
  verdict, commands, counts, hashes, limitations, preservation proof, and next
  hold without changing source.
- `ACCEPTED_HOLD`: the increment is durably recorded and work pauses until Sol
  issues the next architecture decision. Acceptance of an additive increment
  is not global closure, profitability, promotion, or production readiness.
- `REPAIR_CAPSULE`: Sol issues a new capsule iteration with the failed
  criterion, changed knowledge, exact ownership, and rerun boundary.
- `BLOCKED`: exact missing authority, evidence, data, or safety condition is
  recorded; no worker works around it.

Routine executor/tester failures and repairs remain inside Luna operational
context and do not return raw failure traffic to Sol. The same criterion may
receive at most two nonarchitectural focused repair cycles after its initial
failure. Each cycle uses a new repair iteration, gate attempt ID, log, exit
JSON, and pre/post hash evidence. If the criterion still fails after the second
repair cycle, the third failed gate leaves the operational loop and escalates.
Architecture, scope, safety, migration, security, or protected-contract impact
escalates immediately instead of entering this direct loop.

## 4. Task capsule contract

Every capsule is self-contained, no more than 1,500 words, and uses exactly
these top-level fields. A later message is a capsule delta and must identify the
capsule and iteration; it cannot silently broaden authority.

```text
CAPSULE_ID: unique stable package-and-iteration identifier
PROTOCOL_VERSION: controlling protocol version
WORK_PACKAGE: exact increment, objective, and hold boundary
ROLE: explorer | executor | gate_operator | verifier | documentation_reconciler
EXPECTED_STATE: branch, HEAD, dirty-status shape, process state, and known artifacts
AUTHORITATIVE_PATHS: exact files/artifacts that must be read before action
DECISIONS_FIXED: architecture and coordination decisions not open to reinterpretation
INVARIANTS: causal, safety, evidence, determinism, and honesty rules
OWNED_WRITE_SET: exhaustive paths the role may mutate; empty for read-only roles
READ_ONLY_NEIGHBORS: adjacent paths that may be inspected but not edited
START_HASHES: exact algorithm and path-to-hash map for frozen/protected inputs
REQUIRED_GATES: exact cwd, commands, expected collections, order, and pass criteria
EVIDENCE_PATHS: unique durable log, exit JSON, receipt, and report locations
ESCALATION_RULES: explicit stop conditions and repair-cycle limit
RETURN_SCHEMA: the exact material-delta schema required below
```

`OWNED_WRITE_SET` is exhaustive, not illustrative. `REQUIRED_GATES` cannot say
"run relevant tests"; it must name the literal command, expected collection or
explicitly defined acceptable variance, and static checks. `START_HASHES` and
`EVIDENCE_PATHS` use immutable references so a verifier does not need hidden
thread history.

## 5. Material-delta return contract

Worker returns contain only knowledge needed for coordination or adjudication.
Every event is at most 120 words; every final return is at most 350 words.

```text
STATUS: COMPLETE | REPAIRED | NEEDS_DECISION | BLOCKED
MATERIAL_CHANGES: concise contract, behavior, or evidence delta; "none" if none
DECISIONS_REQUIRED: exact Sol decision or "none"
FILES_CHANGED: exact owned paths or "none"
GATE_EVIDENCE: claim, result, exact command/method, count, and artifact paths
HASH_RESULT: algorithm, pre/post comparison, drift result, and frozen manifest path
LIMITATIONS: unresolved data, coverage, proof, or applicability limits
BLOCKER: exact blocker or "none"
NEXT_HOLD: authorized next state and explicitly paused work
```

Updates are event-driven only: lease acquisition/release, a material knowledge
change, a scope or architecture conflict, a completed durable gate, evidence
loss, escalation, adjudication, or final reconciliation. Routine progress,
individual passing tests, raw logs, code dumps, repeated status checks, and
token/benchmark screenshots do not travel upward. Detailed diagnostics remain
in the worker context or cited durable artifacts.

## 6. Ownership leases and concurrency

- Exactly one mutable lease may exist in the shared worktree. It declares the
  capsule, role, owner, owned write set, protected paths, acquisition time,
  expected state, and release/freeze evidence.
- No second writer, formatter, fixer, doc editor, dependency command, Git
  mutation, or generated-file producer may overlap a mutable lease.
- Read-only exploration or audit may run concurrently only when its capsule
  identifies the declared or frozen state and it does not create worktree
  artifacts, refresh indexes, change timestamps intentionally, or mutate Git.
- A gate lease starts only after the mutable lease is released and
  `SOURCE_FROZEN` is recorded. No source, test, or documentation edit is allowed
  until all required gates finish or are invalidated.
- The verifier lease starts only after the gate operator has terminated every
  child process and sealed the evidence. It is read-only with respect to the
  candidate and may write only its separately authorized evidence report.
- Lease transfer is explicit. Silence, task termination, a lost terminal, or an
  apparently idle process does not transfer ownership.
- Automatic worker scaling is disabled. Worker count is bounded by declared
  independent ownership and available platform capacity, never by an upstream
  maximum. There is no executor-to-Sol fallback for routine work.

## 7. Durable long-gate protocol

Before a long gate starts, the gate operator must durably record:

- capsule and gate IDs;
- exact absolute cwd;
- literal command and environment differences;
- expected collection count and how it was obtained;
- UTC and local start timestamps;
- launcher PID and, once known, every worker/child PID;
- algorithm and pre-gate hashes for every frozen source and test path;
- unique stdout/stderr log path and unique exit-JSON path.

The wrapper timeout must be absent or longer than the declared expected
duration plus a safety margin. A UI wait, orchestration timeout, disconnected
terminal, or truncated stream must not kill or orphan evidence collection. The
durable log is append-only for that attempt and may not be reused by a rerun.

The exit JSON must contain at least capsule ID, gate ID, cwd, literal command,
expected and observed collection counts, start/end times, launcher/worker PIDs,
exit code, completion status, log path and log hash, pre/post hash-manifest
paths, and limitations. A green line without a captured process exit is not a
valid result.

After the command ends, record the exit JSON and post-gate hashes, then prove
that no launcher or pytest worker survives. Any source hash or status drift
invalidates the gate. The operator may not repair and continue inside the same
attempt. For an owned, nonarchitectural failure, it seals that attempt and
transfers the same capsule through `GATE_FAILED_OWNED -> LUNA_WRITER_LEASE`;
after repair, the source is frozen again and a new `LUNA_GATE_LEASE` attempt
starts with new evidence identities. Sol is contacted only for architecture,
scope, safety, or other listed escalation conditions, or when two repair cycles
have not cleared the criterion.

If terminal or exit evidence is lost, the attempt is invalid even if process
output appears to have reached 100%. Before one unchanged rerun can be
authorized, prove both that no worker survives and that the frozen hashes,
branch, HEAD, and declared status have not drifted. Use a new gate ID, log, and
exit artifact. A second evidence loss or any drift escalates; it is not retried
automatically.

The separate Luna verifier checks the exact cwd, command, collection, start and
end chronology, PID termination, exit code, complete log, log hash, pre/post
source hashes, worktree scope, static-gate results, and stated limitations. It
reports proof gaps; it never converts incomplete evidence into a pass.

## 8. Context and token controls

- Every new Explorer, executor, tester/gate, verifier, or documentation task is
  spawned with `fork_turns=none` and receives the capsule, immutable references,
  and only the material deltas it needs.
- Do not replay the full Sol transcript. Refer to accepted audits and frozen
  artifacts by exact path and SHA-256.
- Capsule: maximum 1,500 words. Event: maximum 120 words. Final material delta:
  maximum 350 words.
- Follow-ups contain only capsule ID/iteration, changed state or scope, new
  evidence, affected criterion, updated guidance, and next action.
- Raw logs, large diffs, source inventories, test output, API responses, and
  code dumps stay in durable artifacts or the owning task's context.
- Only material knowledge deltas return to Sol. No activity polling, routine
  progress narration, benchmark/token screenshots, or transcript forwarding is
  accepted as proof.

## 9. Escalation and stop rules

Stop the current lease and preserve evidence when any of these occurs:

- protected-file, branch, HEAD, index, worktree-status, ownership, or start-hash
  drift outside the declared capsule;
- a needed edit outside `OWNED_WRITE_SET` or an undeclared public/cross-package
  contract change;
- migration, schema, security, dependency, credential, entitlement, or release
  risk not explicitly authorized;
- any active database, mounted runtime, UI, external network/provider, broker,
  scheduler, deployment, commit, stage, push, or publication action;
- causal-time, target leakage, survivorship, benchmark-overlap, cost-lineage,
  source-entitlement, fill/path, or cohort-membership ambiguity;
- source mutation during a gate lease, collection mismatch, surviving worker,
  missing exit status, truncated/lost evidence, or inconsistent hashes;
- a proposed requirement/finding closure, OOS/holdout validity, profitability,
  promotion, production-readiness, or certification claim;
- the same acceptance criterion still failing after two focused repair cycles;
- a user stop, missing authority, contradictory accepted evidence, or a blocker
  that cannot be resolved inside the capsule.

A stop report names the failed step, evidence, suspected cause, preserved state,
affected criterion, exact decision needed, and safe next hold. Workers do not
expand scope, weaken an invariant, substitute a smaller command, fabricate
missing truth, or silently continue.

## 10. End-session reconciliation and adjudication

The independent Luna verifier/reconciler first seals a compact evidence packet:
frozen manifest, exact commands and counts, logs and exit JSON, hash result,
scope diff, limitations, blockers, and recommended next hold. Sol then decides
whether the increment is accepted as additive evidence, requires a repair
capsule, or is blocked.

Only Sol may change a requirement or finding status, accept/reject an
increment, authorize the next package, make a profitability or readiness
statement, and communicate official status to the user. Even after a successful
adjudication, Luna documentation reconciliation changes only the paths Sol
explicitly owns out and records no broader conclusion.

This project rejects automatic commit, stage, push, deployment, requirement
closure, or lifecycle promotion. It also rejects uncontrolled worker counts,
executor-Sol fallback, and benchmark, Reddit, token, or screenshot claims as
quantitative proof. Git and external-state actions require separate explicit
user authority and a new capsule.

## 11. Project safety boundary

All established no-live boundaries remain: no broker/order execution, active
database mutation, mounted runtime change, operator UI integration, external
network/provider acquisition, scheduler mutation, deployment, publication, or
release action unless the user and Sol explicitly authorize a later bounded
package. Missing or insufficient truth remains null, unavailable, insufficient,
or externally blocked; it never becomes zero, empirical, validated, or
profitable by default.

## 12. Workflow provenance

This protocol adapts the public workflow's knowledge-plane/operational-plane
separation, read-only Explorer, guidance-rich capsules, direct executor/tester
repair loop, layered evidence, and end-session reconciliation concepts from
[Heavy Route](https://github.com/viettran-edgeAI/codex_workflow/blob/main/codex_workflow/heavy_route.md),
[Explorer Companion](https://github.com/viettran-edgeAI/codex_workflow/blob/main/codex_workflow/explorer_companion.md),
[executor_luna.toml](https://github.com/viettran-edgeAI/codex_workflow/blob/main/codex_workflow/agents/executor_luna.toml),
[tester.toml](https://github.com/viettran-edgeAI/codex_workflow/blob/main/codex_workflow/agents/tester.toml),
and [End-of-Session Handoff](https://github.com/viettran-edgeAI/codex_workflow/blob/main/codex_workflow/end_of_session.md).

Those sources are design provenance, not Dawnstrike evidence. Their automatic
Git/deployment closure and broad worker limits are deliberately not adopted.
Reddit reports and benchmark or token screenshots are anecdotal and are not
quantitative proof of correctness, efficiency, edge, profitability, or release
readiness.
