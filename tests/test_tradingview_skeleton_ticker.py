"""Regression tests for the TradingView skeleton-placeholder ticker bug.

TradingView renders a loading-placeholder <span> containing only the
ticker's first letter (class name includes "skeleton") immediately before
the real ticker link text, when the logo image has not loaded yet. The raw
HTML cell-text extraction in ``_TableParser.handle_data`` used to fold that
placeholder text into the surrounding cell text, duplicating the leading
letter: real symbol AEHL (NASDAQ:AEHL, Antelope Enterprise Holdings
Limited) became "AAEHL". "AAEHL" then satisfied
``_split_tradingview_symbol``'s validity regex and was treated as a
genuine, if unfamiliar, ticker -- which later caused Yahoo acquisition to
fail with DataTruthAcquisitionIncomplete because "AAEHL" does not exist.

The fixture in tests/fixtures/tradingview_premarket_skeleton_raw.html is a
trimmed, verbatim reproduction of the real markup captured at
C:\\r\\dawnstrike-state\\outputs\\alpha_cycle\\2026-08-31\\web_collect\\tradingview_premarket\\raw_source.html
(data-rowkey="NASDAQ:AEHL").
"""

from pathlib import Path

from intraday_scanner.providers.public_table_provider import (
    extract_html_tables,
    normalize_public_table_rows,
    select_best_table,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tradingview_premarket_skeleton_raw.html"


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_skeleton_span_letter_excluded_from_extracted_cell_text() -> None:
    """The raw <td> text must not fold in the skeleton span's leading letter."""
    html = _load_fixture()
    tables = extract_html_tables(html)
    assert tables, "expected at least one table to be extracted"
    table = tables[0]
    first_row = table.rows[0]
    ticker_cell = first_row["symbol"]

    # Before the fix this was "AAEHLAntelope Enterprise Holdings Limited"
    # because the skeleton span's "A" and the real ticker link's "AEHL"
    # were concatenated by handle_data with no distinction between them.
    assert ticker_cell.startswith("AEHL"), ticker_cell
    assert not ticker_cell.startswith("AAEHL"), ticker_cell


def test_normalize_public_table_rows_resolves_real_symbol_not_doubled() -> None:
    """End-to-end: the normalized row must carry ticker AEHL, not AAEHL."""
    html = _load_fixture()
    table = select_best_table(extract_html_tables(html))
    assert table is not None

    rows, warnings = normalize_public_table_rows(
        table,
        source_name="tradingview_premarket",
        source_url="https://www.tradingview.com/markets/stocks-usa/market-movers-pre-market-gainers/",
    )

    tickers = [row["ticker"] for row in rows]
    assert "AEHL" in tickers, (tickers, warnings)
    assert "AAEHL" not in tickers, (tickers, warnings)

    aehl_row = next(row for row in rows if row["ticker"] == "AEHL")
    assert aehl_row["company"] == "Antelope Enterprise Holdings Limited"


def test_skeleton_span_regression_minimal_shape() -> None:
    """Minimal regression case for the skeleton-span shape itself.

    Guards against a future regression even if the TradingView page
    structure/class names drift slightly, as long as the placeholder span's
    class contains "skeleton".
    """
    html = """
    <table>
      <tr><th>Symbol</th><th>Company</th></tr>
      <tr>
        <td><span class="logo-x skeleton-x">N</span><a href="#">NCT</a><a href="#">Intercont (Cayman) Limited</a></td>
        <td>ignored</td>
      </tr>
    </table>
    """
    tables = extract_html_tables(html)
    assert tables
    row = tables[0].rows[0]
    ticker_cell = row["symbol"]
    assert ticker_cell.startswith("NCT")
    assert not ticker_cell.startswith("NNCT")


def test_non_skeleton_span_text_is_still_captured() -> None:
    """Sanity check: ordinary (non-skeleton) span text must not be dropped."""
    html = """
    <table>
      <tr><th>Symbol</th><th>Company</th></tr>
      <tr>
        <td><span class="badge-x">VVOS</span> extra text</td>
        <td>ignored</td>
      </tr>
    </table>
    """
    tables = extract_html_tables(html)
    row = tables[0].rows[0]
    assert row["symbol"] == "VVOS extra text"
