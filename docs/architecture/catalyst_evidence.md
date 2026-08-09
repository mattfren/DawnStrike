# Point-in-time catalyst evidence

Catalyst evidence is an append-only research input. A filing or article is
identified by source kind plus immutable content hash; canonical URLs are
retained when available. Raw SEC primary documents are stored outside SQLite
and referenced by path and SHA-256 hash.

Availability is evaluated at the decision timestamp. Evidence published or
first seen after that timestamp is retained as `post_decision_new_information`
and cannot be used to reconstruct the earlier decision. `no_news`,
`provider_failed`, `insufficient_text`, `conflicting_sources`, and
`unclassified` remain distinct states.

The extractor is a strict factual boundary reused from the Scenario extractor.
It may return factual claims with exact evidence spans or abstain/reject. It
cannot emit scores, grades, probabilities, predictions, directions, targets,
stops, sizes, or recommendations. Deterministic code maps verified facts to
research features.

The registered S-3/424B5 inside-72-hours feature is `avoid_long` only, with
security type and actual takedown terms retained. It does not create a fade or
short route. Validation-set precision, recall, coverage, abstention, effective
sample size, and incremental-lift thresholds remain pending human protocol
approval until a frozen reviewed set exists.
