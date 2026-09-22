"""Regression tests for the 2026-09-22 SPY trust root.

These exercise the repo's own attestation path (`_attest_spy_payload`)
against the real, already-staged State Street workbook fetched for the
2026-09-22 rebalance (BE, ILMN, P in; BLDR, TAP, TTD out; 503 -> 503) and
against a tampered copy of that same workbook whose membership does not
match any governed root.  No network call is made by these tests.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from intraday_scanner.services import luna_core_universe_service as core
from scripts import refresh_luna_core_universe as refresh_script

_STAGED_WORKBOOK = Path("C:/r/spy_current_20260922.xlsx")
_NEW_ROOT_ID = "state-street-spy-holdings-proxy-2026-09-22"
_AUG24_ROOT_ID = "state-street-spy-holdings-proxy-2026-08-24"


def _require_staged_workbook() -> bytes:
    if not _STAGED_WORKBOOK.exists():
        pytest.skip(f"staged workbook not present at {_STAGED_WORKBOOK}")
    return _STAGED_WORKBOOK.read_bytes()


def _tamper_membership(payload: bytes) -> bytes:
    """Return a copy of ``payload`` with one ticker cell changed.

    The zip member names and every canonical *static* member's bytes are left
    untouched, so the structural/schema attestations still match the
    governed root exactly.  Only the dynamic worksheet content (row 6's
    ticker cell) changes, which changes the canonical symbol-set hash and
    must therefore be rejected under every governed root.
    """

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        contents = {name: archive.read(name) for name in names}

    sheet = contents["xl/worksheets/sheet1.xml"].decode("utf-8")
    needle = '<c r="B6" t="s" s="18"><v>22</v></c>'
    assert needle in sheet, "unexpected sheet1.xml layout for row 6 ticker cell"
    replacement = '<c r="B6" t="inlineStr" s="18"><is><t>ZZZZ</t></is></c>'
    tampered_sheet = sheet.replace(needle, replacement, 1)
    contents["xl/worksheets/sheet1.xml"] = tampered_sheet.encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name in names:
            archive.writestr(name, contents[name])
    return out.getvalue()


def test_current_workbook_is_accepted_under_the_new_trust_root() -> None:
    """The staged 2026-09-22 workbook attests successfully under the new root."""

    payload = _require_staged_workbook()
    symbols, effective, attestation = refresh_script._attest_spy_payload(
        payload,
        source_id=_NEW_ROOT_ID,
        market_date="2026-09-22",
    )
    assert len(symbols) == 503
    assert len(set(symbols)) == 503
    assert effective == "2026-09-21"
    assert attestation["symbol_set_hash_sha256"] == (
        "d80deb8af1de5b17af7db50a5b634d0903585642f4c426db660767fe5a867e7e"
    )


def test_workbook_with_unmatched_membership_is_still_rejected() -> None:
    """A structurally valid but membership-tampered workbook is refused.

    This is the critical guard: minting the new root must not create any
    path that accepts a workbook whose member set does not match a governed
    root's pinned symbol-set hash.
    """

    payload = _require_staged_workbook()
    tampered = _tamper_membership(payload)
    assert tampered != payload

    for source_id in (
        _AUG24_ROOT_ID,
        _NEW_ROOT_ID,
    ):
        with pytest.raises(RuntimeError, match="member set mismatch"):
            refresh_script._attest_spy_payload(
                tampered,
                source_id=source_id,
                market_date="2026-09-22",
            )


# --- Root SELECTION (not just attestation) -----------------------------


def test_current_spy_source_id_ignores_the_prior_manifest_and_selects_2026_09_22() -> None:
    """Selection must not be hostage to a stale inherited source_id.

    The live active pointer's prior generation names the 2026-08-24 root.
    ``_current_spy_source_id`` must not read that value at all: it must pick
    the newest COMMITTED root for the index whose effective_date is not
    after the market date, exactly as it would today with a real pointer
    naming the 2026-08-24 root.
    """

    assert refresh_script._current_spy_source_id("2026-09-22") == _NEW_ROOT_ID
    # A market date before the new root's effective date must still resolve
    # to the older, correct-at-the-time root (no regression for historical
    # replay/back-dated requests).
    assert refresh_script._current_spy_source_id("2026-08-25") == _AUG24_ROOT_ID


def test_current_spy_source_id_fails_closed_with_no_eligible_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No governed root effective on/before the date -> explicit failure.

    This is the fail-closed guarantee: selection never silently falls back
    to "any root" or skips attestation when nothing is actually governed for
    the requested date.
    """

    monkeypatch.setitem(core._TRUSTED_SOURCE_ROOTS, _AUG24_ROOT_ID, dict(
        core._TRUSTED_SOURCE_ROOTS[_AUG24_ROOT_ID], effective_date="2026-08-24"
    ))
    monkeypatch.setitem(core._TRUSTED_SOURCE_ROOTS, _NEW_ROOT_ID, dict(
        core._TRUSTED_SOURCE_ROOTS[_NEW_ROOT_ID], effective_date="2026-09-22"
    ))
    with pytest.raises(RuntimeError, match="no governed S&P 500 trust root"):
        refresh_script._current_spy_source_id("2026-08-01")


