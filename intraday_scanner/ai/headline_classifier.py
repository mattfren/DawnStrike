"""Headline risk classifier abstraction.

The default implementation is deterministic and offline. A live LLM-backed
classifier can implement the same interface later; tests should mock that
implementation and never make network calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class HeadlineClassification:
    label: str
    severity: str
    reason: str


class HeadlineClassifier(Protocol):
    def classify(self, *, ticker: str, headline: str, thesis: str = "") -> HeadlineClassification:
        ...


class RuleBasedHeadlineClassifier:
    def classify(self, *, ticker: str, headline: str, thesis: str = "") -> HeadlineClassification:
        text = headline.lower()
        if not text.strip():
            return HeadlineClassification("unavailable", "info", "No headline text was available.")
        if any(term in text for term in ("offering", "atm", "shelf", "warrant", "dilution")):
            return HeadlineClassification(
                "dilution_risk", "critical", "Dilution language detected."
            )
        if "halt" in text:
            return HeadlineClassification("halt_risk", "critical", "Halt language detected.")
        if any(term in text for term in ("fraud", "sec investigation", "lawsuit", "doj")):
            return HeadlineClassification(
                "fraud_legal_risk", "critical", "Legal or fraud-risk language detected."
            )
        if any(term in text for term in ("denies", "withdraws", "failed", "rejected")):
            return HeadlineClassification(
                "catalyst_contradicted", "high", "Headline appears to contradict the thesis."
            )
        if any(term in text for term in ("approval", "contract", "partnership", "fda", "award")):
            return HeadlineClassification(
                "catalyst_confirmed", "info", "Headline appears to support the catalyst."
            )
        return HeadlineClassification("neutral", "info", "No high-risk headline terms detected.")
