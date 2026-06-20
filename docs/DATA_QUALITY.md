# Data Quality

Dawnstrike separates scoring mechanics from data quality. A high score from a
manual/free row only means the row scored well under the formula; it does not
mean the data is complete or provider-validated.

## Labels

- `sample` or `fixture`: bundled files for software testing.
- `manual`: user-uploaded screener or outcome rows.
- `free_api`: free provider data, when configured.
- `paid`: reserved for paid data sources, not used by Free Shadow Mode.

## Missing Fields

Manual uploads often miss float, short float, market cap, halt status, offering
status, reverse splits, and catalyst URLs. Dawnstrike records a
`coverage_warning` and `missing_enrichment_count` instead of inventing those
fields.

## No Lookahead

Manual outcomes are rejected if their entry timestamp is earlier than the saved
recommendation timestamp. Outcome prices collected later are only used after the
recommendation has already been persisted.

## Return Labels

Fixture and manual returns are paper-validation metrics. They are not live paid
data validation, not a forecast, and not a real-money trading claim.
