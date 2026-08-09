import json
from datetime import datetime, timezone

from intraday_scanner.providers.sec_edgar_provider import (
    classify_filing_research_feature,
    enrich_rows_with_sec_risk,
    normalize_filing_facts,
    parse_filing_evidence,
    parse_submissions_json,
)


def _submission(form: str, description: str, filed_at: str) -> str:
    return json.dumps(
        {
            "cik": "1234567",
            "filings": {
                "recent": {
                    "accessionNumber": ["0001234567-26-000001"],
                    "filingDate": [filed_at],
                    "form": [form],
                    "primaryDocument": ["filing.htm"],
                    "primaryDocDescription": [description],
                }
            },
        }
    )


def test_sec_old_registration_does_not_create_permanent_offering_block():
    events = parse_submissions_json(
        _submission("S-3", "Shelf registration statement", "2020-01-01"),
        ticker="NOVA",
    )
    enriched = enrich_rows_with_sec_risk(
        [{"ticker": "NOVA"}],
        events,
        checked_tickers=["NOVA"],
        as_of=datetime.now(timezone.utc),
    )

    assert events == []
    assert enriched[0]["sec_risk_status"] == "CLEAR"
    assert enriched[0]["recent_offering"] is False


def test_sec_filing_evidence_preserves_amendment_acceptance_items_and_primary_url():
    payload = json.dumps(
        {
            "cik": "1234567",
            "filings": {
                "recent": {
                    "accessionNumber": ["0001234567-26-000003"],
                    "filingDate": ["2026-08-03"],
                    "acceptanceDateTime": ["20260803135900"],
                    "form": ["S-3/A"],
                    "items": ["1.01;3.02"],
                    "primaryDocument": ["amended.htm"],
                    "primaryDocDescription": ["Amended shelf registration"],
                }
            },
        }
    )

    records = parse_filing_evidence(
        payload,
        ticker="NOVA",
        fetched_at="2026-08-03T14:00:00Z",
    )

    assert records[0]["cik"] == "1234567"
    assert records[0]["amendment_status"] == "amended"
    assert records[0]["eight_k_items"] == ""
    assert records[0]["primary_document_url"].endswith("/amended.htm")


def test_sec_facts_verify_arithmetic_and_register_avoid_long_only_feature():
    filing = {
        "ticker": "NOVA",
        "form": "S-3",
        "filing_date": "2026-08-03",
        "primary_document_url": "https://www.sec.gov/Archives/example",
        "primary_doc_description": "Shelf registration",
    }
    facts = normalize_filing_facts(
        filing,
        document_text=(
            "Common stock offering. Gross proceeds $10 million at $10 price per "
            "share for 1 million shares. Takedown terms apply."
        ),
    )
    feature = classify_filing_research_feature(
        filing,
        facts,
        decision_at="2026-08-03T14:00:00Z",
    )

    assert facts["security_type"] == "common_stock"
    assert facts["arithmetic_status"] == "PASS"
    assert feature["avoid_long"] is True
    assert feature["route"] == "none"


def test_sec_424b2_debt_pricing_supplement_is_not_automatic_dilution():
    today = datetime.now(timezone.utc).date().isoformat()
    events = parse_submissions_json(
        _submission("424B2", "Debt pricing supplement", today),
        ticker="BANK",
    )
    enriched = enrich_rows_with_sec_risk(
        [{"ticker": "BANK"}],
        events,
        checked_tickers=["BANK"],
        as_of=today,
    )

    assert events == []
    assert enriched[0]["sec_risk_status"] == "CLEAR"
