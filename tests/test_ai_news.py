from intraday_scanner.ai.headline_classifier import RuleBasedHeadlineClassifier
from intraday_scanner.ai.thesis_monitor import ThesisMonitor
from intraday_scanner.providers.base import NewsItem
from intraday_scanner.providers.news_provider import MockNewsProvider


def test_mock_news_provider_and_classifier_are_offline():
    provider = MockNewsProvider(
        [
            NewsItem(
                ticker="NOVA",
                headline="NOVA announces registered direct offering",
                published_at="2026-06-20T09:31:00-04:00",
                source="fixture",
            )
        ]
    )

    items = provider.get_news(["NOVA"])
    classification = RuleBasedHeadlineClassifier().classify(
        ticker="NOVA", headline=items[0].headline, thesis="FDA catalyst"
    )
    thesis = ThesisMonitor().compare(classification)

    assert classification.label == "dilution_risk"
    assert classification.severity == "critical"
    assert thesis.state == "broken"
