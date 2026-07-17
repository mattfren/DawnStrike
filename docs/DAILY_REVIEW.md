# Dawnstrike Daily Review

## Current Status

The former `daily-review`, `daily-review-learn`, and
`daily-review-telegram` CLI stages are not implemented and are not part of the
scheduled EOD path. Do not use old runbooks that reference them. They were thin
aspirational names without an authoritative exact-cohort outcome contract.

The supported workflows are now separated by purpose:

- AlphaOps intraday picks: run `scripts\run_alphaops_eod_full.bat YYYY-MM-DD`.
  It reconciles only the exact proven delivered Telegram cohort against a
  complete sourced one-minute RTH artifact. Missing truth returns exit `2` and
  remains `N/A`.
- Daily mover/pattern research: configure
  `config\mover_daily_workflow.json` from the checked-in example and use
  `scripts\mover-pattern-lab\run_operator.ps1`. See
  `docs\operations\mover_pattern_daily_workflow.md`.
- Manual outcome imports remain available for historical audit, but production
  AlphaOps learning does not consume them.

Neither workflow places orders or calls a broker. A mover that was not present
in a source-complete retained universe cannot be labeled as a model miss, and
an absent outcome can never be converted to a zero return.
