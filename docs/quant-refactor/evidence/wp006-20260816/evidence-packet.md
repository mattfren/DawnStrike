# WP006 canonical evidence index

Status: `PASS_CANDIDATE_FOR_SOL_ADJUDICATION` (historical Luna result)

This index does not alter or reseal WP006 evidence. It names the canonical
artifacts that existed when Sol accepted WP006 in
`docs/quant-refactor/21-wp006-sol-audit.md`.

- Terminal summary: `run-summary.json`
- Frozen source identity: `source-hashes.json`
- Modification inventory: `modification-inventory.json`
- Evidence manifest: `evidence-manifest.json`
- Detached manifest seal:
  `6d519bd28f467dcf87952b2c9558f6b7180e48529c884d99f175cde398d9c511`
- Active-state proof: `active-state-before.json`, `active-state-after.json`,
  `active-state-invariance.json`, `active-state-readonly.stdout.txt`
- Focused persistence: `focused.command.txt`, `focused.stdout.txt`,
  `focused.exit.json`
- Robustness: `robustness.command.txt`, `robustness.stdout.txt`,
  `robustness.exit.json`
- Accepted main and affected regressions: `main.*`, `affected.*`
- Static and boundary gates: `ruff.*`, `mypy.*`, `compileall.*`,
  `diff-check.*`, `import-firewall.*`, and `schema.*`

Recorded result: persistence `15/15`, robustness `19/19`, accepted main
`656/656`, affected `139/139`, all exit `0`. Synthetic fixtures established
software invariants only. No real holdout, empirical edge, promotion, broker,
active-state migration/write, deployment, or Git publication occurred.

## Supersession lineage

WP007 later appended to `docs/quant-refactor/04-execution-log.md`. Therefore the
WP006 source inventory's historical hash and length for that append-only file
are intentionally superseded by WP007's accepted current source inventory.
The mismatch does not modify or invalidate the sealed WP006 manifest.
