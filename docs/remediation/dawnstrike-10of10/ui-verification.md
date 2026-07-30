# UI verification

Status: `PRODUCTION_VERIFIED`

Authoritative production artifact:

- source SHA: `51f79ff2a738110b486111d85c4d93cfda9f4ec8`;
- build ID: `5ef6a274f37fd1dbae87`;
- data hash:
  `3a134cc971e69a6f01a50c63687f9440883e82e833afa09bd120d319270cd56d`;
- deployment: `dpl_AbTsQJzj1EvMQ5Bd51naboU1BMv6`;
- public URL:
  `https://dawnstrike-command-center-x3.vercel.app`.

The framework-free public product has four primary sections: Overview,
Performance, Research, and System. Browser calculations do not own or
recalculate return truth.

## Rendered proof

The clean local artifact was checked at 360x800, 390x844, 768x1024,
1280x720, and 1440x900. Every target had:

- zero page, header, and primary-navigation horizontal overflow;
- all ten required metric cards present;
- no clipped navigation controls;
- native table-container scrolling where a mobile table is wider than its
  viewport.

At 1280x720 the bottom of the final KPI row was pixel 607, so latest date,
daily and cumulative return, benchmark, excess return, P&L, drawdown, open
positions, coverage, and system state all appeared in the first desktop
viewport.

The same final artifact was then checked on production at 1280x720 and
360x800. Production had zero page/header/nav overflow and loaded market date
`2026-07-29`.

## Interaction and semantics

- Overview, Performance, Research, and System each activated the intended
  visible panel.
- Navigation exposes `aria-controls` and current `aria-pressed` state.
- Performance and Research render ten detail rows at a time.
- Pagination advanced Performance from `Showing 1-10 of 223` to
  `Showing 11-20 of 223`; Research reported `Showing 1-10 of 234`.
- Pagination buttons have text labels, disabled boundary states, and visible
  keyboard focus.
- Missing returns render `Not reported`; they never render as zero.
- The malformed missing-value chart tooltip found during production QA was
  removed. The final DOM contained zero tooltip attributes with embedded HTML.
- Semantic headings, table captions, scoped column headers, skip navigation,
  status regions, and color-independent status text are present.
- Browser warning/error logs were empty on the final production desktop and
  mobile checks.

The preceding corrected artifact also passed axe-core 4.12.1 with 0
violations, 0 incomplete checks, and 37 passes. The final UI delta added
labeled pagination controls and explicit navigation state; current semantic
tests and rendered interaction checks pass.

## Truth shown in the UI

Production is intentionally degraded, not broken. It visibly reports:

- official after-cost return: `Not reported`;
- benchmark and excess return: `Not reported`;
- readiness: `Not ready`, HTTP 503;
- 28 unresolved outcomes excluded;
- research-only and no broker connection;
- unknown source-quality, halt, corporate-action, and liquidity evidence as
  unknown, not inferred.
