from __future__ import annotations

import hashlib
import io
import json
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from intraday_scanner.services import luna_core_universe_service as core
from scripts import refresh_luna_core_universe as refresh_script


def _ndx_xlsx(symbols: list[str], *, company_prefix: str = "Company") -> bytes:
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
    rows.append('<row r="108"><c r="B108"/></row>')
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


def _spy_xlsx(symbols: list[str], *, effective_date: str = "2026-08-24") -> bytes:
    effective_label = datetime.fromisoformat(effective_date).strftime("%d-%b-%Y")
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
    shared = f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{namespace}"/>'
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
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


def _install_stable_test_root(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, symbols: list[str]
) -> None:
    _symbols, attestation = core._parse_nasdaq_sod_weightings_xlsx_with_attestation(payload)
    member_hash = core._canonical_member_hash(
        [
            {
                "symbol": symbol,
                "provider_symbol": symbol,
                "asset_class": "common_stock",
                "index": "Nasdaq-100",
                "valid_from": "2026-08-27",
                "valid_to": None,
            }
            for symbol in symbols
        ]
    )
    monkeypatch.setitem(
        core._TRUSTED_SOURCE_ROOTS,
        "nasdaq-ndx-point-in-time-2026-08-27",
        {
            "index": "Nasdaq-100",
            "effective_date": "2026-08-27",
            "raw_artifact_hashes": (),
            "raw_artifact_byte_counts": (len(payload),),
            "canonical_zip_member_names": attestation["member_names"],
            "canonical_zip_member_hashes": attestation["member_hashes"],
            "canonical_static_member_hashes": attestation["static_member_hashes"],
            "canonical_content_digest_sha256": attestation["content_digest_sha256"],
            "canonical_member_set_hash_sha256": member_hash,
            "transformation_id": "nasdaq-ndx-sod-weightings-parser-v1",
            "lineage_builder_id": "nasdaq-ndx-sod-weightings-parser-v1",
            "lineage_transformation_id": "official-sod-weightings-export-v1",
            "lineage_schema_version": "dawnstrike.core_universe_lineage.v1",
            "reconstitution_id": "ndx-sod-2026-08-27",
            "membership_authority": "official_index_source",
            "official_index_authority": True,
            "source_scope": "Official Nasdaq-100 SOD Weightings export for 2026-08-27",
            "source_uri": core.NASDAQ_NDX_SOD_2026_08_27_URL,
        },
    )


