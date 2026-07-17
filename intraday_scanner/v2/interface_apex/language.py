"""Plain-English wording for Interface Apex."""

from __future__ import annotations

COPY_TRANSLATIONS: dict[str, str] = {
    "AutoData": "Market data connection",
    "DataTruth": "Data quality check",
    "FillTruth": "Fill quality check",
    "CommitBridge": "Official paper-evidence gate",
    "PaperOps": "Paper trading record",
    "RiskHub": "Risk filter",
    "Strategy Evidence": "Strategy report card",
    "Learning Foundry": "What Dawnstrike learned",
    "Market Masters": "Research-inspired ideas",
    "Autonomous Runner": "Automatic schedule",
    "Telegram Intel": "Telegram updates",
    "Day Trade Lab": "Intraday day-trade research",
    "Daily Swing Research": "Daily-bar swing research",
}


def translate_term(term: str) -> str:
    """Return the primary-page wording for an internal Dawnstrike term."""

    return COPY_TRANSLATIONS.get(term, term)
