"""Regression tests for the 2026-09-22 SPY trust root.

These exercise the repo's own attestation path (`_attest_spy_payload`)
against the real, already-staged State Street workbook fetched for the
2026-09-22 rebalance (BE, ILMN, P in; BLDR, TAP, TTD out; 503 -> 503) and
against a tampered copy of that same workbook whose membership does not
match any governed root.  No network call is made by these tests.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from scripts import refresh_luna_core_universe as refresh_script

_STAGED_WORKBOOK = Path("C:/r/spy_current_20260922.xlsx")
_NEW_ROOT_ID = "state-street-spy-holdings-proxy-2026-09-22"


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
        "state-street-spy-holdings-proxy-2026-08-24",
        _NEW_ROOT_ID,
    ):
        with pytest.raises(RuntimeError, match="member set mismatch"):
            refresh_script._attest_spy_payload(
                tampered,
                source_id=source_id,
                market_date="2026-09-22",
            )
