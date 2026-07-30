# UI verification

The framework-free static product has four primary sections: Overview,
Performance, Research, and System. It renders the canonical snapshot and does
not recalculate returns in browser code.

Current local Playwright proof recorded in `evidence/`:

- 360×800, 390×844, 768×1024, 1280×720, and 1440×900 have no horizontal
  overflow;
- semantic navigation activates each view;
- axe reports 0 violations;
- console and page-error channels are empty;
- no failed network requests were observed;
- Overview, Performance, Research, and System navigation buttons each activate
  the matching visible panel;
- missing after-cost or benchmark values render as `Not reported` rather than
  zero;
- return context shows cohort, period, denominator, cost treatment, coverage,
  and as-of time;
- source quality, halt, corporate-action, and liquidity evidence are explicit
  `Unknown — not reported` states when no source evidence exists;
- research and official paper cohorts remain visibly separate.

Fresh current-artifact browser proof on 2026-07-30 used the corrected
633,502-byte snapshot:

- `evidence/ui-current-overview.png` and
  `evidence/ui-current-mobile.png` capture the refreshed Overview surface;
- the page had meaningful content and no framework error overlay;
- console and page-error channels were empty;
- Overview, Performance, Research, and System navigation each activated the
  expected panel;
- 360×800 had no horizontal overflow;
- axe 4.12.1 reported 0 violations, 0 incomplete checks, and 37 passes.

The current real-data artifact is degraded because its source evidence is
incomplete; the UI exposes that state rather than showing a prior return as
today's result. The latest current-artifact pass rechecked rendering,
navigation, overflow, the safety panel, console errors, and the hosted
Deployment Protection limitation. The hosted preview's API and static-manifest
proof are recorded separately in `deployment-verification.md`.
