from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from intraday_scanner.services import luna_core_universe_service as core
from scripts import refresh_luna_core_universe as refresh_script


def _ndx_xlsx(symbols: list[str]) -> bytes:
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared = ["Company Name", "Security Symbol"]
    for symbol in symbols:
        shared.extend([f"Company {symbol}", symbol])
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sst xmlns="{namespace}" count="{len(shared)}" uniqueCount="{len(shared)}">'
        + "".join(f"<si><t>{value}</t></si>" for value in shared)
        + "</sst>"
    )
    rows = [
        '<row r="5"><c r="A5" t="s"><v>0</v></c>'
        '<c r="B5" t="s"><v>1</v></c></row>'
    ]
    for number, _symbol in enumerate(symbols, start=6):
        name_index = 2 + (number - 6) * 2
        symbol_index = name_index + 1
        rows.append(
            f'<row r="{number}"><c r="A{number}" t="s"><v>{name_index}</v></c>'
            f'<c r="B{number}" t="s"><v>{symbol_index}</v></c></row>'
        )
    rows.append('<row r="108"><c r="B108"/></row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{namespace}"><sheetData>'
        + "".join(rows)
        + "</sheetData></worksheet>"
    )
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return payload.getvalue()


def _members(symbols: list[str], effective: str = "2026-08-27") -> list[dict[str, object]]:
    return [
        {
            "ticker": symbol,
            "provider_symbol": symbol,
            "asset_class": "common_stock",
            "index_memberships": ["Nasdaq-100"],
            "valid_from": effective,
        }
        for symbol in symbols
    ]


def _manifest(
    tmp_path: Path,
    symbols: list[str],
    *,
    effective: str = "2026-08-27",
    observed: str = "2026-08-27T12:00:00Z",
    payload: bytes | None = None,
    source_id: str = "nasdaq-ndx-point-in-time-2026-08-27",
) -> dict[str, object]:
    payload = payload or _ndx_xlsx(symbols)
    path = tmp_path / "ndx.xlsx"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    records = _members(symbols, effective)
    canonical = [
        {
            "symbol": row["ticker"],
            "provider_symbol": row["provider_symbol"],
            "asset_class": row["asset_class"],
            "index": "Nasdaq-100",
            "valid_from": row["valid_from"],
            "valid_to": None,
        }
        for row in records
    ]
    member_hash = core._canonical_member_hash(canonical)
    lineage = {
        "schema_version": "dawnstrike.core_universe_lineage.v1",
        "builder_id": "nasdaq-ndx-sod-weightings-parser-v1",
        "transformation_id": "official-sod-weightings-export-v1",
        "reconstitution_id": "ndx-sod-2026-08-27",
        "effective_date": effective,
        "input_artifact_hashes": [digest],
        "canonical_member_set_hash_sha256": member_hash,
    }
    return {
        "source_id": source_id,
        "source_uri": core.NASDAQ_NDX_SOD_2026_08_27_URL,
        "source_scope": "Official Nasdaq-100 SOD Weightings export for 2026-08-27",
        "observed_at": observed,
        "effective_date": effective,
        "reconstitution_id": "ndx-sod-2026-08-27",
        "index_name": "Nasdaq-100",
        "expected_count": len(symbols),
        "completeness_verdict": "COMPLETE",
        "members": records,
        "source_artifacts": [
            {
                "uri": core.NASDAQ_NDX_SOD_2026_08_27_URL,
                "path": str(path),
                "sha256": digest,
            }
        ],
        "canonical_member_set_hash_sha256": member_hash,
        "reconstitution_lineage": lineage,
    }


@pytest.fixture
def ndx_symbols() -> list[str]:
    return [f"N{number:03d}" for number in range(101)] + ["HONA"]


def _install_test_root(monkeypatch: pytest.MonkeyPatch, digest: str) -> None:
    monkeypatch.setitem(
        core._TRUSTED_SOURCE_ROOTS,
        "nasdaq-ndx-point-in-time-2026-08-27",
        {
            "index": "Nasdaq-100",
            "effective_date": "2026-08-27",
            "raw_artifact_hashes": (digest,),
            "transformation_id": "nasdaq-ndx-sod-weightings-parser-v1",
            "lineage_builder_id": "nasdaq-ndx-sod-weightings-parser-v1",
            "lineage_transformation_id": "official-sod-weightings-export-v1",
            "reconstitution_id": "ndx-sod-2026-08-27",
            "membership_authority": "official_index_source",
            "official_index_authority": True,
            "source_scope": "Official Nasdaq-100 SOD Weightings export for 2026-08-27",
            "source_uri": core.NASDAQ_NDX_SOD_2026_08_27_URL,
        },
    )


def test_sod_export_parser_accepts_exact_102_row_schema(ndx_symbols: list[str]) -> None:
    assert len(core._parse_nasdaq_sod_weightings_xlsx(_ndx_xlsx(ndx_symbols))) == 102
    assert "HONA" in core._parse_nasdaq_sod_weightings_xlsx(_ndx_xlsx(ndx_symbols))
    assert "EA" not in core._parse_nasdaq_sod_weightings_xlsx(_ndx_xlsx(ndx_symbols))


def test_ea_hona_mismatch_is_blocked_even_when_member_hash_is_recomputed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ndx_symbols: list[str]
) -> None:
    payload = _ndx_xlsx(ndx_symbols)
    digest = hashlib.sha256(payload).hexdigest()
    _install_test_root(monkeypatch, digest)
    declared = ["EA" if symbol == "HONA" else symbol for symbol in ndx_symbols]
    manifest = _manifest(tmp_path, declared, payload=payload)
    contract = core.build_core_universe_contract(
        manifest, observed_at="2026-08-27T13:00:00Z", market_date="2026-08-27"
    )
    assert contract["status"] == "DATA_UNAVAILABLE"
    assert "source_binding_membership_mismatch" in contract["blockers"]


