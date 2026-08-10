# CYCLE-002 remediation claim — awaiting SOL audit

This claim remediates default writable SQLite construction on observer paths. The implementation uses fail-closed read-only stores for default observers, preserves explicit writer modes, closes opened connections on read-only setup failures, and supplies an exhaustive parser-name registry plus explicit non-keyword observer guard.

The schema-26 disposable-fixture matrix proves whole-file SHA, every user-table canonical hash, selected table hashes, and table count are unchanged after actual observer functions/handlers. It includes scenario, alpha, outcome-gap, historical/calendar/dashboard, probability/dashboard doctor, daily status, receipt verification, dashboard loading, and the four previously missed optional-persist observer paths.

The former missing-database auto-create expectations were updated to the required fail-closed `StorageError` contract, and the public-build writer fixture now initializes its disposable database before observing scenario evidence. The schema-26 matrix now invokes alpha attribution, return attribution without persistence, and alert replay directly. Return-attribution notification without persistence fails before side effects. Static gates and focused tests pass; a fresh unfiltered full-suite and active-state post-run fingerprint remain required before SOL can accept this claim.
