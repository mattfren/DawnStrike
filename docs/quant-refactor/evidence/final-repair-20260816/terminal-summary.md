# Luna final-repair terminal evidence summary

Date: 2026-08-16
Repair cycle: 2 of 2
Terminal result: `REPAIR_REQUIRED`
Unresolved severity count: 2 CRITICAL, 4 HIGH

This packet is synthetic/non-empirical and has no promotion, provider, broker,
order, holdout, deployment, staging, or commit authority.

Frozen repaired source/test identity: 141 files at manifest SHA-256
`eb456c389b4daeb5be48eea4ba57fe95e1cb149ff764ec2af833c202a49cc3a6`,
based on HEAD `bec32fe752b91f4e1357236a538a6dfea5da56bf` plus the preserved working tree.

## Verified passes

- Active-state isolation, mounted opportunity mission, and daily publish: 15 passed.
- The three exact audit-observed nodes: 3 passed.
- Canonical return-truth adversarial matrix: 614 passed.
- Exact Bandit CI command: exit 0, no medium/high findings.
- Ruff, mypy (59 source files), compileall, pip check, Node syntax, and all
  PowerShell syntax: exit 0.
- Active-state final read-only capture: SHA-256
  `3ec2ffd2b83181ee14b918b88c87beac5c4831e28bd893e718d38f1acb69805c`,
  length `198836224`, mtime `2026-08-16T12:26:00.9825738Z`, schema 26,
  `query_only=1`, `quick_check=ok`, zero sidecars.

## Material failures and incomplete gates

1. C-00 remains CRITICAL. A session-start SQLite guard and focused regression
   tests prevent active-path opens before SQLite access, but the exact original
   full-suite node was not identified and complete suite-wide before/after
   proof was not earned.
2. C-01 remains CRITICAL. The authenticated canonical-return classifier and
   adversarial matrix pass, but mounted label/capture integration is not
   closure-ready. `tests/test_alpha_v6_labels.py` fails broadly after strict
   legacy-fallback removal, and stale outcome-capture fixtures fail. The
   production contract was not weakened to satisfy those fixtures.
3. H-05 remains HIGH. Bandit is clean and all 52 detect-secrets hits were
   reviewed into the baseline as false positives. The exact pre-commit command
   refuses the required unstaged baseline, while the capsule forbids Luna from
   staging, so the exact gate did not earn exit 0.
4. H-06 remains HIGH. The three-node rerun passes, but the full repaired
   inventory was not completed.
5. H-01 remains HIGH and Sol-owned: no commit was permitted or created.
6. H-04 remains HIGH and Sol-owned: the 63-requirement/14-finding matrix is
   proposed evidence only and contains no Luna self-closure.

## Inventory and CI evidence

The corrected deterministic collector reports 3,156 nodes on the repaired
tree, not the audit-era 1,909 nodes. Shard 0 selects 198 nodes and is recorded
in `shard-00-collection.json`. No claim is made that all 16 shards completed;
the mandatory exact-once suite gate remains unearned.

## Implemented proposed repairs retained for Sol

- Disabled-by-default, explicit non-active opportunity producer with shared
  current/historical adapters, immutable append/read-only replay, injected
  catalyst causality, structured telemetry, exact-evidence cache identity, and
  AlphaOps V5 delegation/parity.
- Active-path session guard at SQLite store/read-only/direct-connect test
  surfaces.
- Canonical return-truth classifier, path/capture integration, and strict label
  eligibility hook without `LEGACY_CONTRACT` fallback.
- Daily publish opportunity-projection fixture binding.
- Exact Bandit suppressions with adjacent fixed-identifier justification.
- Deterministic 16-way CI sharding implementation and corrected collection.
- WP006 packet, WP007 supersession seam, and proposed exact ledger matrix.
