"""Prompt templates for optional AI headline/thesis monitoring."""

HEADLINE_CLASSIFIER_PROMPT = """Classify this market headline without inventing facts.

Ticker: {ticker}
Original thesis: {thesis}
Headline: {headline}

Return one label and one concise reason. Allowed labels:
bullish, bearish, neutral, dilution_risk, halt_risk, fraud_legal_risk,
catalyst_confirmed, catalyst_contradicted, unavailable.
"""

THESIS_MONITOR_PROMPT = """Compare the new information with the original thesis.

Ticker: {ticker}
Original thesis: {thesis}
New information: {new_information}

Return whether the thesis is improving, unchanged, weakening, broken, or unavailable.
Never fabricate missing data.
"""