def _refresh_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> tuple[Path, bytes, bytes]:
    ndx_payload = _ndx_xlsx(ndx_symbols)
    _install_stable_test_root(monkeypatch, ndx_payload, ndx_symbols)
    ndx_root = core._TRUSTED_SOURCE_ROOTS["nasdaq-ndx-point-in-time-2026-08-27"]
    _ndx_parsed, ndx_attestation = core._parse_nasdaq_sod_weightings_xlsx_with_attestation(
        ndx_payload
    )
    ndx_root.update(
        {
            "allow_future_same_semantic_set_dates": True,
            "source_uri_template": core.NASDAQ_NDX_SOD_URL_TEMPLATE,
            "source_scope_template": (
                "Official Nasdaq-100 SOD Weightings export for {market_date}"
            ),
            "canonical_symbol_set_hash_sha256": ndx_attestation["symbol_set_hash_sha256"],
        }
    )
    spy_symbols = [f"S{number:03d}" for number in range(503)]
    spy_payload = _spy_xlsx(spy_symbols)
    spy_path = tmp_path / "spy.xlsx"
    spy_path.write_bytes(spy_payload)
    spy_digest = hashlib.sha256(spy_payload).hexdigest()
    _parsed_spy_symbols, _spy_effective, spy_attestation = (
        core._parse_spy_holdings_xlsx_with_attestation([spy_payload])
    )
    spy_members = [
        {
            "ticker": symbol,
            "provider_symbol": symbol,
            "asset_class": "common_stock",
            "index_memberships": ["S&P 500"],
            "valid_from": "2026-08-24",
        }
        for symbol in spy_symbols
    ]
    spy_member_hash = core._canonical_member_hash(
        [
            {
                "symbol": row["ticker"],
                "provider_symbol": row["provider_symbol"],
                "asset_class": row["asset_class"],
                "index": "S&P 500",
                "valid_from": row["valid_from"],
                "valid_to": None,
            }
            for row in spy_members
        ]
    )
    spy_source_id = "test-spy-refresh"
    spy_scope = "SPY tracker holdings refresh test proxy"
    monkeypatch.setitem(
        core._TRUSTED_SOURCE_ROOTS,
        spy_source_id,
        {
            "index": "S&P 500",
            "effective_date": "2026-08-24",
            "raw_artifact_hashes": (),
            "canonical_zip_member_names": spy_attestation["member_names"],
            "canonical_static_member_hashes": spy_attestation["static_member_hashes"],
            "canonical_schema_digest_sha256": spy_attestation["schema_digest_sha256"],
            "canonical_content_digest_sha256": spy_attestation["content_digest_sha256"],
            "canonical_symbol_set_hash_sha256": spy_attestation["symbol_set_hash_sha256"],
            "allow_future_same_semantic_set_dates": True,
            "maximum_source_age_days": 4,
            "transformation_id": "state-street-spy-holdings-parser-v1",
            "lineage_builder_id": "state-street-spy-holdings-parser-v1",
            "lineage_transformation_id": "exclude-cash-and-contra-holdings-v1",
            "lineage_schema_version": "dawnstrike.core_universe_lineage.v1",
            "reconstitution_id": "spy-holdings-2026-08-24",
            "membership_authority": "tracker_holdings_proxy",
            "official_index_authority": False,
            "source_scope": spy_scope,
            "source_uri": "https://example.test/spy.xlsx",
        },
    )
    proxy = {
        "source_id": spy_source_id,
        "source_uri": "https://example.test/spy.xlsx",
        "source_scope": spy_scope,
        "observed_at": "2026-08-26T12:00:00Z",
        "effective_date": "2026-08-24",
        "reconstitution_id": "spy-holdings-2026-08-24",
        "index_name": "S&P 500",
        "expected_count": 503,
        "completeness_verdict": "COMPLETE",
        "members": spy_members,
        "source_artifacts": [
            {
                "uri": "https://example.test/spy.xlsx",
                "path": "../spy.xlsx",
                "sha256": spy_digest,
            }
        ],
        "canonical_member_set_hash_sha256": spy_member_hash,
        "reconstitution_lineage": {
            "schema_version": "dawnstrike.core_universe_lineage.v1",
            "builder_id": "state-street-spy-holdings-parser-v1",
            "transformation_id": "exclude-cash-and-contra-holdings-v1",
            "reconstitution_id": "spy-holdings-2026-08-24",
            "effective_date": "2026-08-24",
            "input_artifact_hashes": [spy_digest],
            "canonical_member_set_hash_sha256": spy_member_hash,
        },
    }
    config = tmp_path / "config"
    config.mkdir()
    output = config / "luna_core_universe.json"
    output.write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.luna.core_universe_manifest_wrapper.v1",
                "manifests": [proxy],
            }
        ),
        encoding="utf-8",
    )
    ndx_path = tmp_path / "ndx.xlsx"
    ndx_path.write_bytes(ndx_payload)
    return output, ndx_path.read_bytes(), spy_payload


def test_sod_export_parser_accepts_exact_102_row_schema(ndx_symbols: list[str]) -> None:
    assert len(core._parse_nasdaq_sod_weightings_xlsx(_ndx_xlsx(ndx_symbols))) == 102
    assert "HONA" in core._parse_nasdaq_sod_weightings_xlsx(_ndx_xlsx(ndx_symbols))
    assert "EA" not in core._parse_nasdaq_sod_weightings_xlsx(_ndx_xlsx(ndx_symbols))


def test_hash_and_replay_use_the_same_single_captured_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    payload = _ndx_xlsx(ndx_symbols)
    path = tmp_path / "ndx.xlsx"
    path.write_bytes(payload)
    _install_test_root(monkeypatch, hashlib.sha256(payload).hexdigest())
    manifest = _manifest(tmp_path, ndx_symbols, payload=payload)
    original_read_bytes = Path.read_bytes
    reads: list[Path] = []

    def counted_read_bytes(candidate: Path) -> bytes:
        if candidate.resolve() == path.resolve():
            reads.append(candidate)
            if len(reads) > 1:
                # A second read would represent a different source snapshot.
                return _ndx_xlsx(["EA" if symbol == "HONA" else symbol for symbol in ndx_symbols])
        return original_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    contract = core.build_core_universe_contract(
        manifest, observed_at="2026-08-27T13:00:00Z", market_date="2026-08-27"
    )
    ndx_artifact = next(
        item for item in contract["source_artifacts"] if item["source_id"].startswith("nasdaq-")
    )
    assert reads == [path]
    assert ndx_artifact["source_binding"]["status"] == "VERIFIED"


