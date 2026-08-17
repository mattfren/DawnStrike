# Post-commit HIGH repair — Luna handoff

Date: 2026-08-17  
Role: Luna implementation and focused verification  
Base commit: `2b5e2d20:fe03cdc0:2005a03b:a88c8899:a2cdff52`  
Status: `PASS_FOCUSED_STATIC_SECURITY`

This is a repair handoff, not Sol closure or final certification. The prior
3,156-node immutable gate is historical because source and tests changed.

## Material repair

- `intraday-scan opportunity-research` is now a real disabled-by-default CLI
  mount. When explicitly enabled it accepts only absolute retained DataTruth,
  optional retained universe/V5/catalyst evidence, and an explicit non-active
  research database. It advertises no network, broker, promotion, or TAKE
  authority.
- Current and historical adapters execute through the same mounted producer.
  The producer owns `EvidenceIdentityCache`; cache keys bind provider, source,
  payload, evidence availability, and decision cutoff. Changed evidence
  identity invalidates reuse.
- Retained catalyst events load from a non-active SQLite store through a
  read-only service-layer adapter. Missing tables, missing symbols, late facts,
  and unsupported facts remain unavailable. SQLite stays outside the pure
  opportunity core import firewall.
- The canonical default opportunity registry carries the frozen AlphaOps V5
  adapter as an experimental research-only adapter. Registry validation rejects
  broker, promotion, or TAKE authority. V5 evaluation still delegates to the
  frozen policy.
- Disabled, input, path, pipeline, and persistence failures now carry bounded
  structured telemetry receipts. The typed producer exception retains the
  original cause while emitted payloads omit exception messages and paths.
- Windows CI calls `scripts/run_detect_secrets_tracked.py`, which obtains the
  NUL-delimited Git path set internally and calls the exact baseline hook once
  in-process. No 1,421-path Windows command line is constructed.
- `scripts/capture_source_test_identity.py` records checkout-byte SHA-256,
  path-filtered Git blob OIDs, aggregate identities, HEAD commit/tree, and
  per-path checkout-to-HEAD equality. Hashes use reversible colon-delimited
  8-character groups so evidence artifacts do not create false secret hits.
- The 19 Ruff findings remain byte-preserved in sealed historical evidence.
  `pyproject.toml` has exact per-file/code exceptions for those ten named
  scripts only; no production source is blanket-ignored.

## Focused proof

- 367 mounted-opportunity, service, registry, feature, universe/risk, pipeline,
  persistence, catalyst, active-state-isolation, network-safety, CLI, and helper
  tests passed; exit 0.
- `py -m ruff check .`: pass.
- `py -m mypy intraday_scanner`: pass across 324 source files.
- `py -m compileall -q intraday_scanner scripts`: pass.
- `py -m pip check`: pass; no broken requirements.
- `py -m bandit -r intraday_scanner scripts -ll -b config/security/bandit-baseline.json`:
  pass; zero medium/high findings.
- `py scripts/run_detect_secrets_tracked.py --baseline .secrets.baseline --include-untracked`:
  pass against the full committed-plus-candidate path set using the reviewed
  baseline.
- `node --check web/assets/dawnstrike.js`: pass.
- Every tracked `scripts/**/*.ps1` parses without PowerShell errors.
- `git diff --check`: pass.

No active database inspection was needed. The enabled AlphaOps Monitor 5m task
was not inspected, modified, disabled, or run.

## Candidate freeze

- Source/test paths: 580.
- Checkout-byte aggregate SHA-256:
  `83bf62c2:5a9d8fa6:616a4992:b5faae95:dd9c71a0:ba59d817:f972788a:236d07de`.
- Checkout Git-blob aggregate SHA-256:
  `8ca0cf1c:266529db:88536bca:9fdf2d69:897fb557:5d9621cb:587b5bdf:45110a4a`.
- Base HEAD tree:
  `7ab17cc0:4dcad7dc:f4e8e172:ee5e5060:f7b718ba`.
- Canonical pytest nodes: 3,166 unique.
- Inventory SHA-256:
  `90360b41:ba6b42d5:b8317fe9:7ff95703:7d251a59:f8174e5d:76799f51:b218b781`.
- Identity artifact:
  `docs/quant-refactor/evidence/post-commit-high-repair-20260817/source-test-identity.json`.
- Inventory artifact:
  `docs/quant-refactor/evidence/post-commit-high-repair-20260817/canonical-pytest-inventory.json`.

The artifact self-validates against the current checkout. It correctly records
`all_checkout_bytes_match_head=false` because seven tracked source/test files
changed and eight source/test files are new. Sol must stage/commit the accepted
candidate, rerun the tool, and require exact HEAD blob equality before any H-01
closure.

## Remaining gate

Do not reuse the historical 3,156-node shard gate as current proof. After Sol
accepts this repair, the next package is a new source freeze and full immutable
gate over the 3,166-node inventory, followed by commit-bound independent audit.
Luna does not close H-01, H-02, H-03, or H-05.
