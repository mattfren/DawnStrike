"""Compare live information against the saved recommendation thesis."""

from __future__ import annotations

from dataclasses import dataclass

from intraday_scanner.ai.headline_classifier import HeadlineClassification


@dataclass(frozen=True)
class ThesisRead:
    state: str
    reason: str


class ThesisMonitor:
    def compare(self, classification: HeadlineClassification) -> ThesisRead:
        if classification.label in {
            "dilution_risk",
            "halt_risk",
            "fraud_legal_risk",
            "catalyst_contradicted",
        }:
            return ThesisRead("broken", classification.reason)
        if classification.label == "bearish":
            return ThesisRead("weakening", classification.reason)
        if classification.label in {"bullish", "catalyst_confirmed"}:
            return ThesisRead("improving", classification.reason)
        if classification.label == "unavailable":
            return ThesisRead("unavailable", "No source data was available.")
        return ThesisRead("unchanged", classification.reason)