def test_refresh_end_to_end_selects_new_root_despite_stale_prior_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full ``refresh()`` run: a live-shaped prior manifest naming the
    2026-08-24 SPY source_id must still cause today's refresh to attest
    against the 2026-09-22 root and succeed against the current workbook.

    The Nasdaq-100 side is neutral scaffolding (a synthetic workbook under a
    synthetic, monkeypatched NDX root) so this test isolates the SPY
    selection behaviour under test; no SPY trust root is monkeypatched.
    """

    market_date = "2026-09-22"
    spy_payload = _require_staged_workbook()

    ndx_symbols = [f"N{number:03d}" for number in range(101)] + ["HONA"]
    ndx_payload = _build_ndx_xlsx(ndx_symbols)
    _install_ndx_root_for_market_date(monkeypatch, ndx_payload, market_date)

    config = tmp_path / "config"
    config.mkdir()
    proxy_manifest = config / "luna_core_universe.json"
    # This is the live-shaped scenario: the prior generation's SPY child
    # names the OLD (2026-08-24) source_id, exactly like the real active
    # pointer's ndx-sod-2026-09-18 generation does today.
    proxy_manifest.write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.luna.core_universe_manifest_wrapper.v1",
                "manifests": [
                    {
                        "source_id": _AUG24_ROOT_ID,
                        "source_uri": core.STATE_STREET_SPY_HOLDINGS_URL,
                        "source_scope": "SPY tracker holdings proxy (prior generation)",
                        "observed_at": "2026-09-18T12:00:00Z",
                        "effective_date": "2026-08-24",
                        "reconstitution_id": "spy-holdings-2026-08-24",
                        "index_name": "S&P 500",
                        "expected_count": 503,
                        "completeness_verdict": "COMPLETE",
                        "members": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    ndx_artifact = tmp_path / "ndx.xlsx"
    ndx_artifact.write_bytes(ndx_payload)
    spy_artifact = tmp_path / "spy.xlsx"
    spy_artifact.write_bytes(spy_payload)

    result = refresh_script.refresh(
        state_root=tmp_path,
        proxy_manifest=proxy_manifest,
        ndx_artifact=ndx_artifact,
        spy_artifact=spy_artifact,
        market_date=market_date,
    )
    assert result["status"] == "READY"
    contract = core.build_core_universe_contract(
        config / "luna_core_universe.json",
        observed_at=result["observed_at"],
        market_date=market_date,
    )
    assert contract["status"] == "READY"
    spy_artifact_entry = next(
        item for item in contract["source_artifacts"] if item.get("source_id") == _NEW_ROOT_ID
    )
    assert spy_artifact_entry["source_id"] == _NEW_ROOT_ID
    assert not any(
        item.get("source_id") == _AUG24_ROOT_ID for item in contract["source_artifacts"]
    )


def test_refresh_aug24_root_still_correct_for_its_own_historical_date() -> None:
    """No regression: a back-dated request for 2026-08-25 still resolves to,
    and succeeds under, the real 2026-08-24 root.

    The test workbook is the real, currently staged production zip (so every
    structural/static-member file is byte-identical to what the governed
    root already pins) with only its dynamic membership rows rebuilt to the
    authorized 2026-08-24 membership set: current 503 symbols minus the
    confirmed ADDED set {BE, ILMN, P} plus the confirmed REMOVED set
    {BLDR, TAP, TTD}.  This is a derivation from orchestrator-verified facts,
    not a hand-typed hash: the resulting canonical symbol-set hash is
    asserted to equal the real pinned Aug-24 root hash below, proving the
    reconstruction is exact rather than assumed.
    """

    market_date = "2026-08-25"
    aug24_root = core._TRUSTED_SOURCE_ROOTS[_AUG24_ROOT_ID]
    current_payload = _require_staged_workbook()
    current_symbols, _effective, _attestation = core._parse_spy_holdings_xlsx_with_attestation(
        [current_payload]
    )
    reconstructed_symbols = sorted(
        (set(current_symbols) - {"BE", "ILMN", "P"}) | {"BLDR", "TAP", "TTD"}
    )
    assert len(reconstructed_symbols) == 503

    payload = _build_spy_xlsx(reconstructed_symbols, effective_date="2026-08-24")

    assert refresh_script._current_spy_source_id(market_date) == _AUG24_ROOT_ID
    symbols, effective, attestation = refresh_script._attest_spy_payload(
        payload,
        source_id=refresh_script._current_spy_source_id(market_date),
        market_date=market_date,
    )
    assert len(symbols) == 503
    assert effective == "2026-08-24"
    assert attestation["symbol_set_hash_sha256"] == aug24_root["canonical_symbol_set_hash_sha256"]


# --- Minimal, self-contained scaffolding for the end-to-end test --------


def _build_ndx_xlsx(symbols: list[str], *, company_prefix: str = "Company") -> bytes:
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared = ["Company Name", "Security Symbol"]
    for symbol in symbols:
        shared.extend([f"{company_prefix} {symbol}", symbol])
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sst xmlns="{namespace}" count="{len(shared)}" uniqueCount="{len(shared)}">'
        + "".join(f"<si><t>{value}</t></si>" for value in shared)
        + "</sst>"
    )
    rows = ['<row r="5"><c r="A5" t="s"><v>0</v></c><c r="B5" t="s"><v>1</v></c></row>']
    for number, _symbol in enumerate(symbols, start=6):
        name_index = 2 + (number - 6) * 2
        symbol_index = name_index + 1
        rows.append(
            f'<row r="{number}"><c r="A{number}" t="s"><v>{name_index}</v></c>'
            f'<c r="B{number}" t="s"><v>{symbol_index}</v></c></row>'
        )
    rows.append(f'<row r="{6 + len(symbols)}"><c r="B{6 + len(symbols)}"/></row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{namespace}"><sheetData>' + "".join(rows) + "</sheetData></worksheet>"
    )
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for name in core._NDX_CANONICAL_ZIP_MEMBER_NAMES:
            archive.writestr(
                name,
                shared_xml
                if name == "xl/sharedStrings.xml"
                else sheet_xml
                if name == "xl/worksheets/sheet1.xml"
                else "<root />",
            )
    return payload.getvalue()


def _install_ndx_root_for_market_date(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, market_date: str
) -> None:
    _symbols, attestation = core._parse_nasdaq_sod_weightings_xlsx_with_attestation(
        payload, effective_date=market_date
    )
    monkeypatch.setitem(
        core._TRUSTED_SOURCE_ROOTS,
        refresh_script.NDX_SOURCE_ID,
        {
            "index": "Nasdaq-100",
            "effective_date": market_date,
            "raw_artifact_hashes": (),
            "canonical_zip_member_names": attestation["member_names"],
            "canonical_zip_member_hashes": attestation["member_hashes"],
            "canonical_static_member_hashes": attestation["static_member_hashes"],
            "canonical_content_digest_sha256": attestation["content_digest_sha256"],
            "canonical_member_set_hash_sha256": attestation["member_set_hash_sha256"],
            "canonical_symbol_set_hash_sha256": attestation["symbol_set_hash_sha256"],
            "transformation_id": "nasdaq-ndx-sod-weightings-parser-v1",
            "lineage_builder_id": "nasdaq-ndx-sod-weightings-parser-v1",
            "lineage_transformation_id": "official-sod-weightings-export-v1",
            "lineage_schema_version": "dawnstrike.core_universe_lineage.v1",
            "reconstitution_id": f"ndx-sod-{market_date}",
            "membership_authority": "official_index_source",
            "official_index_authority": True,
            "source_scope": f"Official Nasdaq-100 SOD Weightings export for {market_date}",
            "source_uri": "https://example.test/ndx.xlsx",
        },
    )


def _build_spy_xlsx(symbols: list[str], *, effective_date: str) -> bytes:
    """Build an SPY workbook for the given membership and as-of date.

    Starts from the real, currently staged production zip and replaces only
    the dynamic ``xl/worksheets/sheet1.xml`` member with freshly built rows.
    Every canonical *static* member (the files the governed root pins by
    name and hash) is left byte-identical to the real production workbook,
    so this always satisfies the structural/static-member checks that both
    the 2026-08-24 and 2026-09-22 roots share.
    """

    import datetime as _dt

    with zipfile.ZipFile(io.BytesIO(_require_staged_workbook())) as archive:
        names = archive.namelist()
        contents = {name: archive.read(name) for name in names}

    effective_label = _dt.datetime.fromisoformat(effective_date).strftime("%d-%b-%Y")
    rows = [
        f'<row r="3"><c r="B3" t="inlineStr"><is><t>As of {effective_label}</t></is></c></row>',
        '<row r="5"><c r="B5" t="inlineStr"><is><t>Ticker</t></is></c></row>',
    ]
    for number, symbol in enumerate([*symbols, "-", "2602335D"], start=6):
        rows.append(
            f'<row r="{number}"><c r="A{number}" t="inlineStr"><is><t>Security</t></is></c>'
            f'<c r="B{number}" t="inlineStr"><is><t>{symbol}</t></is></c></row>'
        )
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{namespace}"><sheetData>{"".join(rows)}</sheetData></worksheet>'
    )
    contents["xl/worksheets/sheet1.xml"] = sheet.encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name in names:
            archive.writestr(name, contents[name])
    return out.getvalue()
