from dataclasses import replace
from itertools import product

import pytest

from intraday_scanner.alpha import cycle3_experiments as cycle3
from intraday_scanner.scenario.prefilter import prefilter_scenario_articles
from tests.test_scenario_intelligence import _article


def test_prefilter_is_order_independent_and_deduplicates_content() -> None:
    first = _article()
    duplicate = replace(first, article_id="news-2")

    left = prefilter_scenario_articles(
        [first, duplicate], decision_at="2026-08-03T14:05:00Z"
    )
    right = prefilter_scenario_articles(
        [duplicate, first], decision_at="2026-08-03T14:05:00Z"
    )

    assert left.counts == {"AVOID": 1, "PREFILTERED": 1}
    assert [item.as_dict() for item in left.observations] == [
        item.as_dict() for item in right.observations
    ]
    assert [item.article_id for item in left.eligible_articles] == ["news-1"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda article: replace(article, created_at="2026-08-03T14:06:00Z"), "created_at_future"),
        (lambda article: replace(article, created_at="2026-08-01T14:00:00Z"), "article_stale"),
        (
            lambda article: replace(article, created_at="not-a-date"),
            "created_at_missing_or_invalid",
        ),
        (
            lambda article: replace(article, provider_delay_seconds=float("nan")),
            "provider_delay_missing_or_invalid",
        ),
        (lambda article: replace(article, source="Unknown blog"), "unsupported_source"),
        (lambda article: replace(article, content="tiny"), "non_material_content"),
    ],
)
def test_prefilter_rejects_hostile_metadata_without_model_input(change, reason: str) -> None:
    result = prefilter_scenario_articles(
        [change(_article())], decision_at="2026-08-03T14:05:00Z"
    )
    assert not result.eligible_articles
    assert reason in result.observations[0].reason_codes
    assert result.observations[0].status in {"ABSTAIN", "AVOID"}


def test_prefilter_rejects_future_decision_timestamp() -> None:
    result = prefilter_scenario_articles([_article()], decision_at="bad-time")
    assert not result.eligible_articles
    assert result.observations[0].status == "ABSTAIN"
    assert result.observations[0].reason_codes == ("evidence_cutoff_timestamp_missing_or_invalid",)


@pytest.mark.parametrize(
    "field,value",
    list(product(("max_age_seconds",), (True, 1.5, float("nan"), float("inf"), "60", -1))),
)
def test_prefilter_rejects_non_integer_max_age(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        prefilter_scenario_articles(
            [_article()], decision_at="2026-08-03T14:05:00Z", **{field: value}
        )


@pytest.mark.parametrize("value", (True, 1.5, float("nan"), float("inf"), "40", 0, -1))
def test_prefilter_rejects_non_positive_integer_min_text(value: object) -> None:
    with pytest.raises(ValueError, match="positive"):
        prefilter_scenario_articles(
            [_article()], decision_at="2026-08-03T14:05:00Z", min_text_chars=value
        )


@pytest.mark.parametrize("tiers", (("T3",), ("t1",), (1,), (), "T1"))
def test_prefilter_rejects_unsupported_source_tiers(tiers: object) -> None:
    with pytest.raises(ValueError, match="source tier"):
        prefilter_scenario_articles(
            [_article()], decision_at="2026-08-03T14:05:00Z", supported_source_tiers=tiers
        )


def test_prefilter_valid_duplicate_wins_over_stale_copy() -> None:
    valid = _article()
    stale = replace(valid, article_id="a-stale", created_at="2020-01-01T00:00:00Z")
    result = prefilter_scenario_articles(
        [valid, stale], decision_at="2026-08-03T14:05:00Z"
    )
    assert [article.article_id for article in result.eligible_articles] == [valid.article_id]
    stale_observation = next(item for item in result.observations if item.article_id == "a-stale")
    assert stale_observation.status == "AVOID"
    assert "article_stale" in stale_observation.reason_codes
    assert "duplicate_article" in stale_observation.reason_codes


def test_prefilter_allows_material_synonym_without_keyword_allowlist() -> None:
    article = replace(
        _article(),
        headline="Company reports a major operational development",
        summary="The issuer disclosed a significant change in its business operations.",
        content=(
            "The company disclosed a significant change in its business operations and "
            "described the expected implementation timeline for customers and employees."
        ),
    )
    result = prefilter_scenario_articles(
        [article], decision_at="2026-08-03T14:05:00Z"
    )
    assert [item.article_id for item in result.eligible_articles] == [article.article_id]


def test_cycle3_prefilter_receipt_rejects_trade_action_and_hash_tampering() -> None:
    evidence = cycle3.Cycle3EvidenceHashes("a" * 64, "b" * 64, "c" * 40, "d" * 64, "e" * 64)
    kwargs = {
        "market_date": "2026-08-03",
        "observations": [
            {
                "candidate_id": "candidate-1",
                "decision_id": "decision-1",
                "decision_at": "2026-08-03T14:05:00Z",
                "observation_policy": "dawnstrike-scenario-prefilter-v1",
                "prefilter_decision": "WATCH",
                "action": "ENTER_LONG",
            }
        ],
        "evidence": evidence,
        "scenario_config_hash_sha256": "b" * 64,
        "observation_policy_config": {"version": "v1"},
        "observation_policy_config_hash_sha256": cycle3.canonical_hash({"version": "v1"}),
    }
    with pytest.raises(ValueError, match="ENTER_LONG"):
        cycle3.build_scenario_prefilter_observation_receipt(**kwargs)

    kwargs["observations"][0].pop("action")
    receipt = cycle3.build_scenario_prefilter_observation_receipt(**kwargs)
    assert cycle3.validate_cycle3_receipt(receipt)
    receipt["observations"][0]["official_pnl"] = 99.0
    assert not cycle3.validate_cycle3_receipt(receipt)
