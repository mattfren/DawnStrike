from __future__ import annotations

import pytest

from intraday_scanner.sql_safety import (
    quote_sql_identifier,
    quote_sql_identifiers,
    quote_sql_order_by,
)


def test_identifier_helpers_quote_allowlisted_identifiers() -> None:
    assert quote_sql_identifier("market_date", allowed={"market_date"}) == '"market_date"'
    assert quote_sql_identifiers(["ticker", "market_date"], allowed={"ticker", "market_date"}) == (
        '"ticker", "market_date"'
    )


@pytest.mark.parametrize("identifier", ["", "table; DROP TABLE rows", "x y", "1table"])
def test_identifier_helpers_reject_injection_shapes(identifier: str) -> None:
    with pytest.raises(ValueError, match="SQLite identifier"):
        quote_sql_identifier(identifier, allowed={"safe_table"})


def test_order_by_quotes_supported_components_and_rejects_sql() -> None:
    allowed = {"market_date", "rank", "ticker"}
    assert (
        quote_sql_order_by(
            "market_date ASC, COALESCE(rank, 999999) ASC, ticker DESC",
            allowed_columns=allowed,
        )
        == '"market_date" ASC, COALESCE("rank", 999999) ASC, "ticker" DESC'
    )
    with pytest.raises(ValueError, match="unsafe SQLite ORDER BY"):
        quote_sql_order_by("market_date; DROP TABLE rows", allowed_columns=allowed)
