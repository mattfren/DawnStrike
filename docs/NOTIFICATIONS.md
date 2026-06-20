# Notifications

Dawnstrike sends research/watchlist alerts only. It never places orders.

## Channels

Set `INTRADAY_NOTIFIER_CHANNELS` to a comma-separated list:

- `console`
- `email`
- `discord`
- `telegram`

`console` works without credentials and is the default.

## Events

The notifier service can emit:

- New top explosive pick
- Score above `INTRADAY_ALERT_SCORE_THRESHOLD`
- Halt/offering risk warning
- Paper audit summary

Each sent event is recorded in `notifications_sent` with a unique event key so the same scan/ticker/channel is not sent twice.

## Commands

```powershell
intraday-scan notify --db-path data/scanner.sqlite --dry-run
intraday-scan notify --db-path data/scanner.sqlite
intraday-scan notify --audit-summary outputs/latest_audit/paper_audit_summary.json --dry-run
```

## Credentials

Copy `.env.example` to `.env` and fill only the channels you use. Missing channel credentials fail with a clear error. Secrets are not included in persisted config JSON.
