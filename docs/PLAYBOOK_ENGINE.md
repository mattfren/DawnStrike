# Playbook Engine

The playbook engine turns a scored research candidate into an operator-readable
plan. It is not an order ticket.

Each AlphaOps alert includes:

- trigger
- confirmation condition
- invalidation level
- do-not-chase rule
- 5-minute monitor instruction
- outcome fields to collect

Hard risk gates block alerts for current halt, active dilution/offering, invalid
ticker, missing price or volume, sub-min price, extreme spread, stale source,
source conflict, or missing source confidence.

Soft penalties reduce score for unknown float, missing previous close/high/low,
missing catalyst, public URL-only data, low source count, and mega gaps.

Monitor labels are:

- `BREAKOUT WATCH`
- `CAUTION`
- `INVALIDATED`
- `THESIS BROKEN`
- `MANUAL REVIEW`

When no live/current price source exists, AlphaOps sends one compact manual
monitor notification and relies on notification dedupe to avoid spam.
