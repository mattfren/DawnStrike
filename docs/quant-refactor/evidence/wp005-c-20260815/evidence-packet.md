# WP005-C durable evidence packet

Terminal material state: `PASS_CANDIDATE_FOR_SOL_ADJUDICATION`

This packet binds Luna's final frozen-source verification. It is evidence for
Sol and is not self-acceptance, lifecycle promotion, profitability evidence,
or final certification.

## Identity and timing

- Worktree: `C:\r\dawnstrike-quant-refactor-20260811`
- Branch: `codex/sol-quant-refactor-20260811`
- HEAD: `bec32fe752b91f4e1357236a538a6dfea5da56bf`
- Start: `2026-08-15T21:11:54.0464032+00:00`
- End: `2026-08-15T23:53:12.2583351+00:00`
- Duration: `9678.212` seconds
- Implementation repair cycles: `2`
- Evidence orchestration attempts: `6`
- Final surviving gate processes: `0`

## Exact results

| Gate | Count | Exit | Duration seconds | Durable artifacts |
|---|---:|---:|---:|---|
| Focused WP005-C | 19 | 0 | 391.892 | `focused.command.txt`, `focused.log`, `focused.exit.json`, `focused.collection.*` |
| Accepted WP005-B main | 656 | 0 | 9025.675 | `main.command.txt`, `main.log`, `main.exit.json`, `main.collection.*` |
| Affected regression | 139 | 0 | 114.370 | `affected.command.txt`, `affected.log`, `affected.exit.json`, `affected.collection.*` |
| Ruff | n/a | 0 | 0.211 | `ruff.command.txt`, `ruff.log`, `ruff.exit.json` |
| mypy | 310 source files | 0 | 1.603 | `mypy.command.txt`, `mypy.log`, `mypy.exit.json` |
| compileall | n/a | 0 | 0.319 | `compileall.command.txt`, `compileall.log`, `compileall.exit.json` |
| diff-check | n/a | 0 | 0.147 | `diff-check.command.txt`, `diff-check.log`, `diff-check.exit.json` |
| Import firewall | 15 imports / 6 scanned | 0 | 4.293 | `import-firewall.command.txt`, `import-firewall.log`, `import-firewall.exit.json`, `import-firewall.py` |

Collection artifacts independently reconcile `19`, `656`, and `139`. Pytest
raw logs reach `[100%]` with only passing progress markers. The exact main and
affected command text was copied from the accepted WP005-B command files.

## Frozen hashes

Pre/post source inventories are byte-identical (`source_count=7`):

| Path | SHA-256 |
|---|---|
| `intraday_scanner/v2/opportunity/validation_robustness.py` | `10fc7fad9baa34259f1c6d8dc4aab771ed677bf45278cea04b67da2453fdf4b0` |
| `intraday_scanner/v2/opportunity/validation_robustness_contracts.py` | `bde69d782481a61e644a5fdd063c0393faf6076701a86c203f9c957486026c93` |
| `intraday_scanner/v2/opportunity/validation_robustness_controls.py` | `14a604b353c4f167cea1f360fdd1fd1f50dcdaf23322210787cea4efc10195b1` |
| `intraday_scanner/v2/opportunity/validation_robustness_math.py` | `8c353e1e9c443727d1028dd177f073ed6d8aec715e2610a4ad6fc22fc8d2ac63` |
| `intraday_scanner/v2/opportunity/validation_robustness_population.py` | `e38ff445781edc30c7252c44adc97b0ad160052d681bdf6e0d346fc2d4e46e57` |
| `intraday_scanner/v2/opportunity/validation_robustness_report.py` | `312afb18c152c897603d05b3755a28326e4058b877c6480e19bec86f28a3502e` |
| `tests/test_opportunity_validation_robustness.py` | `e3975c2b0487cbab03b395c143038139eb0a2ed3c36d3c996c29effa4ea274cd` |

See `source-hashes.pre.json`, `source-hashes.post.json`, and
`source-hash-verification.json` for machine-readable evidence.

## Boundaries and limitations

- Accepted WP005-A/B source, formulas, registry/pipeline/gate behavior,
  persistence, strategy lifecycle, runtime, UI, and operational integrations
  were not modified.
- Locked OOS is rejected as calibration provenance and was not tuned, repaired,
  or statistically consumed.
- The causal layer verifies exact-population, canonical-entrypoint,
  content-hashed recomputation artifacts but does not schedule those reruns.
  Empirical packets remain subject to independent source-body audit.
- Synthetic fixtures prove software invariants only. `NO_CONTROL_VETO` is
  explicitly not validation, approval, profitability, production eligibility,
  or promotion.
- Diff-check emitted only inherited line-ending warnings and exited `0`.
- Requirements remain open for Sol adjudication.