def test_stable_workbook_root_rejects_rehashed_changed_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    baseline = _ndx_xlsx(ndx_symbols)
    _install_stable_test_root(monkeypatch, baseline, ndx_symbols)
    changed_symbols = ["EA" if symbol == "HONA" else symbol for symbol in ndx_symbols]
    changed_payload = _ndx_xlsx(changed_symbols)
    contract = core.build_core_universe_contract(
        _manifest(tmp_path, changed_symbols, payload=changed_payload),
        observed_at="2026-08-27T13:00:00Z",
        market_date="2026-08-27",
    )
    assert contract["status"] == "DATA_UNAVAILABLE"
    assert "source_binding_workbook_members_not_trusted" in contract["blockers"]
    assert "source_binding_workbook_content_not_trusted" in contract["blockers"]


def test_stable_root_accepts_same_content_with_volatile_zip_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    baseline = _ndx_xlsx(ndx_symbols)
    _install_stable_test_root(monkeypatch, baseline, ndx_symbols)
    rewritten = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(baseline)) as source,
        zipfile.ZipFile(rewritten, "w", compression=zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            replacement = zipfile.ZipInfo(info.filename, date_time=(2001, 2, 3, 4, 5, 6))
            replacement.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(replacement, source.read(info.filename))
    payload = rewritten.getvalue()
    contract = core.build_core_universe_contract(
        _manifest(tmp_path, ndx_symbols, payload=payload),
        observed_at="2026-08-27T13:00:00Z",
        market_date="2026-08-27",
    )
    ndx_artifact = contract["source_artifacts"][0]
    assert ndx_artifact["source_binding"]["status"] == "VERIFIED"
    assert "source_binding_raw_artifact_sizes_not_trusted" not in contract["blockers"]


def test_unknown_xlsx_structure_fails_closed_even_with_recomputed_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    baseline = _ndx_xlsx(ndx_symbols)
    _install_stable_test_root(monkeypatch, baseline, ndx_symbols)
    altered = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(baseline)) as source, zipfile.ZipFile(altered, "w") as target:
        for info in source.infolist():
            target.writestr(info.filename, source.read(info.filename))
        target.writestr("xl/unknown.xml", "<unknown />")
    contract = core.build_core_universe_contract(
        _manifest(tmp_path, ndx_symbols, payload=altered.getvalue()),
        observed_at="2026-08-27T13:00:00Z",
        market_date="2026-08-27",
    )
    assert contract["status"] == "DATA_UNAVAILABLE"
    assert any(
        item.startswith("source_binding_replay_failed:Nasdaq SOD workbook structure is unknown")
        for item in contract["blockers"]
    )


def test_lineage_and_scope_are_bound_to_the_trusted_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    payload = _ndx_xlsx(ndx_symbols)
    _install_test_root(monkeypatch, hashlib.sha256(payload).hexdigest())
    manifest = _manifest(tmp_path, ndx_symbols, payload=payload)
    manifest["source_scope"] = "attacker relabelled feed"
    manifest["reconstitution_id"] = "attacker-reconstitution"
    manifest["reconstitution_lineage"]["schema_version"] = "attacker-schema-v9"
    contract = core.build_core_universe_contract(
        manifest, observed_at="2026-08-27T13:00:00Z", market_date="2026-08-27"
    )
    assert contract["status"] == "DATA_UNAVAILABLE"
    assert "source_binding_source_scope_not_trusted" in contract["blockers"]
    assert "source_binding_reconstitution_id_not_trusted" in contract["blockers"]
    assert "source_binding_lineage_schema_mismatch" in contract["blockers"]
    artifact = contract["source_artifacts"][0]
    assert artifact["source_binding"]["status"] == "BLOCKED"
    assert artifact["source_scope"] == "Official Nasdaq-100 SOD Weightings export for 2026-08-27"


