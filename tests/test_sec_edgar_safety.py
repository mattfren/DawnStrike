import json
from datetime import datetime, timezone

from intraday_scanner.providers.sec_edgar_provider import (
    enrich_rows_with_sec_risk,
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
