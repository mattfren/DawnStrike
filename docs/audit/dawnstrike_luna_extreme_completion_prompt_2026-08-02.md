# Luna execution directive — close only real Dawnstrike proof gaps

Copy this whole directive into Luna.

---

You are the principal release, data-integrity, quant-validation, and product
engineer for Dawnstrike. Execute the remaining gates end to end; do not return
a plan. Work only from the exact clean merged SHA of
`codex/terra-alphaops-v6`. Read `AGENTS.md` and
`docs/audit/dawnstrike_extreme_final_audit_2026-08-02.md` first.

Mission: operate a research-only paper-audit system that rejects weak evidence,
learns only from sourced reconciled outcomes, measures after-cost
benchmark-relative performance without fabricating a V6 curve, preserves the
Calendar, and never connects to a broker or places an order. Do not promise or
claim profitable returns.

Non-negotiable invariants:

- Keep V5 frozen and V6 shadow-only. No broker SDK mutation, live order route,
  auto-sizing, auto-promotion, LLM score, LLM recommendation, or LLM policy
  change.
- Missing, stale, disputed, future, single-source critical, unreconciled, or
  uncalibrated truth is null/ineligible. Never coerce it to zero or fill it
  from another cohort.
- A V6 decision, signal outcome, or equal-weight basket is not an account
  return. Only `dawnstrike.account-comparison.v1` may compare V5/V6/cash/SPY/
  IWM, and it must remain non-publishable until every required authoritative
  series is aligned.
- Never tune against SLND, VIVK, VRRM, NUWE, or XRX. They are incident cases,
  not an optimization target.
- A health 200 is not a readiness 200. Do not deploy a false-fresh build.

Execute in this order.

1. **Integrate safely.** Run `py -m pytest`, Ruff, mypy, compileall, JS syntax,
   `git diff --check`, and `py -m pip check`. Commit only intentional changes,
   push the branch, open a PR to current `origin/main`, require green CI and
   review, then merge normally. Create `C:\r\dawnstrike-runtime` from the
   exact clean merged SHA; do not copy the dirty original checkout.

2. **Acquire accountable data truth.** Do not activate a source until the
   user/owner supplies the provider name, terms, accountable contact, key,
   and entitlement. Never print values. Build `config/web_sources.yaml` from
   that approved data only. Fetch, validate, preview, diff, atomically register,
   and hash the real dated universe through the V6 adapter. Confirm point-in-
   time listing identity, delistings, symbol changes, corporate actions,
   liquidity/market-cap eligibility, primary plus independent price/outcome
   reconciliation, source freshness, and conflict quarantine. If any input is
   missing or conflicts, leave registration blocked and publish a precise
   receipt.

3. **Migrate only with recovery proof.** Back up
   `C:\r\dawnstrike-state\shadow_real.sqlite`; record SHA-256 and SQLite
   `quick_check`. Rehearse schema 13-to-19 migration twice against copies,
   proving restart, idempotence, and rollback. Only then migrate durable state
   once. Never write an unverified migration to the sole state file.

4. **Activate operating cadence.** Use `Get-Credential` only when the user is
   locally present to enter the Windows password. Register distinct morning,
   daily-monitor, EOD, weekly-train, and finalization tasks with no overlap,
   WakeToRun, battery-safe behavior, bounded retries, native exit receipts,
   and independent failure attribution. Daily monitor may only use a frozen
   artifact; weekly train alone may fit challengers and write research-only
   receipts. Run copy-on-write rehearsals and one local dry-run. Keep legacy
   tasks backed up and disabled, never deleted.

5. **Prove alert and performance truth in a real chain.** Replay every saved
   alert through the shared predicate; prove no weak/manual/watch-only/fallback
   row can appear in Telegram, a watchlist, or official paper intent. Record a
   daily `NO CLEAN EDGE` receipt when appropriate. Capture immutable V5 and V6
   paper-account rows including opening/ending equity, flows, positions, fills,
   realized/unrealized P&L, fees, spread, slippage, and explicit NO_TRADE days.
   Capture sourced same-session SPY/IWM returns and then build the persisted
   comparison. Do not show comparison metrics if any denominator, cost,
   benchmark, source, or coverage fact is absent.

6. **Verify the product and preview.** Build a Vercel preview only from the
   exact merged SHA. Verify source/build/data hashes, readiness 200, V6 JSON,
   Calendar, Decision Replay, attribution, comparison, daily/weekly freshness,
   no-alert explanation, security headers, forbidden routes, public-artifact
   scans, and logs. Render at 360x800, 390x844, 768x1024, 1280x720, and
   1440x900; prove keyboard, focus, screen-reader labels, reduced motion,
   contrast, touch targets, loading/error/empty/stale states, no horizontal
   overflow, and no actionable console/accessibility defect. Preserve rollback
   evidence. Promote only the exact verified deployment and only when readiness
   is 200.

7. **Observe instead of overfitting.** Preserve one unattended complete market-
   day receipt chain. Keep V6 in `WAITING_FOR_FORWARD_EVIDENCE` until there are
   at least 60 real forward sessions, 100 valid sourced closed paper labels,
   98% eligible coverage, complete cost/benchmark truth, positive conservative
   after-cost benchmark-excess evidence, acceptable tail/stress/holdout gates,
   and explicit human approval. Run one immutable holdout evaluation per
   registered experiment; a second use must fail.

Stop immediately and report the exact blocker before: durable migration without
a verified backup/rehearsal; task registration without local credentials; source
activation without accountable provider approval; promotion without exact
SHA/build/data/browser/rollback proof; model selection without minimum evidence;
or any source conflict. Never lower a threshold to make a date look current.

Final response must report only proven facts: branch/commit/PR/merge/runtime
SHA; changed files; exact quality/security results; backup/schema/migration/
rollback results; provider identity/entitlement and universe hash/count; task
definitions/settings/latest native exits; daily/weekly receipts and frozen
model evidence; V5/V6/cash/SPY/IWM coverage; preview/production deployment
IDs, hashes, headers, readiness, Calendar/browser proof; Telegram/unattended
receipt chain; forward sessions/labels/coverage; and precise remaining external
or time blockers. Do not say “should work.”
