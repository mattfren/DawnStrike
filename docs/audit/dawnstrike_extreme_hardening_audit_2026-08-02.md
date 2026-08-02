# Dawnstrike extreme hardening audit — 2026-08-02

## Verdict

Dawnstrike is a research-only, paper-audit system with a substantially safer
forward path than the legacy record. It is **not** an operationally ready
source of return claims, and it cannot honestly be described as having an
improved strategy yet.

The decisive problem was selection quality, not a UI calculation: the five
source-complete, historically triggered observations all lost by the close.
Their mean gross close return was **-11.5078%**. This is a sourced signal-outcome
cohort, before costs, not an account return or live-trading result.

## Evidence collected now

| Surface | Current evidence | Verdict |
| --- | --- | --- |
| Terra worktree | `codex/terra-alphaops-v6`; committed replay-control baseline `97d7a3d8d02bfb0fff9b736b91d6a81b58153951`, plus the uncommitted security hardening described below | Isolated candidate exists; not deployed. |
| Runtime and public site | Runtime and production health both serve `692e785cf8304a8045e88ab221dc644d4eb2e9e7`; public build is `f42ac827fe55324ae491` | Hardened candidate is not in runtime or production. |
| Production readiness | `/api/health` is HTTP 200, `/api/readiness` is HTTP 503 with `safety_evidence_unverified`, `snapshot_not_publishable`, and `pipeline_not_ready` | Honest degraded publication; do not promote. |
| Durable DB | `shadow_real.sqlite` SHA-256 `5B65C7DC823806F1570015F6F4922E4B1FA73D2B2A84BE2097A14F611D3FFF3F`; `PRAGMA quick_check=ok`; schema version 13; 10 outcomes, 254 Alpha signals, 109 notifications, 16 fills | Intact legacy state, but no current V6/source/benchmark truth. |
| Copy-on-write migration | Protected backup at `C:\r\dawnstrike-state\migration-backups\phase0-20260802T040839Z\shadow_real.sqlite`; rehearsal initialized twice and stayed hash-stable with 101 tables and `quick_check=ok` | Candidate migration is additive/idempotent on a copy; live DB was not migrated. |
| Historical outcome cohort | 5 `complete_sourced` rows and 5 `not_triggered` rows. Triggered close returns: `-1.3438%`, `-20.2778%`, `-10.8696%`, `-24.8305%`, `-0.2173%` | No account-level return may be inferred. |
| New alert replay control | `alpha-alert-replay` reads SQLite in read-only mode and writes a machine-readable report. Live artifact: `C:\r\dawnstrike-state\repro\phase0-20260802-alpha-alert-replay.json`, SHA-256 `D9BBAF3BAFDC6C1AE20F27B8B6DAA9FB386380E4BDBFE5BB08D12937A6774849` | 66 legacy `can_alert` records disagree with current policy; all 5 sourced close losses would be blocked using decision inputs only. |
| Active forward alert path | Alpha cycle gates before persistence; review selects only `PASS`/`ALERT_OK`; legacy Web Telegram candidates require the same strict fields; V5 watcher requires exact selected membership and independently rejects watch-only rows | Forward routes fail closed in source and regression tests. |
| Scheduler | Morning, monitor, EOD, and daily-finalize tasks point to the intended runtime/state roots, but all use `Interactive` logon and unsafe battery settings. Last results are `1`, `1`, `1`, and `0`, respectively. | Not unattended. Re-registration requires the approved password-logon identity. |
| Source truth | No `config/web_sources.yaml` exists in the runtime or candidate. Only contract templates exist. Alpaca credentials pass presence validation, but that is not an entitlement or historical-data validation. No dated V6 universe is registered. | Source collection and return labeling remain blocked. |
| Security | Raw Bandit scan after this hardening reports **0 findings**. `network_safety` validates schemes, credentials, ports, hosts, and redirects; `sql_safety` validates identifiers and ORDER BY grammar before composition. | Local static surface is hardened; preserve tests and do not expand the baseline. |

## What was hardened in this pass

- Added `intraday_scanner/services/alpha_alert_replay_service.py` and the
  `alpha-alert-replay` CLI command. It replays each stored alert from the
  recorded decision inputs before joining outcomes, uses a read-only SQLite URI,
  hashes the replay inputs, and exits nonzero if any sourced close loss remains
  alertable.
- Added regression coverage proving that five legacy `can_alert` losses are
  blocked, changing only a future close outcome cannot change a replayed
  decision, and the output artifact is machine-readable.
