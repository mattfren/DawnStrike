# SOL audit — WP005 increment A chronological corpus and split controls

## Verdict

`ACCEPTED_AS_ADDITIVE_FOUNDATION`

This verdict accepts the downstream-only frozen validation corpus, exact
historical-membership evidence, whole-session chronological allocation,
purge/embargo controls, expanding walk-forward folds, and bounded timestamp-
leakage audit. It does not accept a durably locked OOS, one-time holdout access,
trading metrics, BASE/2X/3X cost stress, perturbation/stability evidence,
promotion, validation persistence, mounted runtime, operator UI, active-
database migration, profitability, or the global mission.

## Accepted scope

- strict validation enums, policies, normalized membership/source evidence,
  and holdout-access evidence in
  `intraday_scanner/v2/opportunity/validation_contracts.py`;
- exact current-outcome replay session reconciliation, policy-bound horizon
  selection, population retention, and required-availability derivation in
  `intraday_scanner/v2/opportunity/validation_corpus.py`;
- count-declared contiguous regions, whole-session purge/embargo, honest
  holdout integrity, and exact expanding folds in
  `intraday_scanner/v2/opportunity/validation_split.py`;
- fixed timestamp/leakage checks and the final nonpromotable preparation
  receipt in `intraday_scanner/v2/opportunity/validation_audit.py`;
- the explicit downstream-only facade in
  `intraday_scanner/v2/opportunity/validation.py`;
- numerical, causal, boundary, incomplete-truth, survivorship, strict-
  construction, tamper, mutation-isolation, and import-firewall proof in
  `tests/test_opportunity_validation.py`.

No accepted opportunity/outcome/miss/metric source, package root, persistence
schema, active database, runtime, UI, network, broker, scheduler, deployment,
commit, or push path changed in this increment.

## Accepted corpus and survivorship facts

- The corpus embeds exact `CurrentOutcomeReplay` bodies and therefore exact
  persisted `PipelineResult`, run/outcome receipts and correction chain,
  outcomes, universe, configs, evaluations, ranks, decisions, risks, and traces.
  It does not relabel, rescore, rerank, regate, or implement a backtest strategy.
- Session identity is the exact exchange-session ID plus UTC open/close from
  accepted outcome horizons. Replays are ordered by decision time/run/replay;
  sessions are strictly chronological and nonoverlapping. Duplicate run,
  replay, evaluation, horizon, or universe identities with conflicting content
  reject.
- Horizon selection is policy-bound. Elapsed horizons require the exact
  declared integral seconds; session-close horizons require exact close. Every
  replay must contain exactly one matching horizon and one exact outcome for
  every evaluation in original preparation order.
- PASS, INSUFFICIENT, WATCH, TAKE, nonrankable, no-entry, pending, partial,
  censored, unavailable, ambiguous, unattainable, and unsupported states remain
  represented. Incomplete truth makes the corpus/split/preparation insufficient
  rather than dropping the row or inventing a label.
- A normalized membership body embeds exact requested symbols and exact
  included/excluded/benchmark `UniverseMember` bodies, membership effective and
  observed times, source identity/version/method, and canonical schema. Its
  normalized SHA-256 excludes artifact inventory to avoid a cycle.
- The body separately embeds a canonical nonempty tuple of exact source
  artifacts. Every artifact must carry the same normalized body hash and exact
  provider/source/version/method/observed lineage. Historical artifacts may be
  fetched after their effective/as-of time; corpus freeze and row availability
  bind that later fetch honestly.
- `POINT_IN_TIME` requires membership effective at or before the exact universe
  as-of. Later-effective membership is only `CURRENT_MEMBERSHIP_PROXY`.
  Proxy, unknown, or unavailable evidence blocks a strong required-OOS study as
  `EXTERNAL_DATA_BLOCKED`; bounded `UniverseSnapshot` alone never proves
  survivorship safety.
- Every row's `required_available_at` is the exact maximum of its selected
  horizon, run and complete outcome persistence chronology, batch/record/source
  freezes, used bar availability, coverage/status/action fetches, risk numeric,
  capability and safety observations, plus every session membership
  effective/observed and source-artifact observed/fetched time. Direct and JSON
  reconstruction recompute it from embedded bodies.

## Accepted split, fold, and holdout facts

- Raw train/research, validation, and OOS regions are contiguous slices of the
  exact ordered session inventory and must exactly cover it. Required regions
  cannot declare zero sessions.
- A declared last-N positional embargo removes the final N sessions of the
  earlier train and validation regions. Embargoed sessions cannot count toward
  training, validation, or OOS samples.
- Purge operates at whole-session granularity. An earlier session is purged
  when any row's label end or required availability is greater than or equal to
  the next evaluation region's first decision. Equality is leakage.
- Walk-forward validation uses disjoint ordered windows, retains a final shorter
  nonempty window, and covers every final validation session exactly once. The
  training inventory expands with prior validation sessions only after each
  fold's embargo and purge. Every fold binds exact session hashes and row IDs;
  OOS cannot appear in train, validation, purge, embargo, or tuning inventory.
- A window lacking minimum training or rows remains an explicit insufficient
  fold. Over-purge, over-embargo, empty required regions, incomplete corpus, or
  uncovered windows make the collection/preparation insufficient; they are not
  omitted while the parent claims validity.
