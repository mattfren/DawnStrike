# Final independent audit evidence index

Verdict: `HASH_DRIFT`

## Controlling report

- `docs/quant-refactor/24-final-independent-audit.md`

## Identity and preservation

- `repository-state-before.json`
- `repository-state-after.json`
- `repository-state-invariance.json`
- `active-state-before.json`
- `active-state-after.json`
- `active-state-invariance.json`
- `candidate-analysis-v2.json`

## Accepted-evidence verification

- `accepted-evidence-rehash-v2.json`
- `accepted-source-rehash.json`

## Gate summary and raw evidence

- `gate-summary.json` indexes 19 recorded gate attempts with commands, UTC
  timing, exit codes, sizes, and SHA-256 identities for raw stdout/stderr.
- Every recorded gate has `<gate>.command.txt`, `<gate>.stdout.txt`,
  `<gate>.stderr.txt`, and `<gate>.exit.json` where produced by `run_gate.ps1`.
- `sbom.cdx.json` is the reproducible CycloneDX environment SBOM.

## Full pytest bounded-recovery evidence

- `full-pytest.process-midrun.json`
- `full-pytest.threshold-liveness.json`
- `full-pytest.bounded-termination.json`
- `full-pytest.process-after.json`
- `full-pytest.stdout.txt`
- `full-pytest.stderr.txt`
- `full-pytest.exit.json`
- `collect-only.stdout.txt`
- `full-pytest.partial-failure-map.json`
- `observed-failures.command.txt`
- `observed-failures.stdout.txt`
- `observed-failures.stderr.txt`
- `observed-failures.exit.json`

## Evidence seal

- `evidence-manifest.json` lists the report and every sealed non-cache evidence
  file with byte length and SHA-256.
- `evidence-manifest.sha256` is the detached manifest seal.

Generated Python bytecode was redirected to `pycache/` under this audit root;
it is incidental compileall output and is intentionally excluded from the
evidence manifest.