def test_changed_members_and_recomputed_manifest_hash_cannot_forge_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ndx_symbols: list[str]
) -> None:
    payload = _ndx_xlsx(ndx_symbols)
    digest = hashlib.sha256(payload).hexdigest()
    _install_test_root(monkeypatch, digest)
    changed = ["FORGED" if symbol == "N001" else symbol for symbol in ndx_symbols]
    manifest = _manifest(tmp_path, changed, payload=payload)
    contract = core.build_core_universe_contract(
        manifest, observed_at="2026-08-27T13:00:00Z", market_date="2026-08-27"
    )
    assert contract["status"] == "DATA_UNAVAILABLE"
    assert "source_binding_membership_mismatch" in contract["blockers"]


def test_old_july_replay_cannot_be_ready_for_august_27(
    tmp_path: Path, ndx_symbols: list[str]
) -> None:
    manifest = _manifest(
        tmp_path,
        ndx_symbols,
        effective="2026-07-07",
        source_id="nasdaq-ndx-point-in-time-2026-07-07",
    )
    contract = core.build_core_universe_contract(
        manifest, observed_at="2026-08-27T13:00:00Z", market_date="2026-08-27"
    )
    assert contract["status"] == "DATA_UNAVAILABLE"
    assert "currentness_date_mismatch:Nasdaq-100" in contract["blockers"]


def test_wrong_effective_date_and_stale_observation_are_both_blockers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ndx_symbols: list[str]
) -> None:
    payload = _ndx_xlsx(ndx_symbols)
    digest = hashlib.sha256(payload).hexdigest()
    _install_test_root(monkeypatch, digest)
    manifest = _manifest(
        tmp_path,
        ndx_symbols,
        effective="2026-08-26",
        observed="2026-07-01T12:00:00Z",
        payload=payload,
    )
    contract = core.build_core_universe_contract(
        manifest,
        observed_at=datetime(2026, 8, 27, 13, tzinfo=timezone.utc),
        market_date="2026-08-27",
    )
    assert contract["status"] == "DATA_UNAVAILABLE"
    assert "currentness_date_mismatch:Nasdaq-100" in contract["blockers"]
    assert "stale_manifest" in contract["blockers"]


def test_source_schema_failure_never_becomes_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ndx_symbols: list[str]
) -> None:
    payload = b"not-an-xlsx"
    digest = hashlib.sha256(payload).hexdigest()
    _install_test_root(monkeypatch, digest)
    manifest = _manifest(tmp_path, ndx_symbols, payload=payload)
    contract = core.build_core_universe_contract(
        manifest, observed_at="2026-08-27T13:00:00Z", market_date="2026-08-27"
    )
    assert contract["status"] == "DATA_UNAVAILABLE"
    assert any(
        item.startswith("source_binding_replay_failed:") for item in contract["blockers"]
    )


def test_proxy_authority_is_explicit_when_contract_is_built_in_test_mode() -> None:
    contract = core.build_core_universe_contract(
        [
            {
                "source_id": "spy-proxy",
                "source_uri": core.STATE_STREET_SPY_HOLDINGS_URL,
                "observed_at": "2026-08-27T12:00:00Z",
                "effective_date": "2026-08-24",
                "index_name": "S&P 500",
                "expected_count": 1,
                "members": [{"ticker": "SPY", "index": "S&P 500"}],
            },
            {
                "source_id": "ndx-test",
                "source_uri": "https://example.test/ndx",
                "observed_at": "2026-08-27T12:00:00Z",
                "effective_date": "2026-08-27",
                "index_name": "Nasdaq-100",
                "expected_count": 1,
                "members": [{"ticker": "QQQ", "index": "Nasdaq-100"}],
            },
        ],
        observed_at="2026-08-27T13:00:00Z",
        allow_test_override=True,
    )
    assert contract["proxy_disclosures"]
    assert contract["membership_authorities"] == {"S&P 500": [], "Nasdaq-100": []}


def test_direct_source_download_failure_is_explicitly_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_download(*args: object, **kwargs: object) -> object:
        raise OSError("network unavailable")

    monkeypatch.setattr(refresh_script, "urlopen", fail_download)
    with pytest.raises(RuntimeError, match="source download failed"):
        refresh_script._fetch(core.NASDAQ_NDX_SOD_2026_08_27_URL)


def test_refresh_preserves_prior_manifest_when_raw_capture_is_not_governed(
    tmp_path: Path, ndx_symbols: list[str]
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    output = config / "luna_core_universe.json"
    prior = {
        "schema_version": "dawnstrike.luna.core_universe_manifest_wrapper.v1",
        "manifests": [{"source_id": "spy", "index_name": "S&P 500"}],
    }
    output.write_text(json.dumps(prior), encoding="utf-8")
    raw = tmp_path / "ndx.xlsx"
    raw.write_bytes(_ndx_xlsx(ndx_symbols))
    with pytest.raises(RuntimeError, match="not the governed 2026-08-27 capture"):
        refresh_script.refresh(state_root=tmp_path, proxy_manifest=None, ndx_artifact=raw)
    assert json.loads(output.read_text(encoding="utf-8")) == prior
    assert not (config / "luna_core_universe_evidence" / "ndx-sod-2026-08-27.xlsx").exists()