- Increment A intentionally has no valid-lock state. A split declaration before
  the first OOS decision yields only
  `DECLARED_BEFORE_OOS_NOT_DURABLY_VERIFIED`; equality or later is
  retrospective. Previously evaluated evidence fails a fresh-lock claim.
  Unknown/unavailable access remains unresolved.
- A required OOS without durable one-time access proof cannot produce
  `PASSED_BOUNDED` or a ready preparation. A bounded software-only audit without
  an OOS claim may pass its applicable split checks while holdout and strong-
  survivorship checks remain `NOT_APPLICABLE`.
- Every public artifact is content-bound, research-only, and
  `promotion_eligible=False`. Direct and strict JSON construction recomputes
  source bodies, corpus rows, allocation, purge, embargo, fold coverage, checks,
  counts, limitations, chronology, and identities. Unknown fields, duplicate
  keys, schema drift, float injection, naive time, private/path-shaped lineage,
  omission, injection, reorder, cross-content substitution, and consistent-
  rehash tampering fail closed.

## Sol adversarial findings remediated

1. The approved design initially allowed a normalized membership body to
   inventory exact artifact IDs/hashes without forcing its source metadata or
   normalized hash to match those embedded artifact bodies. The final contract
   binds one normalized body hash and exact source metadata across every
   artifact; direct/from-JSON substitution attacks reject.
2. The first fold implementation suppressed all folds whenever the split plan
   was insufficient. An asymmetric over-embargo case could therefore leave
   validation allocations that the fold DTO could neither cover nor represent.
   Final derivation retains every validation window, marks affected folds and
   collection insufficient, and preserves exact final-short-window coverage.
3. The first split status ignored an EMPTY/INCOMPLETE corpus after the region
   allocation itself remained nonempty. Pending/censored/unavailable labels
   could reach an apparently available fold and bounded-ready preparation.
   Corpus incompleteness now propagates through split, applicable checks, audit,
   and preparation without dropping rows.
4. Corpus freeze covered survivorship evidence, but row-level
   `required_available_at` initially excluded membership and source-artifact
   times. Since purge used the row timestamp, a training session whose exact
   membership file arrived after validation began could enter a fold. Final rows
   include all survivorship availability. Fetch exactly at validation decision
   purges the whole session; one microsecond before is retained.

Initial implementation failures were otherwise mechanical or fixture-scoped:
builder identity payloads omitted default schema fields, a proxy fixture froze
before its later-effective fact, one strict-time regex expected different
wording, and an early test overclaimed that every later fold must be
insufficient after a first undertrained fold. No unresolved correctness defect
remains in increment A's accepted scope.

## Independent proof

Sol independently reran the five highest-risk provenance, over-embargo,
incomplete-corpus, holdout, and survivorship-purge reproducers: `5 passed`, exit
0, 119.7 seconds. Luna's final focused command was:

```powershell
py -m pytest tests/test_opportunity_validation.py -q -p no:cacheprovider
```

Result: `24 passed`, exit 0, 490.8 seconds.

Final combined WP005/WP004/WP003 opportunity, validation, metric, miss, outcome,
persistence, and migration gate:

```powershell
py -m pytest tests/test_opportunity_validation.py tests/test_opportunity_metric_persistence.py tests/test_opportunity_discovery_metrics.py tests/test_opportunity_miss_persistence.py tests/test_opportunity_missed.py tests/test_opportunity_outcomes.py tests/test_opportunity_outcome_persistence.py tests/test_opportunity_persistence.py tests/test_intraday_evidence_migration.py tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py -q -p no:cacheprovider
```

Exact collection: 621. Final durable result: `621 passed`, exit 0, 7,574.635
seconds. The accepted SQLite/data-truth gate passed `139/139`, exit 0, in
135.153 seconds.

The first frozen combined worker survived its wrapper's 7,204.1-second timeout
and then exited after the closed pipe made its pytest summary and exit code
irretrievable. Sol did not count that run as evidence. One unchanged rerun was
authorized only after verifying no pytest worker remained and all six frozen
hashes matched. It wrote stdout and exit status to unique temporary artifacts,
reached 100%, and produced the green evidence above. No source or test edit
occurred during either run.

Final whole-repository Ruff passed; mypy reported no issues in 297 source files;
compileall for `intraday_scanner` and `scripts` passed; and `git diff --check`
passed with only pre-existing shared-worktree line-ending notices. Frozen hashes
before and after were identical; the validation test SHA-256 is
`2EE93FC055881D3DA05D1E51F8FD215FB04050ACCDB97BE8925BA00E6DB427CF`.

Sol's fresh-process import check loaded zero validation modules after importing
the opportunity root, pipeline, and all accepted storage adapters. An AST scan
found no V6, backtest, app, broker, network, scheduler, or Streamlit dependency
in the validation modules. All five implementation modules are below 800 lines
and 40 KB.

## Requirement adjudication

No global requirement or finding is closed. Increment A supplies additive
evidence toward REQ-ARCH-002, REQ-SAFE-001/002, REQ-DATA-003/005/006,
REQ-OUT-002/003, REQ-BT-001/002/006, REQ-TRACE-001, REQ-TEST-001/002, and
REQ-DOC-001. REQ-BT-002 remains OPEN because durable one-time locked-OOS access
and persistence are not implemented. Acceptance still depends on exact trading
metrics, BASE/2X/3X execution stress, parameter perturbation and stability,
multiple-hypothesis and baseline evidence, validation persistence, empirical
external data, disabled-by-default read-only projection, active-database
migration, operator UI, and final clean-worktree end-to-end proof.
