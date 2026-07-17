# Mover Pattern Lab Daily Workflow

## Outcome

The scheduled workflow closes the operational gap between frozen mover
hypotheses and daily paper evidence. On every configured cutoff it consumes
genuine retained CSV inputs, obtains an authoritative process receipt inside the
cutoff-to-cutoff-plus-five-minute window, builds point-in-time snapshots, and
runs the frozen paper scan. After close it reconciles every scheduled cutoff,
updates cumulative core analysis and the clickable calendar, verifies retained
evidence, and sends a durable Telegram message.

This is research and simulated paper observation only. It has no broker client,
order route, position mutation, or live-execution switch.

## Configure genuine inputs

Copy the example and edit the copy; do not commit credentials:

```powershell
Copy-Item config\mover_daily_workflow.example.json config\mover_daily_workflow.json
```

The retained CSV adapter resolves three path templates relative to the config
file:

- `bars_csv_template`: bar-close OHLCV available at one exact cutoff;
- `context_csv_template`: point-in-time universe, spread, halt, split,
  offering, source-conflict, and catalyst facts for that cutoff; and
- `reconciliation_bars_csv_template`: the complete official session used only
  after close.

Templates may use `{market_date}`, `{cutoff_et}`, and `{cutoff_token}`. For
example, the 09:45 task on 2026-07-20 resolves `bars_{cutoff_token}.csv` to
`bars_0945.csv`.

The scan file must contain no current-session bar after the declared cutoff.
Every timestamp must be timezone-aware and represent bar close. Context facts
must be observed no later than the cutoff and within the core freshness limit.
Missing truth remains missing and suppresses the signal.

Forward candidate selection also requires an immutable universe JSON artifact
referenced by `universe_source_ref` and `source_refs` as
`sha256:<canonical-json-sha256>:<absolute-path>`. Its object contract is:

```json
{
  "schema_version": "v2.mover_candidate_universe.v1",
  "market_date": "2026-07-20",
  "feature_cutoff_at": "2026-07-20T09:45:00-04:00",
  "system_received_at": "2026-07-20T08:30:00-04:00",
  "evidence_mode": "forward_observation",
  "universe_selection_method": "scheduled_universe",
  "expected_symbols": ["ABC"],
  "expected_symbols_complete": true,
  "research_only": true,
  "broker_execution_enabled": false
}
```

`expected_symbols` must be complete, uppercase, sorted, and unique. Its receipt
must be no later than the feature cutoff. The hash is SHA-256 of compact JSON
with keys sorted, matching the core content-addressed contract. A post-close
winner list is never an eligible universe.

Real Telegram delivery uses the existing `.env` names:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

The workflow config stores no secret. For a local delivery check, set
`notification_channel` to `console`; do not relabel console evidence as a
Telegram delivery.

## Run one stage manually

The same noninteractive command used by Task Scheduler can be invoked directly:

```powershell
# Run immediately after the 09:45 bar file is durably written.
powershell -ExecutionPolicy Bypass -File `
  scripts\mover-pattern-lab\run_operator.ps1 `
  -Stage Scan `
  -Cutoff 09:45 `
  -Config config\mover_daily_workflow.json

# Run only after the published close plus the configured post-close lag.
powershell -ExecutionPolicy Bypass -File `
  scripts\mover-pattern-lab\run_operator.ps1 `
  -Stage Reconcile `
  -Config config\mover_daily_workflow.json
```

The scan stage accepts no operator-supplied receipt timestamp. Core reads the
actual process clock and creates a content-addressed forward receipt. A late,
early, wrong-date, future-contaminated, incomplete, or unverified input fails
closed and generates a no-data notification.

Reconciliation resolves the session through the published exchange calendar.
`reconcile_not_before_et` defines the normal-session post-close lag: for
example, `16:10` means ten minutes after the published close, so a 13:00 early
close may reconcile at 13:10. The operator rejects every bar later than its
internal system receipt and requires a bar at the exact published close. It
retains that decision, the input hash, the calendar decision, and receipt time
in an immutable content-addressed reconciliation source receipt before it
consumes scan state.

## Register the schedule safely

Preview first:

```powershell
powershell -ExecutionPolicy Bypass -File `
  scripts\mover-pattern-lab\register_daily_workflow.ps1 `
  -Config config\mover_daily_workflow.json