def test_generic_source_binding_error_blocks_and_propagates_to_each_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    payload = _ndx_xlsx(ndx_symbols)
    _install_test_root(monkeypatch, hashlib.sha256(payload).hexdigest())
    declared = ["EA" if symbol == "HONA" else symbol for symbol in ndx_symbols]
    contract = core.build_core_universe_contract(
        _manifest(tmp_path, declared, payload=payload),
        observed_at="2026-08-27T13:00:00Z",
        market_date="2026-08-27",
    )
    assert contract["index_verdicts"]["Nasdaq-100"]["status"] == "DATA_UNAVAILABLE"
    assert contract["index_verdicts"]["S&P 500"]["status"] == "DATA_UNAVAILABLE"
    assert "source_binding_membership_mismatch" in contract["index_verdicts"]["S&P 500"]["blockers"]


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
    assert any(item.startswith("source_binding_replay_failed:") for item in contract["blockers"])


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, _ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    prior = output.read_bytes()
    raw = tmp_path / "ndx.xlsx"
    changed = ["EA" if symbol == "HONA" else symbol for symbol in ndx_symbols]
    raw.write_bytes(_ndx_xlsx(changed))
    with pytest.raises(RuntimeError, match="not the governed currentness root"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=raw,
            spy_artifact=tmp_path / "spy.xlsx",
        )
    assert output.read_bytes() == prior
    assert not json.loads(output.read_text(encoding="utf-8")).get("generation_id")


def test_refresh_accepts_later_market_date_only_for_same_semantic_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, _ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    future_ndx = tmp_path / "ndx-2026-08-28.xlsx"
    future_ndx.write_bytes(_ndx_xlsx(ndx_symbols, company_prefix="Current Company"))
    assert future_ndx.read_bytes() != (tmp_path / "ndx.xlsx").read_bytes()
    future_spy = tmp_path / "spy-2026-08-27.xlsx"
    future_spy.write_bytes(
        _spy_xlsx(
            [f"S{number:03d}" for number in range(503)],
            effective_date="2026-08-27",
        )
    )
    result = refresh_script.refresh(
        state_root=tmp_path,
        proxy_manifest=None,
        ndx_artifact=future_ndx,
        spy_artifact=future_spy,
        market_date="2026-08-28",
    )
    assert result["status"] == "READY"
    assert result["market_date"] == "2026-08-28"
    installed = core.build_core_universe_contract(
        output,
        observed_at=result["observed_at"],
        market_date="2026-08-28",
    )
    assert installed["status"] == "READY"
    assert installed["effective_date"] == "2026-08-28"


def test_refresh_rejects_stale_spy_capture_and_preserves_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, _ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    prior = output.read_bytes()
    with pytest.raises(RuntimeError, match="SPY workbook is stale"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=tmp_path / "ndx.xlsx",
            spy_artifact=tmp_path / "spy.xlsx",
            market_date="2026-08-31",
        )
    assert output.read_bytes() == prior


def test_refresh_installs_and_revalidates_one_atomic_active_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    result = refresh_script.refresh(
        state_root=tmp_path,
        proxy_manifest=None,
        ndx_artifact=tmp_path / "ndx.xlsx",
        spy_artifact=tmp_path / "spy.xlsx",
    )
    pointer = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "READY"
    assert pointer["schema_version"] == core.ACTIVE_POINTER_SCHEMA_VERSION
    generation_manifest = (output.parent / pointer["manifest_path"]).resolve()
    assert generation_manifest.is_file()
    assert (
        hashlib.sha256(generation_manifest.read_bytes()).hexdigest() == pointer["manifest_sha256"]
    )
    generation = generation_manifest.parent
    installed_artifact = generation / "ndx-sod-2026-08-27.xlsx"
    assert installed_artifact.read_bytes() == ndx_payload
    installed = core.build_core_universe_contract(
        output, observed_at=result["observed_at"], market_date="2026-08-27"
    )
    assert installed["status"] == "READY"
    assert result["ndx_artifact"] == str(installed_artifact)


def test_refresh_reuses_same_content_addressed_generation_byte_identically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, _ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    first = refresh_script.refresh(
        state_root=tmp_path,
        proxy_manifest=None,
        ndx_artifact=tmp_path / "ndx.xlsx",
        spy_artifact=tmp_path / "spy.xlsx",
    )
    pointer_before = output.read_bytes()
    generations = sorted((output.parent / refresh_script.GENERATION_DIRECTORY).iterdir())
    second = refresh_script.refresh(
        state_root=tmp_path,
        proxy_manifest=None,
        ndx_artifact=tmp_path / "ndx.xlsx",
        spy_artifact=tmp_path / "spy.xlsx",
    )
    assert second["status"] == "READY"
    assert second["reused"] is True
    assert second["generation_id"] == first["generation_id"]
    assert second["generation_key"] == first["generation_key"]
    assert output.read_bytes() == pointer_before
    assert sorted((output.parent / refresh_script.GENERATION_DIRECTORY).iterdir()) == generations
    assert not (output.parent / refresh_script.REFRESH_LOCK_NAME).exists()


