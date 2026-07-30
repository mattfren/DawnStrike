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

The current real-data artifact is degraded because its source evidence is
incomplete; the UI exposes that state rather than showing a prior return as
today's result. The previously recorded axe pass remains part of the local
proof history; the current pass specifically rechecked rendering, navigation,
overflow, console, page errors, and failed requests.