```

The preview converts every ET cutoff to the current Windows host timezone and
shows the exact tasks. It makes no system change. Register only after the
preview and upstream file-delivery timing are correct:

```powershell
powershell -ExecutionPolicy Bypass -File `
  scripts\mover-pattern-lab\register_daily_workflow.ps1 `
  -Config config\mover_daily_workflow.json `
  -Apply
```

The registrar creates weekday tasks, ignores overlapping duplicate instances,
and limits a run to 30 minutes. Cutoff tasks are not backfilled when missed,
because a late process cannot recreate point-in-time evidence. They default to
one minute after each feature cutoff so the completed bar can be durably present
while remaining inside core's five-minute receipt window. Reconciliation is
registered as bounded 30-minute probes from the published early-close clock
plus the configured lag through the regular-session clock plus one final retry.
With `reconcile_not_before_et=16:10`, probes run from 13:10 through 16:40 ET.
Pre-close probes return `not_applicable_yet` without sending a notification;
the first eligible probe runs, and later probes are idempotently suppressed.
On `-Apply`, the registrar removes the exact legacy single-reconciliation task
for the configured task prefix before installing the bounded probes, preventing
the old and new schedules from launching the same reconciliation concurrently.
The published-calendar service remains the fail-closed timing authority and
explicitly reports any missing cutoff state.

Task registration does not create or fetch market data. The upstream feed must
write each configured input atomically before the corresponding task begins.
Exchange holidays still fail closed through the published market calendar.

## Daily evidence and recovery

Operator state is written under:

```text
<output_root>/operator/runs/YYYY-MM-DD/scan_HHMM.json
<output_root>/operator/runs/YYYY-MM-DD/reconcile.json
```

Each mutable state file points to an immutable content-addressed operator
receipt under `<output_root>/operator/receipts/sha256`. Before reconciliation or
redelivery, the service verifies the state schema, market date, cutoff, config
fingerprint, receipt hash, and every referenced manifest/artifact path and hash.
Mutable state is never an authority for signals or results.

Every signal and reconciled paper outcome has its own explicit delivery
membership and outbox record under `<output_root>/operator/notifications/outbox`.
The attempt is durable before the transport call. A Telegram acknowledgement
persists the exact transmitted UTF-8 text, byte count, byte hash, provider
response, and `message_id`. Duplicate suppression is reported as
`duplicate_suppressed`, never as a new delivery. A timeout or exception after a
durable attempt is `delivery_unknown`; it is not automatically retried because
the first request may have reached Telegram.

Workflow truth and notification truth are orthogonal. A valid scan remains
`workflow_status=passed` when Telegram is unknown or failed, so after-close
reconciliation still consumes its immutable receipt. The mutable state records
the separate `notification_status`.

Reconciliation requires successful immutable workflow evidence for every
configured cutoff. It calls
the core reconciliation and analysis APIs for each retained scan manifest. Core
then aggregates all compatible retained daily pairs; the latest analysis and
calendar paths are recorded in `reconcile.json`.

`incomplete_pending` is deliberately nonterminal and exits nonzero. A later run
with a new complete retained bars receipt reconciles again while preserving all
prior operator receipt refs. Once complete, the terminal result is immutable;
an identical rerun suppresses the already acknowledged final scorecard.

Interpret messages literally:

- `source-validated forward paper signal` means frozen gates passed; entry remains a
  simulated next eligible bar and no order was placed;
- `passed_no_signal` means valid snapshots were evaluated but no frozen setup
  cleared; and
- `blocked` means required market data, timing, lineage, or verification was
  missing. It is not a 0% return;
- `incomplete_pending` means at least one signal still lacks a complete outcome
  and the stage must run again with newly retained source evidence; and
- `not_applicable_yet` is a quiet scheduler probe before the authoritative
  session-close gate; it consumes no market evidence and sends no message; and
- `delivery_unknown` is notification truth only and never relabels a successful
  market-evidence workflow as blocked.

Pending and incomplete outcomes retain return `null`. Only source-complete,
after-cost closed forward observations enter forward performance. Historical
replay stays separate and cannot promote a strategy.

## External readiness blockers

Code cannot manufacture a real feed, verified corporate-action context, or
Telegram credentials. Production operation therefore remains blocked until:

1. an upstream read-only data process writes the cutoff and after-close CSVs at
   the configured paths;
2. the candidate-universe and context artifacts contain genuine, timely,
   content-addressed lineage; and
3. `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are available to the scheduled
   task account.

Do not replace any missing item with fixture data or a permissive boolean.
