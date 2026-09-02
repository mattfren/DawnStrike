# Dawnstrike v5 release — historical

This v5 cutover guide is archived. Its former direct task registration,
finalizer invocation, state migration, and restore commands are not valid for
the current production contract and must not be executed.

The current fixed boundaries remain `C:\r\dawnstrike-runtime` for the mounted
clean runtime and `C:\r\dawnstrike-state` for durable mutable state. The shared
checkout is not a production runtime. Use:

- `runtime_activation_and_rollback.md` for exact-SHA activation and rollback;
- `daily_finalize_runbook.md` for the guarded scheduled daily chain; and
- `public_dashboard_rollback.md` for an interrupted publication.

Historical v5 evidence remains evidence only. It does not authorize task,
database, runtime, or Vercel mutation.