- Reconfirmed forward notifier behavior: the legacy Web Telegram route
  quarantines ungated rows, and the paper watcher rejects missing, partial, or
  watch-only selection evidence.
- Created a fresh, checksummed durable-state backup and performed an
  idempotent copy-on-write migration rehearsal. Neither operation changed the
  live durable DB.
- Added one redirect-aware, approved-host HTTP transport and moved all prior
  URL-open callers (providers, web collection, notification scripts, and URL
  ingestion) behind it. HTTPS is the default; legacy web collection must name
  allowed hosts explicitly to permit HTTP.
- Added centralized SQLite identifier and ORDER BY validation. Dynamic values
  remain bound parameters; hostile identifiers and ordering expressions are
  regression-tested and rejected before SQLite executes them.
- Repaired the PaperOps shadow lifecycle test so its registration timestamp is
  deterministic and still proves the required next-session activation rule.

## Why returns were bad

1. The sampled historical record is tiny: only five triggered, sourced close
   observations. It is not statistically adequate for strategy claims.
2. Each triggered loss had a legacy alert truth mismatch. At decision time the
   stored record either had `NEEDS_CONFIRMATION` or weak/unknown inputs while
   still carrying `can_alert=1`.
3. The hardened replay blocks the losses for evidence quality, source
   confidence, missing verified safety data, weak catalyst evidence, low edge,
   insufficient calibration, poor setup grade, and in several cases gap/stop
   policy violations. This is a filter diagnosis, not proof that a new model
   will be profitable.
4. There is no qualified after-cost paper-account cohort, benchmark series, or
   forward V6 outcome set. A dashboard percentage would be misleading.

## Remaining work, in priority order

1. **Do not deploy or claim performance.** Keep `/api/readiness` degraded until
   source, run, and public-artifact gates are objectively complete.
2. **Make source truth real.** Obtain an approved primary and independent
   price/outcome source, accountable user-agent contact, secure configuration,
   and terms-compliant point-in-time universe source. Build/validate the dated
   universe artifact, preview it, and register it by exact preview hash.
3. **Cut over safely.** After all local gates pass, copy the exact candidate SHA
   to `C:\r\dawnstrike-runtime`; make another protected backup; migrate only
   after a fresh copy-on-write rehearsal; re-register tasks with the supplied
   password-logon identity; run the scheduler doctor and a dated dry-run chain.
4. **Prove the product.** Build a clean Vercel preview bound to the exact
   source/data/build hashes; verify Calendar, Research, System, headers,
   artifact scan, health, readiness, and rollback; only then request a
   production promotion.
5. **Learn prospectively.** Keep V6 daily work label-only and refits weekly.
   Run one immutable, tagged experiment at a time. Do not claim improved returns
   before 60 forward sessions, 100 closed after-cost labels, complete
   source/benchmark coverage, positive purged OOF, calibration/interval proof,
   one untouched holdout, acceptable drawdown/concentration, and a manual
   approval record.

## Exact external gates

- Approved provider names, terms/entitlements, and secure key locations for a
  primary plus independent quote/outcome source.
- An accountable contact email for the production user agent.
- Approval of a point-in-time small-cap universe source and its licensing.
- A Windows password-logon identity with access to the network, encrypted
  secrets, `C:\r\dawnstrike-state`, Telegram, and Vercel credentials. The
  password must be supplied interactively to the Windows credential prompt,
  never committed or sent in chat.
- Time and sourced forward outcomes. These cannot be accelerated or fabricated.

## Verification completed for this change

```text
py -m pytest tests/test_alpha_alert_replay_service.py tests/test_alpha_v5_policy.py tests/test_alpha_selection_delivery_identity.py -q
24 passed

py -m ruff check intraday_scanner/services/alpha_alert_replay_service.py intraday_scanner/cli.py tests/test_alpha_alert_replay_service.py
All checks passed

py -m pytest tests/test_network_safety.py tests/test_sql_safety.py tests/test_sec_provider.py tests/test_e2e_automation.py tests/test_web_autopilot.py tests/test_yahoo_chart_fetcher.py tests/test_paper_ops_shadow_runner.py -q
83 passed

py -m pytest -q
Captured full-suite log reached [100%] with no FAILED, ERROR, or Traceback marker

py -m ruff check .
All checks passed

py -m mypy intraday_scanner
Success: no issues found in 208 source files

py -m bandit -r intraday_scanner scripts -ll -f json -o build\bandit-after-network.json
0 findings
```
