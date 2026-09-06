"""The automatic float source must refuse stale evidence and never fabricate.

``float_shares`` feeds a 14-point scoring component, so a wrong or stale share
count directly distorts the setup grade.  These tests pin the guarantees that
make an automatic source safe to rely on.
"""

from __future__ import annotations

import json

from intraday_scanner.providers.sec_float_provider import (
    FLOAT_SOURCE_KIND,
    MAX_FILING_AGE_DAYS,
    _latest_share_count,
    enrich_rows_with_float,
)


def _concept(entries: list[dict[str, object]]) -> str:
    return json.dumps({"units": {"shares": entries}})


def test_latest_share_count_uses_filing_date_not_period_end() -> None:
    """An amended filing supersedes an older one even with an earlier period end."""

    payload = _concept(
        [
            {"val": 1_000_000, "end": "2026-06-30", "filed": "2026-07-15"},
            {"val": 2_500_000, "end": "2026-03-31", "filed": "2026-08-20"},
        ]
    )

    best = _latest_share_count(payload)

    assert best is not None
    assert best["shares"] == 2_500_000
    assert best["filed"] == "2026-08-20"


def test_latest_share_count_ignores_non_share_units_and_bad_values() -> None:
    payload = json.dumps(
        {
            "units": {
                "USD": [{"val": 999_999, "filed": "2026-08-01"}],
                "shares": [
                    {"val": 0, "filed": "2026-08-02"},
                    {"val": -5, "filed": "2026-08-03"},
                    {"val": None, "filed": "2026-08-04"},
                    {"val": 700, "filed": "2026-08-05"},
                ],
            }
        }
    )

    best = _latest_share_count(payload)

    assert best is not None
    assert best["shares"] == 700


def test_latest_share_count_requires_a_filing_date() -> None:
    assert _latest_share_count(_concept([{"val": 1_000}])) is None
    assert _latest_share_count("not json") is None


def test_max_filing_age_allows_an_annual_filer() -> None:
    """A yearly filer must not be discarded merely for filing annually."""

    assert MAX_FILING_AGE_DAYS >= 366


def test_enrichment_clears_unknown_float_and_records_provenance() -> None:
    counts = {
        "shares_by_ticker": {
            "BIAF": {
                "ticker": "BIAF",
                "cik": 1712762,
                "shares_outstanding": 8_101_725.0,
                "filed": "2026-08-07",
                "period_end": "2026-08-07",
                "form": "10-Q",
                "concept": "dei:EntityCommonStockSharesOutstanding",
                "source_kind": FLOAT_SOURCE_KIND,
                "proxy_note": "shares_outstanding_proxy_not_free_float",
            }
        }
    }

    [row] = enrich_rows_with_float(
        [{"ticker": "BIAF", "risk_flags": "unknown_float;low_source_count"}], counts
    )

    assert row["float_shares"] == 8_101_725.0
    assert row["float_source"] == FLOAT_SOURCE_KIND
    assert row["float_as_of"] == "2026-08-07"
    # The proxy must be labelled: outstanding shares are not free float.
    assert row["float_is_outstanding_proxy"] is True
    assert "unknown_float" not in row["risk_flags"]
    # Unrelated flags survive.
    assert "low_source_count" in row["risk_flags"]


def test_enrichment_never_overwrites_a_supplied_float() -> None:
    """A precise operator-supplied float outranks an outstanding-share proxy."""

    counts = {
        "shares_by_ticker": {
            "BIAF": {
                "shares_outstanding": 8_101_725.0,
                "filed": "2026-08-07",
                "concept": "dei:EntityCommonStockSharesOutstanding",
                "source_kind": FLOAT_SOURCE_KIND,
                "proxy_note": "x",
            }
        }
    }

    [row] = enrich_rows_with_float([{"ticker": "BIAF", "float_shares": 3_000_000}], counts)

    assert row["float_shares"] == 3_000_000
    assert "float_source" not in row


def test_unresolved_ticker_keeps_unknown_float() -> None:
    """A missing share count must stay visibly missing, never invented."""

    [row] = enrich_rows_with_float(
        [{"ticker": "GTLB", "risk_flags": "unknown_float"}],
        {"shares_by_ticker": {}},
    )

    assert "float_shares" not in row or not row.get("float_shares")
    assert "unknown_float" in row["risk_flags"]
