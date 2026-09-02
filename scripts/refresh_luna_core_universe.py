"""Refresh the governed Luna core manifest from an exact current NDX export.

The refresh is staged and validated before the manifest pointer is replaced.
An unavailable, stale, malformed, or unexpected source leaves the previous
manifest untouched.  This script only writes the caller-provided state root;
it does not touch broker configuration or the runtime checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.services.luna_core_universe_service import (
    _TRUSTED_SOURCE_ROOTS,
    ACTIVE_POINTER_SCHEMA_VERSION,
    STATE_STREET_SPY_HOLDINGS_URL,
    _active_pointer_target,
    _canonical_member_hash,
    _nasdaq_sod_url_for_date,
    _parse_nasdaq_sod_weightings_xlsx_with_attestation,
    _parse_spy_holdings_xlsx_with_attestation,
    build_core_universe_contract,
    read_core_universe_manifest,
)

MAX_DOWNLOAD_BYTES = 2_000_000
NDX_SOURCE_ID = "nasdaq-ndx-point-in-time-2026-08-27"
SPY_SOURCE_ID = "state-street-spy-holdings-proxy-2026-08-24"
GENERATION_DIRECTORY = "luna_core_universe_generations"
# The release root anchors trust, but is not a recurring-session gate.  A
# requested later date is accepted only when the fresh official source still
# replays to this root's exact governed schema/content/member set.
RELEASE_ANCHOR_MARKET_DATE = "2026-08-27"
# Compatibility alias for callers that imported the old constant.  It is not
# used to reject a requested market date.
SUPPORTED_MARKET_DATE = RELEASE_ANCHOR_MARKET_DATE
REFRESH_LOCK_NAME = ".luna_core_universe.refresh.lock"
REFRESH_LOCK_SCHEMA_VERSION = "dawnstrike.luna.core_universe_refresh_lock.v1"
SPY_ARTIFACT_NAME = "spy-holdings.xlsx"


def _normalise_market_date(value: str | None) -> str:
    requested = value or RELEASE_ANCHOR_MARKET_DATE
    try:
        return date.fromisoformat(str(requested)).isoformat()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"market date must be an ISO date: {requested}") from exc


def _ndx_artifact_name(market_date: str) -> str:
    return f"ndx-sod-{market_date}.xlsx"


def _source_scope(root: dict[str, object], market_date: str) -> str:
    template = str(root.get("source_scope_template") or "").strip()
    if template:
        return template.format(market_date=market_date)
    return str(root.get("source_scope") or "").strip()


def _source_url(root: dict[str, object], market_date: str) -> str:
    if root.get("source_uri_template"):
        return _nasdaq_sod_url_for_date(market_date)
    return str(root.get("source_uri") or "").strip()


def _fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "Dawnstrike/1 core-universe refresh"})
    try:
        with urlopen(request, timeout=30) as response:  # nosec B310 - fixed HTTPS roots below
            if response.status != 200:
                raise RuntimeError(f"source returned HTTP {response.status}")
            payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    except (OSError, URLError) as exc:
        raise RuntimeError(f"source download failed: {exc}") from exc
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError("source download exceeded bounded size")
    return payload


def _read_json(path: Path) -> dict[str, object]:
    try:
        parsed = read_core_universe_manifest(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"proxy manifest unreadable: {exc}") from exc
    return parsed


def _resolve_proxy_paths(child: dict[str, object], base: Path) -> dict[str, object]:
    """Make retained proxy evidence paths independent of staging location."""

    resolved = dict(child)
    for key in ("raw_artifact", "raw_artifact_path"):
        path = resolved.get(key)
        if isinstance(path, str) and path and not Path(path).is_absolute():
            resolved[key] = str((base / path).resolve())
    entries = resolved.get("source_artifacts") or resolved.get("raw_artifacts")
    if isinstance(entries, list):
        copied: list[object] = []
        for entry in entries:
            if not isinstance(entry, dict):
                copied.append(entry)
                continue
            item = dict(entry)
            for key in ("path", "file", "local_path"):
                path = item.get(key)
                if isinstance(path, str) and path and not Path(path).is_absolute():
                    item[key] = str((base / path).resolve())
            copied.append(item)
        resolved["source_artifacts"] = copied
    if str(resolved.get("source_id") or "") == "state-street-spy-holdings-proxy-2026-08-24":
        resolved["source_uri"] = STATE_STREET_SPY_HOLDINGS_URL
        entries = resolved.get("source_artifacts")
        if isinstance(entries, list):
            resolved["source_artifacts"] = [
                {
                    **entry,
                    "uri": STATE_STREET_SPY_HOLDINGS_URL,
                }
                if isinstance(entry, dict)
                else entry
                for entry in entries
            ]
    return resolved


def _canonical_hash(records: list[dict[str, object]], *, effective_date: str) -> str:
    return _canonical_member_hash(
        [
            {
                "symbol": row["symbol"],
                "provider_symbol": row["provider_symbol"],
                "asset_class": row["asset_class"],
                "index": "Nasdaq-100",
                "valid_from": effective_date,
                "valid_to": None,
            }
            for row in records
        ]
    )


def _attest_ndx_payload(
    payload: bytes,
    *,
    market_date: str,
) -> tuple[list[str], dict[str, object]]:
    root = _TRUSTED_SOURCE_ROOTS[NDX_SOURCE_ID]
    try:
        symbols, attestation = _parse_nasdaq_sod_weightings_xlsx_with_attestation(
            payload, effective_date=market_date
        )
    except ValueError as exc:
        raise RuntimeError(
            f"NDX workbook is not the governed currentness root for {market_date}: {exc}"
        ) from exc
    expected_names = root.get("canonical_zip_member_names")
    expected_hashes = root.get("canonical_zip_member_hashes")
    expected_static = root.get("canonical_static_member_hashes")
    expected_content = str(root.get("canonical_content_digest_sha256") or "").lower()
    root_effective = str(root.get("effective_date") or "")
    if expected_names and list(attestation["member_names"]) != list(expected_names):
        raise RuntimeError(
            "NDX workbook is not the governed currentness root for "
            f"{market_date}: structure mismatch"
        )
    if expected_static and attestation["static_member_hashes"] != dict(expected_static):
        raise RuntimeError(
            "NDX workbook is not the governed currentness root for "
            f"{market_date}: static member mismatch"
        )
    if (
        market_date == root_effective
        and expected_hashes
        and attestation["member_hashes"] != dict(expected_hashes)
    ):
        raise RuntimeError(
            f"NDX workbook is not the governed currentness root for {market_date}: member mismatch"
        )
    if (
        market_date == root_effective
        and expected_content
        and attestation["content_digest_sha256"] != expected_content
    ):
        raise RuntimeError(
            f"NDX workbook is not the governed currentness root for {market_date}: content mismatch"
        )
    records: list[dict[str, object]] = [
        {
            "ticker": symbol,
            "provider_symbol": symbol,
            "asset_class": "common_stock",
            "index_memberships": ["Nasdaq-100"],
            "valid_from": market_date,
        }
        for symbol in symbols
    ]
    member_hash = _canonical_hash(
        [
            {
                "symbol": row["ticker"],
                "provider_symbol": row["provider_symbol"],
                "asset_class": row["asset_class"],
            }
            for row in records
        ],
        effective_date=market_date,
    )
    expected_member_set = str(root.get("canonical_member_set_hash_sha256") or "").lower()
    expected_symbol_set = str(root.get("canonical_symbol_set_hash_sha256") or "").lower()
    if expected_symbol_set and attestation.get("symbol_set_hash_sha256") != expected_symbol_set:
        raise RuntimeError(
            "NDX workbook is not the governed currentness root for "
            f"{market_date}: member set mismatch"
        )
    if (
        expected_member_set
        and market_date == str(root.get("effective_date") or "")
        and member_hash != expected_member_set
    ):
        raise RuntimeError(
            "NDX workbook is not the governed currentness root for "
            f"{market_date}: member set mismatch"
        )
    attestation["member_set_hash_sha256"] = member_hash
    return symbols, attestation


def _ndx_manifest(
    *,
    artifact_path: Path,
    payload: bytes,
    observed_at: str,
    market_date: str,
    parsed: tuple[list[str], dict[str, object]] | None = None,
) -> dict[str, object]:
    symbols, attestation = parsed or _attest_ndx_payload(payload, market_date=market_date)
    root = _TRUSTED_SOURCE_ROOTS[NDX_SOURCE_ID]
    source_uri = _source_url(root, market_date)
    records: list[dict[str, object]] = [
        {
            "ticker": symbol,
            "provider_symbol": symbol,
            "asset_class": "common_stock",
            "index_memberships": ["Nasdaq-100"],
            "valid_from": market_date,
        }
        for symbol in symbols
    ]
    member_hash = str(attestation["member_set_hash_sha256"])
    raw_hash = hashlib.sha256(payload).hexdigest()
    return {
        "source_id": NDX_SOURCE_ID,
        "source_uri": source_uri,
        "source_scope": _source_scope(root, market_date),
        "observed_at": observed_at,
        "effective_date": market_date,
        "reconstitution_id": root["reconstitution_id"],
        "index_name": "Nasdaq-100",
        "expected_count": 102,
        "completeness_verdict": "COMPLETE",
        "members": records,
        "canonical_zip_member_names": attestation["member_names"],
        "canonical_zip_member_hashes": attestation["member_hashes"],
        "canonical_static_member_hashes": attestation["static_member_hashes"],
        "canonical_content_digest_sha256": attestation["content_digest_sha256"],
        "canonical_member_set_hash_sha256": member_hash,
        "canonical_symbol_set_hash_sha256": attestation["symbol_set_hash_sha256"],
        "source_artifacts": [
            {
                "uri": source_uri,
                "path": str(artifact_path),
                "sha256": raw_hash,
                "byte_count": len(payload),
            }
        ],
        "reconstitution_lineage": {
            "schema_version": "dawnstrike.core_universe_lineage.v1",
            "builder_id": root["lineage_builder_id"],
            "transformation_id": root["lineage_transformation_id"],
            "reconstitution_id": root["reconstitution_id"],
            "effective_date": market_date,
            "input_artifact_hashes": [raw_hash],
            "canonical_member_set_hash_sha256": member_hash,
        },
    }


def _attest_spy_payload(
    payload: bytes,
    *,
    source_id: str,
    market_date: str,
) -> tuple[list[str], str, dict[str, object]]:
    """Capture a fresh State Street proxy only when its governed set matches."""

    root = _TRUSTED_SOURCE_ROOTS.get(source_id)
    if root is None:
        raise RuntimeError(f"SPY source trust root unknown: {source_id}")
    try:
        symbols, source_effective, attestation = _parse_spy_holdings_xlsx_with_attestation(
            [payload]
        )
    except (OSError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"SPY workbook is not a governed holdings capture: {exc}") from exc
    root_effective = str(root.get("effective_date") or "")
    maximum_age = int(root.get("maximum_source_age_days") or 0)
    try:
        source_age = (date.fromisoformat(market_date) - date.fromisoformat(source_effective)).days
    except ValueError:
        source_age = -1
    if (
        not root_effective
        or source_effective < root_effective
        or source_effective > market_date
        or maximum_age <= 0
        or source_age < 0
        or source_age > maximum_age
    ):
        raise RuntimeError(
            "SPY workbook is stale or future-dated for market date "
            f"{market_date}: source effective date {source_effective}"
        )
    expected_names = root.get("canonical_zip_member_names")
    if expected_names and list(attestation["member_names"]) != list(expected_names):
        raise RuntimeError("SPY workbook is not the governed holdings schema: structure mismatch")
    expected_static = root.get("canonical_static_member_hashes")
    if expected_static and attestation["static_member_hashes"] != dict(expected_static):
        raise RuntimeError("SPY workbook is not the governed holdings schema: member mismatch")
    expected_schema = str(root.get("canonical_schema_digest_sha256") or "").lower()
    if expected_schema and attestation["schema_digest_sha256"] != expected_schema:
        raise RuntimeError("SPY workbook is not the governed holdings schema: schema mismatch")
    expected_content = str(root.get("canonical_content_digest_sha256") or "").lower()
    if expected_content and attestation["content_digest_sha256"] != expected_content:
        raise RuntimeError("SPY workbook is not the governed holdings root: content mismatch")
    expected_symbols = str(root.get("canonical_symbol_set_hash_sha256") or "").lower()
    if expected_symbols and attestation["symbol_set_hash_sha256"] != expected_symbols:
        raise RuntimeError("SPY workbook is not the governed holdings root: member set mismatch")
    trusted_raw = root.get("raw_artifact_hashes")
    if trusted_raw and [hashlib.sha256(payload).hexdigest()] != list(trusted_raw):
        raise RuntimeError("SPY workbook is not the governed holdings root: raw digest mismatch")
    return symbols, source_effective, attestation


def _spy_manifest(
    *,
    artifact_path: Path,
    payload: bytes,
    observed_at: str,
    source_id: str,
    source_uri: str,
    parsed: tuple[list[str], str, dict[str, object]],
) -> dict[str, object]:
    symbols, source_effective, attestation = parsed
    root = _TRUSTED_SOURCE_ROOTS[source_id]
    records: list[dict[str, object]] = [
        {
            "ticker": symbol,
            "provider_symbol": symbol,
            "asset_class": "common_stock",
            "index_memberships": ["S&P 500"],
            "valid_from": source_effective,
        }
        for symbol in symbols
    ]
    canonical = [
        {
            "symbol": row["ticker"],
            "provider_symbol": row["provider_symbol"],
            "asset_class": row["asset_class"],
            "index": "S&P 500",
            "valid_from": source_effective,
            "valid_to": None,
        }
        for row in records
    ]
    member_hash = _canonical_member_hash(canonical)
    raw_hash = hashlib.sha256(payload).hexdigest()
    return {
        "source_id": source_id,
        "source_uri": source_uri,
        "source_scope": root["source_scope"],
        "observed_at": observed_at,
        "effective_date": source_effective,
        "reconstitution_id": root["reconstitution_id"],
        "index_name": "S&P 500",
        "expected_count": 503,
        "completeness_verdict": "COMPLETE",
        "members": records,
        "canonical_zip_member_names": attestation["member_names"],
        "canonical_static_member_hashes": attestation["static_member_hashes"],
        "canonical_schema_digest_sha256": attestation["schema_digest_sha256"],
        "canonical_content_digest_sha256": attestation["content_digest_sha256"],
        "canonical_symbol_set_hash_sha256": attestation["symbol_set_hash_sha256"],
        "canonical_member_set_hash_sha256": member_hash,
        "source_artifacts": [
            {
                "uri": source_uri,
                "path": str(artifact_path),
                "sha256": raw_hash,
                "byte_count": len(payload),
            }
        ],
        "reconstitution_lineage": {
            "schema_version": root["lineage_schema_version"],
            "builder_id": root["lineage_builder_id"],
            "transformation_id": root["lineage_transformation_id"],
            "reconstitution_id": root["reconstitution_id"],
            "effective_date": source_effective,
            "input_artifact_hashes": [raw_hash],
            "canonical_member_set_hash_sha256": member_hash,
        },
    }


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def _refresh_lock(config_root: Path):
    """Serialize refreshes with identity-bound, recoverable owner metadata."""

    lock_path = config_root / REFRESH_LOCK_NAME
    config_root.mkdir(parents=True, exist_ok=True)
    owner = _lock_owner_metadata()
    descriptor: int | None = None
    for _attempt in range(3):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError as exc:
            if not _archive_provably_dead_lock(lock_path):
                raise RuntimeError("core universe refresh already in progress") from exc
        except OSError as exc:
            raise RuntimeError(f"core universe refresh lock unavailable: {exc}") from exc
    if descriptor is None:
        raise RuntimeError("core universe refresh lock could not be acquired")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(owner, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            current = None
        if isinstance(current, dict) and current.get("owner_token") == owner["owner_token"]:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                # The refresh result is still governed by the atomic pointer;
                # leave a lock owned by this process visible rather than
                # allowing a concurrent writer after cleanup failure.
                raise RuntimeError(f"could not remove refresh lock: {lock_path}") from exc


def _process_start_time(pid: int) -> str | None:
    """Return a PID-reuse-resistant process creation marker when available."""

    try:
        import psutil  # type: ignore[import-not-found]

        return f"{float(psutil.Process(pid).create_time()):.6f}"
    except (ImportError, OSError, ValueError):
        return None


def _lock_owner_metadata() -> dict[str, object]:
    pid = os.getpid()
    return {
        "schema_version": REFRESH_LOCK_SCHEMA_VERSION,
        "owner_token": uuid.uuid4().hex,
        "pid": pid,
        "process_start_time": _process_start_time(pid),
        "hostname": socket.gethostname(),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def _lock_owner_is_dead(metadata: object) -> bool:
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema_version") != REFRESH_LOCK_SCHEMA_VERSION
    ):
        return False
    try:
        pid = int(metadata.get("pid"))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    live = _process_is_live(pid)
    if live is False:
        return True
    if live is not True:
        # Unknown is not evidence of death.  Preserve the lock fail-closed.
        return False
    stored_start = metadata.get("process_start_time")
    current_start = _process_start_time(pid)
    # When a platform can provide creation time, a mismatch proves PID reuse.
    # If it cannot, an existing process is conservatively treated as live.
    return bool(stored_start and current_start and str(stored_start) != current_start)


def _process_is_live(pid: int) -> bool | None:
    """Probe liveness without using Windows ``os.kill(pid, 0)``.

    On Windows ``os.kill`` delegates to ``TerminateProcess`` for ordinary
    signals, including zero, so the POSIX liveness idiom can kill the refresh
    owner.  Return ``None`` whenever the platform cannot prove either state;
    callers must keep the lock in that case.
    """

    if pid == os.getpid():
        return True
    try:
        import psutil  # type: ignore[import-not-found]

        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
            if handle:
                kernel32.CloseHandle(handle)
                return True
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: no such PID.
                return False
            if error == 5:  # ERROR_ACCESS_DENIED still proves a live process.
                return True
            return None
        except (AttributeError, OSError, ValueError):
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _archive_provably_dead_lock(lock_path: Path) -> bool:
    try:
        metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not _lock_owner_is_dead(metadata):
        return False
    archive = lock_path.with_name(
        f"{lock_path.name}.dead.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.{uuid.uuid4().hex[:8]}"
    )
    try:
        os.replace(lock_path, archive)
    except (FileNotFoundError, OSError):
        return False
    return True


def _replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_active_pointer(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict) and parsed.get("schema_version") == ACTIVE_POINTER_SCHEMA_VERSION:
        return parsed
    return None


def _refresh_locked(
    *,
    state_root: Path,
    proxy_manifest: Path | None,
    ndx_artifact: Path | None,
    spy_artifact: Path | None,
    market_date: str,
    bootstrap_state_street_proxy: bool,
) -> dict[str, object]:
    market_date = _normalise_market_date(market_date)
    state_root = state_root.resolve()
    config_root = state_root / "config"
    output_path = config_root / "luna_core_universe.json"
    if bootstrap_state_street_proxy:
        if proxy_manifest is not None:
            raise RuntimeError("State Street bootstrap does not accept an explicit proxy manifest")
        if os.path.lexists(output_path):
            raise RuntimeError("State Street bootstrap requires a completely absent active pointer")
    source_path = (proxy_manifest or output_path).resolve()
    prior_output_bytes = output_path.read_bytes() if output_path.is_file() else None
    proxy_bootstrapped = False
    if ndx_artifact is not None and market_date != RELEASE_ANCHOR_MARKET_DATE:
        raise RuntimeError(
            "later-date NDX refresh requires the authenticated dated source download; "
            "an explicit artifact has no authenticated date provenance"
        )
    if source_path.is_file():
        wrapper = _read_json(source_path)
        source_base = source_path.parent
        try:
            pointer = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pointer = None
        if (
            isinstance(pointer, dict)
            and pointer.get("schema_version") == ACTIVE_POINTER_SCHEMA_VERSION
        ):
            source_base = _active_pointer_target(source_path, pointer).parent
    elif bootstrap_state_street_proxy and proxy_manifest is None:
        spy_root = _TRUSTED_SOURCE_ROOTS.get(SPY_SOURCE_ID)
        spy_source_uri = str(spy_root.get("source_uri") or "") if spy_root else ""
        if not spy_root or spy_source_uri != STATE_STREET_SPY_HOLDINGS_URL:
            raise RuntimeError("State Street SPY bootstrap trust root is unavailable")
        wrapper = {
            "schema_version": "dawnstrike.luna.core_universe_manifest_wrapper.v1",
            "manifests": [
                {
                    "source_id": SPY_SOURCE_ID,
                    "source_uri": spy_source_uri,
                    "index_name": "S&P 500",
                }
            ],
        }
        source_base = config_root
        proxy_bootstrapped = True
    else:
        raise RuntimeError(f"proxy manifest missing: {source_path}")
    children = wrapper.get("manifests")
    if not isinstance(children, list):
        raise RuntimeError("proxy manifest must contain a manifests list")
    proxy_children = [
        _resolve_proxy_paths(child, source_base)
        for child in children
        if isinstance(child, dict)
        and str(child.get("index_name") or child.get("index") or "")
        .strip()
        .lower()
        .replace(" ", "")
        in {"s&p500", "sp500", "sandp500"}
    ]
    if len(proxy_children) != 1:
        raise RuntimeError("exactly one existing SPY tracker proxy manifest is required")

    ndx_root = _TRUSTED_SOURCE_ROOTS[NDX_SOURCE_ID]
    ndx_url = _source_url(ndx_root, market_date)
    payload = ndx_artifact.read_bytes() if ndx_artifact else _fetch(ndx_url)
    spy_source_id = str(proxy_children[0].get("source_id") or "").strip()
    if not spy_source_id:
        raise RuntimeError("SPY proxy source_id missing")
    spy_url = str(proxy_children[0].get("source_uri") or "").strip()
    if not spy_url:
        root = _TRUSTED_SOURCE_ROOTS.get(spy_source_id)
        spy_url = str(root.get("source_uri") or "") if root else ""
    if not spy_url:
        raise RuntimeError("SPY proxy source_uri missing")
    spy_payload = (
        spy_artifact.read_bytes() if spy_artifact else _fetch(STATE_STREET_SPY_HOLDINGS_URL)
    )
    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ndx_symbols, ndx_attestation = _attest_ndx_payload(payload, market_date=market_date)
    spy_symbols, spy_effective, spy_attestation = _attest_spy_payload(
        spy_payload,
        source_id=spy_source_id,
        market_date=market_date,
    )
    stable_workbook_digest = str(ndx_attestation["content_digest_sha256"])
    proxy_manifest_hash = _canonical_json_sha256(
        {
            "source_id": spy_source_id,
            "source_uri": spy_url,
            "effective_date": spy_effective,
            "canonical_content_digest_sha256": spy_attestation["content_digest_sha256"],
            "canonical_symbol_set_hash_sha256": spy_attestation["symbol_set_hash_sha256"],
        }
    )
    generation_key = _canonical_json_sha256(
        {
            "market_date": market_date,
            "ndx_canonical_content_digest_sha256": stable_workbook_digest,
            "ndx_canonical_symbol_set_hash_sha256": ndx_attestation["symbol_set_hash_sha256"],
            "proxy_manifest_sha256": proxy_manifest_hash,
        }
    )
    generation_id = f"ndx-sod-{market_date}-{generation_key[:48]}"
    active_pointer = _read_active_pointer(output_path)
    if active_pointer and (
        active_pointer.get("generation_id") == generation_id
        and active_pointer.get("generation_key") == generation_key
        and active_pointer.get("market_date") == market_date
        and active_pointer.get("ndx_canonical_content_digest_sha256") == stable_workbook_digest
        and active_pointer.get("proxy_manifest_sha256") == proxy_manifest_hash
    ):
        # Validate the byte-identical active pair before reusing it.  A
        # corrupted or forged pointer never becomes a READY retry merely
        # because its content-addressed metadata matches.
        installed_contract = build_core_universe_contract(
            output_path,
            observed_at=observed_at,
            market_date=market_date,
        )
        if installed_contract.get("status") != "READY":
            raise RuntimeError(
                "active core generation did not reach READY: "
                + str(installed_contract.get("reason") or installed_contract.get("blockers"))
            )
        active_target = _active_pointer_target(output_path, active_pointer)
        active_wrapper = _read_json(output_path)
        active_ndx = next(
            (
                child
                for child in active_wrapper.get("manifests", [])
                if isinstance(child, dict)
                and str(child.get("index_name") or child.get("index") or "")
                .strip()
                .lower()
                .replace(" ", "")
                in {"nasdaq-100", "nasdaq100", "ndx"}
            ),
            {},
        )
        active_spy = next(
            (
                child
                for child in active_wrapper.get("manifests", [])
                if isinstance(child, dict)
                and str(child.get("index_name") or child.get("index") or "")
                .strip()
                .lower()
                .replace(" ", "")
                in {"s&p500", "sp500", "sandp500"}
            ),
            {},
        )
        active_entries = active_ndx.get("source_artifacts")
        active_artifact = active_target.parent / _ndx_artifact_name(market_date)
        active_sha256 = ""
        if isinstance(active_entries, list) and active_entries:
            entry = active_entries[0]
            if isinstance(entry, dict):
                if isinstance(entry.get("path"), str):
                    active_artifact = Path(str(entry["path"]))
                    if not active_artifact.is_absolute():
                        active_artifact = (active_target.parent / active_artifact).resolve()
                active_sha256 = str(entry.get("sha256") or "").lower()
        if not active_sha256:
            active_sha256 = str(
                next(
                    (
                        item.get("raw_artifact_sha256")
                        for item in installed_contract.get("source_artifacts") or []
                        if isinstance(item, dict) and item.get("source_id") == NDX_SOURCE_ID
                    ),
                    "",
                )
            ).lower()
        active_spy_entries = active_spy.get("source_artifacts")
        active_spy_artifact = active_target.parent / SPY_ARTIFACT_NAME
        active_spy_sha256 = ""
        if isinstance(active_spy_entries, list) and active_spy_entries:
            spy_entry = active_spy_entries[0]
            if isinstance(spy_entry, dict):
                if isinstance(spy_entry.get("path"), str):
                    active_spy_artifact = Path(str(spy_entry["path"]))
                    if not active_spy_artifact.is_absolute():
                        active_spy_artifact = (active_target.parent / active_spy_artifact).resolve()
                active_spy_sha256 = str(spy_entry.get("sha256") or "").lower()
        if not active_spy_sha256:
            active_spy_sha256 = str(
                next(
                    (
                        item.get("raw_artifact_sha256")
                        for item in installed_contract.get("source_artifacts") or []
                        if isinstance(item, dict) and item.get("source_id") == spy_source_id
                    ),
                    "",
                )
            ).lower()
        return {
            "status": "READY",
            "manifest": str(output_path),
            "ndx_artifact": str(active_artifact),
            "ndx_sha256": active_sha256,
            "ndx_member_count": len(active_ndx.get("members", [])),
            "spy_artifact": str(active_spy_artifact),
            "spy_sha256": active_spy_sha256,
            "spy_member_count": len(active_spy.get("members", [])),
            "spy_effective_date": active_spy.get("effective_date"),
            "observed_at": installed_contract.get("observed_at") or observed_at,
            "generation_id": generation_id,
            "generation_key": generation_key,
            "market_date": market_date,
            "reused": True,
            "proxy_bootstrapped": False,
        }
    generations_root = config_root / GENERATION_DIRECTORY
    generations_root.mkdir(parents=True, exist_ok=True)
    generation_dir = generations_root / generation_id
    if os.path.lexists(generation_dir):
        if active_pointer is not None:
            active_generation_target = _active_pointer_target(output_path, active_pointer)
            if (
                active_pointer.get("generation_id") == generation_id
                or active_generation_target.parent.resolve() == generation_dir.resolve()
            ):
                raise RuntimeError("refusing to replace an active core-universe generation")
        orphan = generations_root / (
            f"{generation_id}.orphan."
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}."
            f"{uuid.uuid4().hex[:8]}"
        )
        try:
            os.replace(generation_dir, orphan)
        except OSError as exc:
            raise RuntimeError(
                f"could not preserve inactive core-universe generation: {generation_dir}"
            ) from exc
    final_artifact = generation_dir / _ndx_artifact_name(market_date)
    final_spy_artifact = generation_dir / SPY_ARTIFACT_NAME
    candidate_path = generation_dir / "luna_core_universe.json"
    pointer_swapped = False
    generation_created = False
    try:
        generation_dir.mkdir()
        generation_created = True
        # The generation is inactive until its pointer is swapped.  Both raw
        # bytes and manifest are written under that generation, then the exact
        # final paths are validated before activation.
        _replace_bytes(final_artifact, payload)
        _replace_bytes(final_spy_artifact, spy_payload)
        spy_child = _spy_manifest(
            artifact_path=final_spy_artifact,
            payload=spy_payload,
            observed_at=observed_at,
            source_id=spy_source_id,
            source_uri=spy_url,
            parsed=(spy_symbols, spy_effective, spy_attestation),
        )
        ndx_child = _ndx_manifest(
            artifact_path=final_artifact,
            payload=payload,
            observed_at=observed_at,
            market_date=market_date,
            parsed=(ndx_symbols, ndx_attestation),
        )
        candidate = {**wrapper, "manifests": [spy_child, ndx_child]}
        candidate_bytes = (json.dumps(candidate, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _replace_bytes(candidate_path, candidate_bytes)
        contract = build_core_universe_contract(
            candidate_path,
            observed_at=observed_at,
            market_date=market_date,
        )
        if contract.get("status") != "READY":
            raise RuntimeError(
                "candidate core manifest did not reach READY: "
                + str(contract.get("reason") or contract.get("blockers"))
            )
        pointer = {
            "schema_version": ACTIVE_POINTER_SCHEMA_VERSION,
            "generation_id": generation_id,
            "generation_key": generation_key,
            "market_date": market_date,
            "ndx_canonical_content_digest_sha256": stable_workbook_digest,
            "ndx_canonical_symbol_set_hash_sha256": ndx_attestation["symbol_set_hash_sha256"],
            "proxy_manifest_sha256": proxy_manifest_hash,
            "spy_canonical_content_digest_sha256": spy_attestation["content_digest_sha256"],
            "spy_canonical_symbol_set_hash_sha256": spy_attestation["symbol_set_hash_sha256"],
            "spy_effective_date": spy_effective,
            "observed_at": observed_at,
            "manifest_path": (
                Path(GENERATION_DIRECTORY) / generation_id / "luna_core_universe.json"
            ).as_posix(),
            "manifest_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
            "created_at": observed_at,
        }
        pointer_bytes = (json.dumps(pointer, indent=2, sort_keys=True) + "\n").encode("utf-8")
        # This is the single active-pair swap.  The previous pointer/manifest
        # is not touched until the complete generation is READY.
        _replace_bytes(output_path, pointer_bytes)
        pointer_swapped = True
        installed_contract = build_core_universe_contract(
            output_path,
            observed_at=observed_at,
            market_date=market_date,
        )
        if installed_contract.get("status") != "READY":
            raise RuntimeError(
                "installed core manifest did not reach READY: "
                + str(installed_contract.get("reason") or installed_contract.get("blockers"))
            )
    except Exception:
        if pointer_swapped and prior_output_bytes is not None:
            _replace_bytes(output_path, prior_output_bytes)
        elif pointer_swapped and prior_output_bytes is None and output_path.exists():
            output_path.unlink()
        if generation_created and not pointer_swapped:
            shutil.rmtree(generation_dir, ignore_errors=True)
        raise
    return {
        "status": "READY",
        "manifest": str(output_path),
        "ndx_artifact": str(final_artifact),
        "ndx_sha256": hashlib.sha256(payload).hexdigest(),
        "spy_artifact": str(final_spy_artifact),
        "spy_sha256": hashlib.sha256(spy_payload).hexdigest(),
        "spy_member_count": len(spy_child["members"]),
        "spy_effective_date": spy_effective,
        "ndx_member_count": len(ndx_child["members"]),
        "observed_at": observed_at,
        "generation_id": generation_id,
        "generation_key": generation_key,
        "market_date": market_date,
        "reused": False,
        "proxy_bootstrapped": proxy_bootstrapped,
    }


def refresh(
    *,
    state_root: Path,
    proxy_manifest: Path | None,
    ndx_artifact: Path | None,
    spy_artifact: Path | None = None,
    market_date: str | None = None,
    bootstrap_state_street_proxy: bool = False,
) -> dict[str, object]:
    market_date = _normalise_market_date(market_date)
    state_root = state_root.resolve()
    with _refresh_lock(state_root / "config"):
        return _refresh_locked(
            state_root=state_root,
            proxy_manifest=proxy_manifest,
            ndx_artifact=ndx_artifact,
            spy_artifact=spy_artifact,
            market_date=market_date,
            bootstrap_state_street_proxy=bootstrap_state_street_proxy,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", default=r"C:\r\dawnstrike-state")
    parser.add_argument("--proxy-manifest", default=None)
    parser.add_argument("--ndx-artifact", default=None)
    parser.add_argument("--spy-artifact", default=None)
    parser.add_argument("--market-date", required=True)
    parser.add_argument(
        "--bootstrap-state-street-proxy",
        action="store_true",
        help=(
            "Bootstrap a missing core-universe pointer from the pinned official "
            "State Street SPY holdings source; never replaces an existing pointer"
        ),
    )
    args = parser.parse_args()
    try:
        result = refresh(
            state_root=Path(args.state_root),
            proxy_manifest=Path(args.proxy_manifest) if args.proxy_manifest else None,
            ndx_artifact=Path(args.ndx_artifact) if args.ndx_artifact else None,
            spy_artifact=Path(args.spy_artifact) if args.spy_artifact else None,
            market_date=args.market_date,
            bootstrap_state_street_proxy=args.bootstrap_state_street_proxy,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "DATA_UNAVAILABLE", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
