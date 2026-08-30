# Durable state disaster recovery

Dawnstrike remains research-only and broker-disabled. The Morning and EOD
scripts create a durable backup after acquiring the daily lock and before
writing a heartbeat or any other state. A backup failure stops that run.

## Backup contract

Set `-BackupRoot` to a durable directory outside `StateRoot` (the default is
`C:\r\dawnstrike-state-backups`). The backup uses SQLite's online backup API,
not a raw copy of an open database. Each completed bundle contains only
`shadow_real.sqlite`, `manifest.json`, and `receipt.json`; no environment,
secret, WAL, or SHM files are copied. The manifest and receipt are
self-hashed, bind the authoritative online-backup bytes/hash, application
schema (`schema_version.version`), `quick_check`, release SHA, and UTC
creation time. The live main-file hash is retained only as an observational
WAL-aware field. All bundle files are written atomically.

Retention is bounded to known-good bundles. Invalid, partial, or tampered
bundles are never selected and the last known-good bundle is never deleted.
Do not manually delete a bundle until another bundle has passed verification.

## Verify and plan a restore (no write)

```powershell
py scripts\state_disaster_recovery.py restore-verify `
  --bundle C:\r\dawnstrike-state-backups\<backup-id> `
  --backup-root C:\r\dawnstrike-state-backups `
  --state-root C:\r\dawnstrike-state `
  --target-db C:\r\dawnstrike-state\shadow_real.sqlite

py scripts\state_disaster_recovery.py restore-plan `
  --bundle C:\r\dawnstrike-state-backups\<backup-id> `
  --backup-root C:\r\dawnstrike-state-backups `
  --state-root C:\r\dawnstrike-state `
  --target-db C:\r\dawnstrike-state\shadow_real.sqlite
```

Both commands fail closed on tamper, partial files, corruption, path
containment violations, or mismatched hashes. They never copy or overwrite a
database. Any actual restore requires a separately approved, manually
reviewed change with a fresh pre-change backup; this tooling makes no history,
performance, or live-readiness claim.
