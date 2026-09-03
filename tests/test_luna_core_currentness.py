from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import threading
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from intraday_scanner.services import luna_core_universe_service as core
from scripts import refresh_luna_core_universe as refresh_script


def _make_directory_reparse(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            pytest.skip("directory reparse creation is unavailable on this host")
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-Command",
                (
                    "& { param($linkPath, $targetPath) "
                    "New-Item -ItemType Junction -Path $linkPath "
                    "-Target $targetPath -ErrorAction Stop | Out-Null }"
                ),
                str(link),
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("directory reparse creation is unavailable on this host")


def _remove_directory_reparse(path: Path) -> None:
    if not os.path.lexists(path):
        return
    try:
        path.unlink()
    except OSError:
        path.rmdir()


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


@pytest.mark.parametrize(
    "relative_link",
    [
        Path("config"),
        Path("config") / refresh_script.GENERATION_DIRECTORY,
    ],
)
def test_refresh_rejects_preexisting_state_descendant_reparse_without_external_writes(
    tmp_path: Path,
    relative_link: Path,
) -> None:
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    state.mkdir()
    outside.mkdir()
    link = state / relative_link
    link.parent.mkdir(parents=True, exist_ok=True)
    _make_directory_reparse(link, outside)

    try:
        with pytest.raises(RuntimeError, match="symlink|junction|reparse point"):
            refresh_script.refresh(
                state_root=state,
                proxy_manifest=None,
                ndx_artifact=None,
                market_date="2026-08-27",
                bootstrap_state_street_proxy=True,
            )
        assert list(outside.iterdir()) == []
    finally:
        _remove_directory_reparse(link)


def test_refresh_rejects_reparse_active_pointer_without_touching_target(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    config = state / "config"
    config.mkdir(parents=True)
    outside = tmp_path / "outside-pointer.json"
    sentinel = b"outside pointer must remain byte-identical"
    outside.write_bytes(sentinel)
    pointer = config / "luna_core_universe.json"
    try:
        pointer.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("file reparse creation is unavailable on this host")

    try:
        with pytest.raises(RuntimeError, match="symlink|junction|reparse point"):
            refresh_script.refresh(
                state_root=state,
                proxy_manifest=None,
                ndx_artifact=None,
                market_date="2026-08-27",
                bootstrap_state_street_proxy=True,
            )
        assert outside.read_bytes() == sentinel
    finally:
        pointer.unlink()


def test_refresh_safely_creates_an_absent_regular_config_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()

    def observe_boundary(**kwargs: object) -> dict[str, object]:
        config = Path(str(kwargs["state_root"])) / "config"
        assert config.is_dir()
        assert not config.is_symlink()
        refresh_script._assert_universe_state_layout(
            Path(str(kwargs["state_root"])),
            require_config=True,
        )
        return {"status": "READY"}

    monkeypatch.setattr(refresh_script, "_refresh_locked", observe_boundary)
    result = refresh_script.refresh(
        state_root=state,
        proxy_manifest=None,
        ndx_artifact=None,
        market_date="2026-08-27",
    )

    assert result == {"status": "READY"}
    assert (state / "config").is_dir()
    assert not (state / "config" / refresh_script.REFRESH_LOCK_NAME).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics only")
def test_windows_write_boundary_keeps_marker_immutable_and_atomic_replace_operational(
    tmp_path: Path,
) -> None:
    guarded = tmp_path / "guarded"
    moved = tmp_path / "guarded.moved"
    guarded.mkdir()
    target = guarded / "active.json"
    target.write_bytes(b"old")
    temporary = guarded / "active.tmp"
    temporary.write_bytes(b"new")

    with refresh_script._hold_directory_write_boundary(
        guarded,
        label="test guarded output",
    ):
        marker = guarded / refresh_script.DIRECTORY_BOUNDARY_MARKER_NAME
        assert marker.read_bytes() == refresh_script.DIRECTORY_BOUNDARY_MARKER_BYTES
        assert not marker.is_symlink()
        with pytest.raises(OSError):
            marker.write_bytes(b"hostile")
        with pytest.raises(OSError):
            marker.unlink()
        with pytest.raises(OSError):
            guarded.rename(moved)
        os.replace(temporary, target)
        assert target.read_bytes() == b"new"

    assert guarded.is_dir()
    assert not moved.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse control only")
def test_windows_write_boundary_denies_in_place_mount_point_conversion(
    tmp_path: Path,
) -> None:
    import ctypes
    from ctypes import wintypes

    guarded = tmp_path / "guarded"
    outside = tmp_path / "outside"
    guarded.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside bytes must remain unchanged")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    device_io = kernel32.DeviceIoControl
    device_io.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    device_io.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    with refresh_script._hold_directory_write_boundary(
        guarded,
        label="test guarded output",
    ):
        handle = create_file(
            str(guarded),
            0x40000000,
            0x1 | 0x2 | 0x4,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        assert handle != ctypes.c_void_p(-1).value
        try:
            substitute = ("\\??\\" + str(outside)).encode("utf-16-le")
            display = str(outside).encode("utf-16-le")
            path_buffer = substitute + b"\x00\x00" + display + b"\x00\x00"
            reparse_data = (
                struct.pack(
                    "<IHHHHHH",
                    0xA0000003,
                    8 + len(path_buffer),
                    0,
                    0,
                    len(substitute),
                    len(substitute) + 2,
                    len(display),
                )
                + path_buffer
            )
            buffer = ctypes.create_string_buffer(reparse_data)
            returned = wintypes.DWORD()
            converted = bool(
                device_io(
                    handle,
                    0x000900A4,
                    buffer,
                    len(reparse_data),
                    None,
                    0,
                    ctypes.byref(returned),
                    None,
                )
            )
            error = 0 if converted else ctypes.get_last_error()
            if converted:  # pragma: no cover - cleanup before surfacing a failed invariant
                delete_data = struct.pack("<IHH", 0xA0000003, 0, 0)
                delete_buffer = ctypes.create_string_buffer(delete_data)
                assert device_io(
                    handle,
                    0x000900AC,
                    delete_buffer,
                    len(delete_data),
                    None,
                    0,
                    ctypes.byref(returned),
                    None,
                )
            assert converted is False
            assert error == 145  # ERROR_DIR_NOT_EMPTY: the held marker is effective.
        finally:
            close_handle(handle)

    assert sentinel.read_bytes() == b"outside bytes must remain unchanged"
    assert not guarded.is_symlink()


def test_write_boundary_rejects_a_tampered_marker_before_output(
    tmp_path: Path,
) -> None:
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    marker = guarded / refresh_script.DIRECTORY_BOUNDARY_MARKER_NAME
    marker.write_bytes(b"attacker-controlled")

    with pytest.raises(RuntimeError, match="boundary marker has unexpected bytes"):
        with refresh_script._hold_directory_write_boundary(
            guarded,
            label="test guarded output",
        ):
            pytest.fail("tampered marker was admitted")


def test_refresh_blocks_config_swap_after_admission_without_external_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    config = state / "config"
    outside = tmp_path / "outside"
    saved = state / "config.saved"
    config.mkdir(parents=True)
    outside.mkdir()
    swapped = False

    def attempt_swap(**_kwargs: object) -> dict[str, object]:
        nonlocal swapped
        try:
            config.rename(saved)
            _make_directory_reparse(config, outside)
            swapped = True
        except OSError:
            # Windows must deny the rename while the no-delete directory
            # handle is held.  POSIX permits it and the post-hold identity
            # check below must still reject the operation.
            pass
        return {"status": "READY"}

    monkeypatch.setattr(refresh_script, "_refresh_locked", attempt_swap)
    try:
        if os.name == "nt":
            result = refresh_script.refresh(
                state_root=state,
                proxy_manifest=None,
                ndx_artifact=None,
                market_date="2026-08-27",
            )
            assert result == {"status": "READY"}
            assert swapped is False
        else:
            with pytest.raises(RuntimeError, match="config root.*changed"):
                refresh_script.refresh(
                    state_root=state,
                    proxy_manifest=None,
                    ndx_artifact=None,
                    market_date="2026-08-27",
                )
            assert swapped is True
        assert list(outside.iterdir()) == []
    finally:
        if swapped:
            _remove_directory_reparse(config)
            saved.rename(config)


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


def test_explicit_state_street_bootstrap_installs_a_missing_active_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, ndx_payload, spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    output.unlink()
    monkeypatch.setattr(refresh_script, "SPY_SOURCE_ID", "test-spy-refresh")
    monkeypatch.setattr(
        refresh_script,
        "STATE_STREET_SPY_HOLDINGS_URL",
        "https://example.test/spy.xlsx",
    )

    with pytest.raises(RuntimeError, match="proxy manifest missing"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=tmp_path / "ndx.xlsx",
            spy_artifact=tmp_path / "spy.xlsx",
            market_date="2026-08-27",
        )

    result = refresh_script.refresh(
        state_root=tmp_path,
        proxy_manifest=None,
        ndx_artifact=tmp_path / "ndx.xlsx",
        spy_artifact=tmp_path / "spy.xlsx",
        market_date="2026-08-27",
        bootstrap_state_street_proxy=True,
    )

    assert result["status"] == "READY"
    assert result["proxy_bootstrapped"] is True
    assert result["ndx_sha256"] == hashlib.sha256(ndx_payload).hexdigest()
    assert result["spy_sha256"] == hashlib.sha256(spy_payload).hexdigest()
    assert result["ndx_member_count"] == 102
    assert result["spy_member_count"] == 503
    installed = core.build_core_universe_contract(
        output,
        observed_at=result["observed_at"],
        market_date="2026-08-27",
    )
    assert installed["status"] == "READY"


def test_state_street_bootstrap_never_substitutes_for_a_missing_explicit_proxy(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="does not accept an explicit proxy manifest"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=tmp_path / "missing-proxy.json",
            ndx_artifact=tmp_path / "ndx.xlsx",
            spy_artifact=tmp_path / "spy.xlsx",
            market_date="2026-08-27",
            bootstrap_state_street_proxy=True,
        )


def test_state_street_bootstrap_rejects_an_existing_pointer_byte_identically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, _ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    prior = output.read_bytes()

    with pytest.raises(RuntimeError, match="requires a completely absent active pointer"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=tmp_path / "ndx.xlsx",
            spy_artifact=tmp_path / "spy.xlsx",
            market_date="2026-08-27",
            bootstrap_state_street_proxy=True,
        )

    assert output.read_bytes() == prior

    malformed = b"{not-json"
    output.write_bytes(malformed)
    with pytest.raises(RuntimeError, match="requires a completely absent active pointer"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=tmp_path / "ndx.xlsx",
            spy_artifact=tmp_path / "spy.xlsx",
            market_date="2026-08-27",
            bootstrap_state_street_proxy=True,
        )
    assert output.read_bytes() == malformed


def test_state_street_bootstrap_rejects_non_file_and_dangling_output_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    output = config / "luna_core_universe.json"
    output.mkdir()
    with pytest.raises(RuntimeError, match="active pointer is not a regular file"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=tmp_path / "ndx.xlsx",
            market_date="2026-08-27",
            bootstrap_state_street_proxy=True,
        )
    assert output.is_dir()

    output.rmdir()
    original_lexists = refresh_script.os.path.lexists
    monkeypatch.setattr(
        refresh_script.os.path,
        "lexists",
        lambda path: Path(path) == output or original_lexists(path),
    )
    with pytest.raises(RuntimeError, match="requires a completely absent active pointer"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=tmp_path / "ndx.xlsx",
            market_date="2026-08-27",
            bootstrap_state_street_proxy=True,
        )


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


def test_refresh_accepts_later_market_date_only_from_the_exact_dated_download(
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
    requested: list[str] = []

    def fetch_exact_date(url: str) -> bytes:
        requested.append(url)
        return future_ndx.read_bytes()

    monkeypatch.setattr(refresh_script, "_fetch", fetch_exact_date)
    result = refresh_script.refresh(
        state_root=tmp_path,
        proxy_manifest=None,
        ndx_artifact=None,
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
    assert requested == [core._nasdaq_sod_url_for_date("2026-08-28")]


def test_refresh_rejects_a_later_date_explicit_artifact_without_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, _ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    prior = output.read_bytes()
    with pytest.raises(RuntimeError, match="no authenticated date provenance"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=tmp_path / "ndx.xlsx",
            spy_artifact=tmp_path / "spy.xlsx",
            market_date="2026-08-28",
        )
    assert output.read_bytes() == prior


def test_refresh_rejects_stale_spy_capture_and_preserves_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    prior = output.read_bytes()
    monkeypatch.setattr(refresh_script, "_fetch", lambda _url: ndx_payload)
    with pytest.raises(RuntimeError, match="SPY workbook is stale"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=None,
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
    assert second["spy_sha256"] == first["spy_sha256"]
    assert second["spy_member_count"] == first["spy_member_count"]
    assert second["spy_effective_date"] == first["spy_effective_date"]
    assert output.read_bytes() == pointer_before
    assert sorted((output.parent / refresh_script.GENERATION_DIRECTORY).iterdir()) == generations
    assert not (output.parent / refresh_script.REFRESH_LOCK_NAME).exists()


def test_refresh_preserves_a_partial_inactive_generation_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ndx_symbols: list[str],
) -> None:
    output, _ndx_payload, _spy_payload = _refresh_fixture(tmp_path, monkeypatch, ndx_symbols)
    legacy_wrapper = output.read_bytes()
    first = refresh_script.refresh(
        state_root=tmp_path,
        proxy_manifest=None,
        ndx_artifact=tmp_path / "ndx.xlsx",
        spy_artifact=tmp_path / "spy.xlsx",
    )
    pointer = json.loads(output.read_text(encoding="utf-8"))
    generation = (output.parent / pointer["manifest_path"]).resolve().parent
    output.write_bytes(legacy_wrapper)
    (generation / "luna_core_universe.json").unlink()
    (generation / "operator-note.txt").write_text("preserve me", encoding="utf-8")

    second = refresh_script.refresh(
        state_root=tmp_path,
        proxy_manifest=None,
        ndx_artifact=tmp_path / "ndx.xlsx",
        spy_artifact=tmp_path / "spy.xlsx",
    )

    assert second["status"] == "READY"
    assert second["generation_id"] == first["generation_id"]
    orphans = list(generation.parent.glob(f"{generation.name}.orphan.*"))
    assert len(orphans) == 1
    assert (orphans[0] / "operator-note.txt").read_text(encoding="utf-8") == "preserve me"
    assert (generation / "luna_core_universe.json").is_file()


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

    if os.name != "nt":
        with pytest.raises(RuntimeError, match="refresh already in progress"):
            refresh_script.refresh(
                state_root=tmp_path,
                proxy_manifest=None,
                ndx_artifact=tmp_path / "ndx.xlsx",
                spy_artifact=tmp_path / "spy.xlsx",
            )
        assert json.loads(lock_path.read_text(encoding="utf-8"))["owner_token"] == "dead-owner"
        assert not list(output.parent.glob(f"{refresh_script.REFRESH_LOCK_NAME}.dead.*"))
        return

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


def test_refresh_lock_release_never_unlinks_a_replacement_after_handle_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    lock_path = config / refresh_script.REFRESH_LOCK_NAME
    displaced = config / "displaced-original.lock"
    replacement = config / "replacement-live.lock"
    real_open = refresh_script._open_refresh_lock_handle
    replacement_bytes = b""
    raced = False

    @contextmanager
    def publish_replacement_after_close(*args: object, **kwargs: object):
        nonlocal raced, replacement_bytes
        with real_open(*args, **kwargs) as handle:
            yield handle
            handle.seek(0)
            replacement_bytes = handle.read()
        raced = True
        # A contender may publish immediately after the exact owner handle is
        # closed. Cleanup must already have removed that exact identity; it may
        # never token-match and unlink this replacement by pathname.
        if os.path.lexists(lock_path):
            os.replace(lock_path, displaced)
        replacement.write_bytes(replacement_bytes)
        os.replace(replacement, lock_path)

    monkeypatch.setattr(
        refresh_script,
        "_open_refresh_lock_handle",
        publish_replacement_after_close,
    )

    with refresh_script._refresh_lock(config):
        pass

    assert raced is True
    assert not displaced.exists()
    assert lock_path.read_bytes() == replacement_bytes


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound rename only")
def test_stale_lock_archival_blocks_replacement_after_exact_handle_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    lock_path = config / refresh_script.REFRESH_LOCK_NAME
    dead_owner = refresh_script._lock_owner_metadata()
    dead_owner.update({"owner_token": "dead-owner", "pid": 999_999})
    lock_path.write_text(
        json.dumps(dead_owner, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    replacement = config / "replacement-live.lock"
    replacement.write_bytes(b"replacement-live-owner\n")
    real_rename = refresh_script._windows_rename_from_handle_no_replace
    attempted = False
    blocked = False

    def attack_after_admission(handle: object, destination: Path) -> None:
        nonlocal attempted, blocked
        attempted = True
        try:
            os.replace(replacement, lock_path)
        except OSError:
            blocked = True
        real_rename(handle, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(refresh_script, "_lock_owner_is_dead", lambda _metadata: True)
    monkeypatch.setattr(
        refresh_script,
        "_windows_rename_from_handle_no_replace",
        attack_after_admission,
    )

    assert refresh_script._archive_provably_dead_lock(lock_path) is True

    assert attempted is True
    assert blocked is True
    assert not lock_path.exists()
    assert replacement.read_bytes() == b"replacement-live-owner\n"
    archived = list(config.glob(f"{refresh_script.REFRESH_LOCK_NAME}.dead.*"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8"))["owner_token"] == "dead-owner"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound rename only")
def test_stale_lock_archive_destination_is_no_clobber(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    lock_path = config / refresh_script.REFRESH_LOCK_NAME
    lock_path.write_bytes(b"dead-owner\n")
    destination = config / "dead-archive"
    destination.write_bytes(b"third-contender\n")

    with refresh_script._open_refresh_lock_handle(
        lock_path,
        label="test stale lock",
        exact_namespace_mutation=True,
    ) as handle:
        with pytest.raises(FileExistsError):
            refresh_script._windows_rename_from_handle_no_replace(handle, destination)

    assert lock_path.read_bytes() == b"dead-owner\n"
    assert destination.read_bytes() == b"third-contender\n"


@pytest.mark.parametrize(
    "prior_bytes",
    [pytest.param(b"prior-pointer\n", id="existing"), pytest.param(None, id="absent")],
)
def test_atomic_output_temp_swap_is_blocked_or_restores_exact_prior_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_bytes: bytes | None,
) -> None:
    destination = tmp_path / "luna_core_universe.json"
    prior_identity: os.stat_result | None = None
    if prior_bytes is not None:
        destination.write_bytes(prior_bytes)
        prior_identity = os.lstat(destination)
    hostile = tmp_path / "hostile-temporary"
    hostile.write_bytes(b"hostile-pointer\n")
    expected = b"admitted-pointer\n"

    if os.name == "nt":
        real_replace = refresh_script._windows_replace_from_handle
        attempted = False
        blocked = False

        def attack_after_source_admission(handle: object, target: Path) -> None:
            nonlocal attempted, blocked
            attempted = True
            temporaries = list(tmp_path.glob(f".{destination.name}.*.tmp"))
            assert len(temporaries) == 1
            try:
                os.replace(hostile, temporaries[0])
            except PermissionError:
                blocked = True
            real_replace(handle, target)  # type: ignore[arg-type]

        monkeypatch.setattr(
            refresh_script,
            "_windows_replace_from_handle",
            attack_after_source_admission,
        )
        refresh_script._replace_bytes(destination, expected)
        assert attempted is True
        assert blocked is True
        assert destination.read_bytes() == expected
        return

    if not refresh_script.sys.platform.startswith("linux"):
        pytest.skip("exact POSIX rollback requires Linux renameat2")

    real_renameat2 = refresh_script._linux_renameat2
    attacked = False

    def swap_after_source_admission(source: Path, target: Path, flags: int) -> bool:
        nonlocal attacked
        if not attacked and target == destination:
            attacked = True
            os.replace(hostile, source)
        return real_renameat2(source, target, flags)

    monkeypatch.setattr(refresh_script, "_linux_renameat2", swap_after_source_admission)
    with pytest.raises(RuntimeError, match="replaced during commit"):
        refresh_script._replace_bytes(destination, expected)

    assert attacked is True
    if prior_bytes is None:
        assert not os.path.lexists(destination)
    else:
        assert destination.read_bytes() == prior_bytes
        assert prior_identity is not None
        assert refresh_script._same_file_identity(prior_identity, os.lstat(destination))


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
    failed_generations = list(
        (output.parent / refresh_script.GENERATION_DIRECTORY).glob("ndx-sod-*")
    )
    assert len(failed_generations) == 1

    retry = refresh_script.refresh(
        state_root=tmp_path,
        proxy_manifest=None,
        ndx_artifact=tmp_path / "ndx.xlsx",
        spy_artifact=tmp_path / "spy.xlsx",
    )
    assert retry["status"] == "READY"
    assert json.loads(output.read_text(encoding="utf-8"))["generation_id"] == retry["generation_id"]
    orphans = list(
        (output.parent / refresh_script.GENERATION_DIRECTORY).glob(
            f"{retry['generation_id']}.orphan.*"
        )
    )
    assert len(orphans) == 1
    assert (orphans[0] / "luna_core_universe.json").is_file()


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
    monkeypatch.setattr(refresh_script, "_fetch", lambda _url: changed_path.read_bytes())
    with pytest.raises(RuntimeError, match="not the governed currentness root"):
        refresh_script.refresh(
            state_root=tmp_path,
            proxy_manifest=None,
            ndx_artifact=None,
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