def test_concurrent_refresh_writer_is_rejected_without_pointer_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    entered = threading.Event()
    release = threading.Event()
    result: dict[str, object] = {}

    def slow_fetch(_url: str) -> bytes:
        entered.set()
        assert release.wait(timeout=5)
        return ndx_payload

    monkeypatch.setattr(refresh_script, "_fetch", slow_fetch)

    def run_first_refresh() -> None:
        try:
            result["value"] = refresh_script.refresh(
                state_root=tmp_path,
                proxy_manifest=None,
                ndx_artifact=None,
                spy_artifact=tmp_path / "spy.xlsx",
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["error"] = exc

    worker = threading.Thread(target=run_first_refresh)
    worker.start()
    assert entered.wait(timeout=5)
    with pytest.raises(RuntimeError, match="refresh already in progress"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=tmp_path / "ndx.xlsx",
            spy_artifact=tmp_path / "spy.xlsx",
        )
    release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert "error" not in result
    assert result["value"]["status"] == "READY"  # type: ignore[index]
    pointer = json.loads(output.read_text(encoding="utf-8"))
    assert pointer["schema_version"] == core.ACTIVE_POINTER_SCHEMA_VERSION
    assert not (output.parent / refresh_script.REFRESH_LOCK_NAME).exists()


def test_provably_dead_refresh_owner_is_archived_and_recovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, _ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    lock_path = output.parent / refresh_script.REFRESH_LOCK_NAME
    dead_owner = refresh_script._lock_owner_metadata()
    dead_owner.update(
        {
            "owner_token": "dead-owner",
            "pid": 999_999,
            "process_start_time": "1.000000",
        }
    )
    lock_path.write_text(
        json.dumps(dead_owner, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(refresh_script, "_process_is_live", lambda _pid: False)

    result = refresh_script.refresh(
        state_root=tmp_path,
        proxy_manifest=None,
        ndx_artifact=tmp_path / "ndx.xlsx",
        spy_artifact=tmp_path / "spy.xlsx",
    )

    assert result["status"] == "READY"
    assert not lock_path.exists()
    archived = list(output.parent.glob(f"{refresh_script.REFRESH_LOCK_NAME}.dead.*"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8"))["owner_token"] == ("dead-owner")


def test_refresh_rolls_back_active_pointer_when_post_swap_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, _ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    prior = output.read_bytes()
    original_build = refresh_script.build_core_universe_contract
    calls = 0

    def fail_after_install(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        contract = original_build(*args, **kwargs)
        if calls == 2:
            return {**contract, "status": "DATA_UNAVAILABLE", "reason": "injected failure"}
        return contract

    monkeypatch.setattr(refresh_script, "build_core_universe_contract", fail_after_install)
    with pytest.raises(RuntimeError, match="installed core manifest did not reach READY"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=tmp_path / "ndx.xlsx",
            spy_artifact=tmp_path / "spy.xlsx",
        )
    assert output.read_bytes() == prior
    assert json.loads(output.read_text(encoding="utf-8"))["manifests"][0]["source_id"] == (
        "test-spy-refresh"
    )


def test_refresh_changed_member_attestation_leaves_prior_generation_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, _ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    prior = output.read_bytes()
    changed = ["EA" if symbol == "HONA" else symbol for symbol in ndx_symbols]
    changed_path = tmp_path / "changed-ndx.xlsx"
    changed_path.write_bytes(_ndx_xlsx(changed, company_prefix="Current Company"))
    with pytest.raises(RuntimeError, match="not the governed currentness root"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=changed_path,
            spy_artifact=tmp_path / "spy.xlsx",
            market_date="2026-08-28",
        )
    assert output.read_bytes() == prior
    assert not json.loads(output.read_text(encoding="utf-8")).get("generation_id")


def test_refresh_source_download_failure_leaves_active_generation_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, _ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    first = refresh_script.refresh(
        state_root=tmp_path,
        proxy_manifest=None,
        ndx_artifact=tmp_path / "ndx.xlsx",
        spy_artifact=tmp_path / "spy.xlsx",
    )
    prior = output.read_bytes()

    def fail_fetch(_url: str) -> bytes:
        raise RuntimeError("source download failed: injected outage")

    monkeypatch.setattr(refresh_script, "_fetch", fail_fetch)
    with pytest.raises(RuntimeError, match="source download failed"):
        refresh_script.refresh(state_root=tmp_path, proxy_manifest=None, ndx_artifact=None)
    assert output.read_bytes() == prior
    assert json.loads(output.read_text(encoding="utf-8"))["generation_id"] == first["generation_id"]
