# Final repair evidence matrix

This is Sol's final software-evidence adjudication matrix as of 2026-08-17. It
binds accepted WP001-WP007 audits and the commit-bound 3,166-node final gate to
the requirements and prior findings. `PASS_SOFTWARE_ONLY` means the contract
and deterministic proof pass without claiming empirical edge, promotion
eligibility, or production trading readiness.

## Requirements (63)

| Requirement | Title | Final adjudication | Exact evidence route |
| --- | --- | --- | --- |
| REQ-ARCH-001 | Additive opportunity core | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-ARCH-002 | Shared research/live rules | PASS | tests/test_final_repair_opportunity_mission.py; intraday_scanner/v2/opportunity/producer.py; intraday_scanner/v2/opportunity/catalyst.py |
| REQ-ARCH-003 | Staged computation | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-SAFE-001 | No live execution | PASS | tests/test_network_safety.py; tests/test_opportunity_contracts.py; tests/test_opportunity_validation_robustness.py; import/broker/promotion firewalls |
| REQ-SAFE-002 | Experimental isolation | PASS | tests/test_network_safety.py; tests/test_opportunity_contracts.py; tests/test_opportunity_validation_robustness.py; import/broker/promotion firewalls |
| REQ-DATA-001 | Point-in-time universe | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-DATA-002 | Provider capability truth | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-DATA-003 | Immutable evidence identity | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-DATA-004 | Causal timestamps | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-DATA-005 | Missing truth is not zero | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-DATA-006 | Corporate-action and halt status | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-FEAT-001 | FeatureSnapshot contract | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-FEAT-002 | Price features | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-FEAT-003 | Volume features | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-FEAT-004 | VWAP features | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-FEAT-005 | Relative features | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-FEAT-006 | Time and catalyst features | PASS | tests/test_final_repair_opportunity_mission.py; intraday_scanner/v2/opportunity/producer.py; intraday_scanner/v2/opportunity/catalyst.py |
| REQ-FEAT-007 | Order-flow capability gate | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-DISC-001 | Strategy-independent discovery | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-DISC-002 | Normalized anomalies | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-DISC-003 | Broad anomaly coverage | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-REG-001 | Market regime | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-REG-002 | Security regime | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-REG-003 | Candidate indicators not doctrine | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 09-wp002-increment-b3-sol-audit.md; tests/test_opportunity_features.py; tests/test_opportunity_universe_risk.py |
| REQ-STRAT-001 | Versioned registry | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-STRAT-002 | Initial supported families | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-STRAT-003 | Disabled order-flow families | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-STRAT-004 | Reuse AlphaOps V5 | PASS | tests/test_final_repair_opportunity_mission.py; intraday_scanner/v2/opportunity/producer.py; intraday_scanner/v2/opportunity/catalyst.py |
| REQ-LIFE-001 | Lifecycle state machine | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-EVAL-001 | Symbol-plus-strategy evaluation | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-EV-001 | Expectancy in R | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-EV-002 | Uncertainty and stability | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-EV-003 | Heuristic honesty | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-RANK-001 | Cross-market pair ranking | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-RANK-002 | Rank/gate separation | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-RANK-003 | Concentration penalty | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-GATE-001 | Absolute decisions | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-GATE-002 | TAKE evidence threshold | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-RISK-001 | Execution and liquidity checks | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-RISK-002 | Fixed-fractional and concentration | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-OUT-001 | Capture all evaluated pairs | PASS | docs/quant-refactor/10-wp003-increment-a-sol-audit.md through 15-wp004-increment-c-sol-audit.md; opportunity outcome/miss/metric tests |
| REQ-OUT-002 | Future-label isolation | PASS | docs/quant-refactor/10-wp003-increment-a-sol-audit.md through 15-wp004-increment-c-sol-audit.md; opportunity outcome/miss/metric tests |
| REQ-OUT-003 | Ambiguous path honesty | PASS | strict canonical return classifier; mounted capture/label/reconciliation collections; final commit-bound 3,166-node gate |
| REQ-MISS-001 | First-class miss engine | PASS | docs/quant-refactor/10-wp003-increment-a-sol-audit.md through 15-wp004-increment-c-sol-audit.md; opportunity outcome/miss/metric tests |
| REQ-MISS-002 | Required miss taxonomy | PASS | docs/quant-refactor/10-wp003-increment-a-sol-audit.md through 15-wp004-increment-c-sol-audit.md; opportunity outcome/miss/metric tests |
| REQ-METRIC-001 | Discovery metrics | PASS | docs/quant-refactor/10-wp003-increment-a-sol-audit.md through 15-wp004-increment-c-sol-audit.md; opportunity outcome/miss/metric tests |
| REQ-BT-001 | Causal replay engine | PASS | strict canonical replay and return-truth suites; final commit-bound 3,166-node gate |
| REQ-BT-002 | Chronological partitions | PASS_SOFTWARE_ONLY | docs/quant-refactor/19-wp005-increment-c-sol-audit.md; docs/quant-refactor/21-wp006-sol-audit.md; synthetic proof only; empirical status remains unavailable |
| REQ-BT-003 | Trading metrics | PASS_SOFTWARE_ONLY | docs/quant-refactor/19-wp005-increment-c-sol-audit.md; docs/quant-refactor/21-wp006-sol-audit.md; synthetic proof only; empirical status remains unavailable |
| REQ-BT-004 | Execution stress | PASS_SOFTWARE_ONLY | docs/quant-refactor/19-wp005-increment-c-sol-audit.md; docs/quant-refactor/21-wp006-sol-audit.md; synthetic proof only; empirical status remains unavailable |
| REQ-BT-005 | Overfitting controls | PASS_SOFTWARE_ONLY | docs/quant-refactor/19-wp005-increment-c-sol-audit.md; docs/quant-refactor/21-wp006-sol-audit.md; synthetic proof only; empirical status remains unavailable |
| REQ-BT-006 | Survivorship disclosure | PASS_SOFTWARE_ONLY | docs/quant-refactor/19-wp005-increment-c-sol-audit.md; docs/quant-refactor/21-wp006-sol-audit.md; synthetic proof only; empirical status remains unavailable |
| REQ-ML-001 | Baseline comparison | PASS_SOFTWARE_ONLY | docs/quant-refactor/19-wp005-increment-c-sol-audit.md; docs/quant-refactor/21-wp006-sol-audit.md; synthetic proof only; empirical status remains unavailable |
| REQ-ML-002 | LLM non-authority | PASS | tests/test_network_safety.py; tests/test_opportunity_contracts.py; tests/test_opportunity_validation_robustness.py; import/broker/promotion firewalls |
| REQ-TRACE-001 | End-to-end decision trace | PASS | docs/quant-refactor/05-wp001-sol-audit.md through 12-wp003-increment-c-sol-audit.md; tests/test_opportunity_pipeline.py; tests/test_opportunity_contracts.py |
| REQ-OBS-001 | Structured observability | PASS | tests/test_final_repair_opportunity_mission.py; intraday_scanner/v2/opportunity/producer.py; intraday_scanner/v2/opportunity/catalyst.py |
| REQ-UI-001 | Best-opportunities projection | PASS | docs/quant-refactor/evidence/wp007-20260816/; tests/test_opportunity_projection.py; tests/test_opportunity_projection_public.py |
| REQ-UI-002 | Honest no-trade state | PASS | docs/quant-refactor/evidence/wp007-20260816/; tests/test_opportunity_projection.py; tests/test_opportunity_projection_public.py |
| REQ-PERF-001 | Incremental/cached computation | PASS | tests/test_final_repair_opportunity_mission.py; intraday_scanner/v2/opportunity/producer.py; intraday_scanner/v2/opportunity/catalyst.py |
| REQ-PERSIST-001 | Schema and migrations | PASS_SOFTWARE_ONLY | docs/quant-refactor/19-wp005-increment-c-sol-audit.md; docs/quant-refactor/21-wp006-sol-audit.md; synthetic proof only; empirical status remains unavailable |
| REQ-TEST-001 | Deterministic core coverage | PASS | accepted Sol audits 05-23 and mapped deterministic tests |
| REQ-TEST-002 | Repository gates | PASS | docs/quant-refactor/evidence/final-commit-gate-20260817/ |
| REQ-DOC-001 | Living documentation | PASS | docs/quant-refactor/25-final-repair-capsule.md; docs/quant-refactor/evidence/wp006-20260816/evidence-packet.md; docs/quant-refactor/26-final-repair-luna-handoff.md |

