# AlphaOps V6 data card

## Scope

The V6 dataset joins immutable candidate decisions to sourced outcome receipts.
The problem domain is U.S. small-cap/premarket research, not a fixed
large-cap-only sample. Universe membership, listings, delistings, symbol
changes, corporate actions, raw-source availability times, and source hashes
must be retained as data lineage.

## Required source truth

- Primary configured quote/bar source and independent secondary reconciliation;
- authoritative exchange halt evidence;
- SEC filing, dilution, and corporate-action evidence;
- sourced previous close, float, spread, volume, catalyst, and timestamps;
- predeclared SPY/IWM benchmark observations;
- provider health, quota, freshness, disagreement, and failure receipts.

## Inclusion

A return label is eligible only when decision identity, simulated fill, close,
fees, slippage, benchmark, source bars, no-lookahead validation, and
reconciliation agree. If full-candidate outcome capture is impractical,
rejected-candidate research uses a frozen stratified sample with stored
inclusion probability. The sample cannot be chosen after outcomes are seen.

## Known limitation

The deployed runtime lacks a real `config/web_sources.yaml` and a dated,
source-backed V6 small-cap universe registration. No placeholder or example
configuration is a production data source. Until credentialed providers, a real
accountable user agent, and immutable universe snapshots are configured, the
dataset remains intentionally insufficient for return learning.
