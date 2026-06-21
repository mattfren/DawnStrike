# Data Quality

Dawnstrike separates scoring mechanics from data quality. A high score from a
manual/free row only means the row scored well under the formula; it does not
mean the data is complete or provider-validated.

## Labels

- `sample` or `fixture`: bundled files for software testing.
- `manual`: user-uploaded screener or outcome rows.
- `web_url`: public table rows collected from an explicitly allowed URL.
- `browser_url`: optional rendered public table rows, still unverified shadow
  data.
- `free_api`: free provider data, when configured.
- `paid`: reserved for paid data sources, not used by Free Shadow Mode.

## Missing Fields

Manual uploads often miss float, short float, market cap, halt status, offering
status, reverse splits, and catalyst URLs. Dawnstrike records a
`coverage_warning` and `missing_enrichment_count` instead of inventing those
fields.

Public URL rows are marked `url_table_unverified`. If a table is missing price,
previous close, high, low, or volume, Dawnstrike skips the row or reports
failure. It does not create fake market data to make a scan run.

Official APIs, direct CSV exports, and broker/data-provider feeds are better
than scraping because their fields are more stable and their permissions are
clearer. Web table ingestion is a fallback for research and paper validation.

## Source Confidence

Signal Engine v3 carries `source_confidence` and `stale_data_flag` into every
candidate. Confidence is reduced for unverified public URLs, missing enrichment,
source conflicts, stale timestamps, and low-quality source runs. Source doctor
also reports aggregate source confidence and stale-data status.

## No Lookahead

Manual outcomes are rejected if their entry timestamp is earlier than the saved
recommendation timestamp. Outcome prices collected later are only used after the
recommendation has already been persisted.

## Return Labels

Fixture and manual returns are paper-validation metrics. They are not live paid
data validation, not a forecast, and not a real-money trading claim.