## Prior findings (14)

| Finding | Final adjudication | Exact evidence route |
| --- | --- | --- |
| FINDING-001 | PASS | WP001-WP002 accepted audits; opportunity contracts/pipeline tests |
| FINDING-002 | PASS | WP002 accepted audits; tests/test_opportunity_features.py |
| FINDING-003 | PASS | WP002 accepted audits; regime fixtures in opportunity tests |
| FINDING-004 | PASS | opportunity registry/lifecycle tests plus tests/test_final_repair_opportunity_mission.py |
| FINDING-005 | PASS | WP001 accepted audit; pair ranking and expectancy tests |
| FINDING-006 | PASS | WP001-WP002 accepted audits; quality-gate and risk tests |
| FINDING-007 | PASS | WP003 accepted audits; opportunity outcome persistence tests |
| FINDING-008 | PASS | WP004 accepted audits; miss and discovery metric tests |
| FINDING-009 | PASS | strict classifier plus mounted watcher/capture/reconciliation/label integration; final commit-bound 3,166-node gate |
| FINDING-010 | PASS_SOFTWARE_ONLY | WP005-C/WP006 accepted audits; real empirical data remains unavailable and non-promotional |
| FINDING-011 | EXTERNAL_DATA_BLOCKED | capability/cost stress contracts pass; consolidated historical entitlement remains external |
| FINDING-012 | PASS | DecisionTrace v2 tests plus structured producer telemetry tests |
| FINDING-013 | PASS | WP007 accepted audit and projection/public/render tests |
| FINDING-014 | PASS | final-repair exact-once CI shard evidence and static gates |

## Audit-finding seam

The original independent-audit implementation findings C-00, C-01, H-02,
H-03, H-05, H-06, M-01, and M-02 have repair evidence in
`docs/quant-refactor/27-final-repair-sol-escalation.md`,
`docs/quant-refactor/28-final-repair-post-automation-recovery.md`,
`docs/quant-refactor/30-post-commit-high-repair-luna-handoff.md`, and
`docs/quant-refactor/evidence/final-commit-gate-20260817/`. H-01 is closed by
portable checkout-to-HEAD blob equality at commit `fabca37f`; H-02/H-03 are
closed by the mounted disabled-by-default research CLI/service path; H-04 by
this exact matrix and the two Sol-owned ledgers; H-05 by whole-repo static and
Windows-safe tracked secret gates. C-00 is closed by candidate
non-interference plus exact attribution of live-state changes to the separate
five-minute research monitor. No critical or high implementation finding
remains open.
