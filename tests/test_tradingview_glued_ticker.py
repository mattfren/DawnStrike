"""Regression tests for the TradingView "glued ticker+company" defect.

Distinct from the skeleton-placeholder bug (test_tradingview_skeleton_ticker.py):
here the ticker link (<a class="...tickerName...">TOPS</a>) and the separate
company-description link (<a class="...tickerDescription...">TOP Ships,
Inc.</a>) are adjacent siblings with NO whitespace text node between them at
all in the raw markup, so naive concatenation glues them: "TOPS" + "TOP
Ships, Inc." -> "TOPSTOP Ships, Inc.". Because "TOP Ships, Inc." happens to
start with letters that overlap the ticker, the downstream compound-split
heuristic (_best_compound_split) was tricked by a scoring tie into returning
"TOPST" instead of "TOPS". Similarly "QTTBQ32 Bio Inc." (real ticker QTTB)
and "USGOU.S. GoldMining Inc." (real ticker USGO).

The fix inserts a normalizing boundary space at every element edge inside a
table cell in _TableParser; " ".join(cell.split()) at cell-close collapses
any resulting duplicate/leading/trailing whitespace, so values that already
contained real inter-element spacing are unaffected.

The fixture is a trimmed, verbatim reproduction of the real markup captured
at
C:\\r\\dawnstrike-state\\outputs\\alpha_cycle\\2026-09-22\\web_collect\\tradingview_premarket\\raw_source.html
(data-rowkey="AMEX:TOPS", "NASDAQ:QTTB", "NASDAQ:USGO").
"""

from pathlib import Path

from intraday_scanner.providers.public_table_provider import (
    extract_html_tables,
    normalize_public_table_rows,
    select_best_table,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tradingview_premarket_glued_ticker_raw.html"


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_glued_ticker_and_company_are_separated_in_raw_cell_text() -> None:
    html = _load_fixture()
    tables = extract_html_tables(html)
    assert tables
    table = tables[0]
    ticker_cells = [row["symbol"] for row in table.rows]

    # Before the fix these were "TOPSTOP Ships, Inc.", "QTTBQ32 Bio Inc.",
    # "USGOU.S. GoldMining Inc." with zero separator between ticker and
    # company text.
    assert ticker_cells[0].startswith("TOPS ")
    assert ticker_cells[1].startswith("QTTB ")
    assert ticker_cells[2].startswith("USGO ")


def test_normalize_public_table_rows_resolves_real_tickers() -> None:
    html = _load_fixture()
    table = select_best_table(extract_html_tables(html))
    assert table is not None

    rows, warnings = normalize_public_table_rows(
        table,
        source_name="tradingview_premarket",
        source_url="https://www.tradingview.com/markets/stocks-usa/market-movers-pre-market-gainers/",
    )

    tickers = [row["ticker"] for row in rows]

    # Real tickers, confirmed against the anchor tags' title="TICKER - Company"
    # attribute in the on-disk raw HTML.
    assert "TOPS" in tickers, (tickers, warnings)
    assert "QTTB" in tickers, (tickers, warnings)
    assert "USGO" in tickers, (tickers, warnings)

    # Corrupted values the bug used to produce.
    assert "TOPST" not in tickers, (tickers, warnings)
    assert "QTTBQ" not in tickers, (tickers, warnings)
    assert "USGOU" not in tickers, (tickers, warnings)

    topst_row = next(row for row in rows if row["ticker"] == "TOPS")
    assert topst_row["company"] == "TOP Ships, Inc."

    usgo_row = next(row for row in rows if row["ticker"] == "USGO")
    assert usgo_row["company"] == "U.S. GoldMining Inc."


def test_boundary_space_regression_minimal_shape() -> None:
    """Minimal regression case: two adjacent <a> tags with zero whitespace
    between them must not be glued into one token."""
    html = """
    <table>
      <tr><th>Symbol</th><th>Company</th></tr>
      <tr>
        <td><a href="#">TOPS</a><a href="#">TOP Ships, Inc.</a></td>
        <td>ignored</td>
      </tr>
    </table>
    """
    tables = extract_html_tables(html)
    row = tables[0].rows[0]
    assert row["symbol"] == "TOPS TOP Ships, Inc."


def test_values_with_real_inter_element_spacing_are_unaffected() -> None:
    """Sanity check: cells whose sub-elements already had genuine
    whitespace between them (e.g. price + currency span) must not gain
    extra/duplicate spaces from the boundary-space fix."""
    html = """
    <table>
      <tr><th>Symbol</th><th>Price</th></tr>
      <tr>
        <td>ABCD</td>
        <td>6.07<span> USD</span></td>
      </tr>
    </table>
    """
    tables = extract_html_tables(html)
    row = tables[0].rows[0]
    assert row["price"] == "6.07 USD"
