# Final repair post-automation recovery evidence index

- Terminal event: `REPAIR_REQUIRED`
- Combined payload: `recovery-combined-result.payload.json`
- Final combined result: `recovery-combined-result.json`
- Evidence manifest: `recovery-evidence-manifest.json`
- Fresh shard 05: `shard-05.command.txt`, `shard-05.stdout.txt`, `shard-05.stderr.txt`, `shard-05.exit.json`, `shard-05.manifest.json`
- Failure: `tests/test_opportunity_discovery_metrics.py::test_multi_session_recall_is_exact_micro_aggregate[1-0.333333333333]`
- Shards 06-07: not run after the terminal shard-05 failure
- Shard validation: `recovery-shard-validation.stdout.txt`
- Active-state before/after: `recovery-active-state-before.stdout.txt`, `recovery-active-state-after.stdout.txt`
- Source freeze before/after: `recovery-source-freeze-before.stdout.txt`, `recovery-source-freeze-after.stdout.txt`
- Inventory before/after: `recovery-inventory-before.stdout.txt`, `recovery-inventory-after.stdout.txt`
- Process audit before/after: `recovery-process-audit-before.stdout.txt`, `recovery-process-audit-after.stdout.txt`
