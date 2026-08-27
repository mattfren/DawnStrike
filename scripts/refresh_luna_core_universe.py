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
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.services.luna_core_universe_service import (
    _TRUSTED_SOURCE_ROOTS,
    ACTIVE_POINTER_SCHEMA_VERSION,
    NASDAQ_NDX_SOD_2026_08_27_URL,
    STATE_STREET_SPY_HOLDINGS_URL,
    _active_pointer_target,
    _canonical_member_hash,
    _parse_nasdaq_sod_weightings_xlsx_with_attestation,
    build_core_universe_contract,
    read_core_universe_manifest,
)

MAX_DOWNLOAD_BYTES = 2_000_000
NDX_SOURCE_ID = "nasdaq-ndx-point-in-time-2026-08-27"
GENERATION_DIRECTORY = "luna_core_universe_generations"
SUPPORTED_MARKET_DATE = "2026-08-27"
NDX_ARTIFACT_NAME = "ndx-sod-2026-08-27.xlsx"
REFRESH_LOCK_NAME = ".luna_core_universe.refresh.lock"


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


def _canonical_hash(records: list[dict[str, object]]) -> str:
    return _canonical_member_hash(
        [
            {
                "symbol": row["symbol"],
                "provider_symbol": row["provider_symbol"],
                "asset_class": row["asset_class"],
                "index": "Nasdaq-100",
                "valid_from": "2026-08-27",
                "valid_to": None,
            }
            for row in records
        ]
    )


def _attest_ndx_payload(
    payload: bytes,
) -> tuple[list[str], dict[str, object]]:
    root = _TRUSTED_SOURCE_ROOTS[NDX_SOURCE_ID]
    try:
        symbols, attestation = _parse_nasdaq_sod_weightings_xlsx_with_attestation(payload)
    except ValueError as exc:
        raise RuntimeError(f"NDX workbook is not the governed 2026-08-27 capture: {exc}") from exc
    expected_names = root.get("canonical_zip_member_names")
    expected_hashes = root.get("canonical_zip_member_hashes")
    expected_content = str(root.get("canonical_content_digest_sha256") or "").lower()
    if expected_names and list(attestation["member_names"]) != list(expected_names):
        raise RuntimeError(
            "NDX workbook is not the governed 2026-08-27 capture: structure mismatch"
        )
    if expected_hashes and attestation["member_hashes"] != dict(expected_hashes):
        raise RuntimeError("NDX workbook is not the governed 2026-08-27 capture: member mismatch")
    if expected_content and attestation["content_digest_sha256"] != expected_content:
        raise RuntimeError("NDX workbook is not the governed 2026-08-27 capture: content mismatch")
    records: list[dict[str, object]] = [
        {
            "ticker": symbol,
            "provider_symbol": symbol,
            "asset_class": "common_stock",
            "index_memberships": ["Nasdaq-100"],
            "valid_from": "2026-08-27",
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
        ]
    )
    expected_member_set = str(root.get("canonical_member_set_hash_sha256") or "").lower()
    if expected_member_set and member_hash != expected_member_set:
        raise RuntimeError(
            "NDX workbook is not the governed 2026-08-27 capture: member set mismatch"
        )
    attestation["member_set_hash_sha256"] = member_hash
    return symbols, attestation


def _ndx_manifest(
    *,
    artifact_path: Path,
    payload: bytes,
    observed_at: str,
    parsed: tuple[list[str], dict[str, object]] | None = None,
) -> dict[str, object]:
    symbols, attestation = parsed or _attest_ndx_payload(payload)
    root = _TRUSTED_SOURCE_ROOTS[NDX_SOURCE_ID]
    records: list[dict[str, object]] = [
        {
            "ticker": symbol,
            "provider_symbol": symbol,
            "asset_class": "common_stock",
            "index_memberships": ["Nasdaq-100"],
            "valid_from": "2026-08-27",
        }
        for symbol in symbols
    ]
    member_hash = str(attestation["member_set_hash_sha256"])
    raw_hash = hashlib.sha256(payload).hexdigest()
    return {
        "source_id": NDX_SOURCE_ID,
        "source_uri": NASDAQ_NDX_SOD_2026_08_27_URL,
        "source_scope": root["source_scope"],
        "observed_at": observed_at,
        "effective_date": "2026-08-27",
        "reconstitution_id": root["reconstitution_id"],
        "index_name": "Nasdaq-100",
        "expected_count": 102,
        "completeness_verdict": "COMPLETE",
        "members": records,
        "canonical_zip_member_names": attestation["member_names"],
        "canonical_zip_member_hashes": attestation["member_hashes"],
        "canonical_content_digest_sha256": attestation["content_digest_sha256"],
        "canonical_member_set_hash_sha256": member_hash,
        "source_artifacts": [
            {
                "uri": NASDAQ_NDX_SOD_2026_08_27_URL,
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
            "effective_date": "2026-08-27",
            "input_artifact_hashes": [raw_hash],
            "canonical_member_set_hash_sha256": member_hash,
        },
    }


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def _refresh_lock(config_root: Path):
    """Serialize refreshes without replacing an active pair concurrently."""

    lock_path = config_root / REFRESH_LOCK_NAME
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("core universe refresh already in progress") from exc
    except OSError as exc:
        raise RuntimeError(f"core universe refresh lock unavailable: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("Dawnstrike Luna core refresh lock\n")
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # The refresh result is still governed by the atomic pointer.  A
            # stale lock is safer than allowing concurrent writers after an
            # unusual cleanup failure, so surface it to the caller.
            raise RuntimeError(f"could not remove refresh lock: {lock_path}") from exc


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
    market_date: str = SUPPORTED_MARKET_DATE,
) -> dict[str, object]:
    if market_date != SUPPORTED_MARKET_DATE:
        raise RuntimeError(
            "no governed current NDX source is available for market date "
            f"{market_date}; Aug-27 evidence cannot certify a later session"
        )
    state_root = state_root.resolve()
    config_root = state_root / "config"
    output_path = config_root / "luna_core_universe.json"
    source_path = (proxy_manifest or output_path).resolve()
    if not source_path.is_file():
        raise RuntimeError(f"proxy manifest missing: {source_path}")
    prior_output_bytes = output_path.read_bytes() if output_path.is_file() else None
    wrapper = _read_json(source_path)
    source_base = source_path.parent
    try:
        pointer = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pointer = None
    if isinstance(pointer, dict) and pointer.get("schema_version") == ACTIVE_POINTER_SCHEMA_VERSION:
        source_base = _active_pointer_target(source_path, pointer).parent
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

    payload = ndx_artifact.read_bytes() if ndx_artifact else _fetch(NASDAQ_NDX_SOD_2026_08_27_URL)
    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ndx_symbols, ndx_attestation = _attest_ndx_payload(payload)
    stable_workbook_digest = str(ndx_attestation["content_digest_sha256"])
    proxy_manifest_hash = _canonical_json_sha256(proxy_children[0])
    generation_key = _canonical_json_sha256(
        {
            "market_date": market_date,
            "ndx_canonical_content_digest_sha256": stable_workbook_digest,
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
        active_entries = active_ndx.get("source_artifacts")
        active_artifact = active_target.parent / NDX_ARTIFACT_NAME
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
                        if isinstance(item, dict)
                        and item.get("source_id") == NDX_SOURCE_ID
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
            "observed_at": installed_contract.get("observed_at") or observed_at,
            "generation_id": generation_id,
            "generation_key": generation_key,
            "market_date": market_date,
            "reused": True,
        }
    generations_root = config_root / GENERATION_DIRECTORY
    generations_root.mkdir(parents=True, exist_ok=True)
    generation_dir = generations_root / generation_id
    generation_dir.mkdir()
    final_artifact = generation_dir / NDX_ARTIFACT_NAME
    candidate_path = generation_dir / "luna_core_universe.json"
    pointer_swapped = False
    try:
        # The generation is inactive until its pointer is swapped.  Both raw
        # bytes and manifest are written under that generation, then the exact
        # final paths are validated before activation.
        _replace_bytes(final_artifact, payload)
        ndx_child = _ndx_manifest(
            artifact_path=final_artifact,
            payload=payload,
            observed_at=observed_at,
            parsed=(ndx_symbols, ndx_attestation),
        )
        candidate = {**wrapper, "manifests": [proxy_children[0], ndx_child]}
        candidate_bytes = (json.dumps(candidate, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
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
            "proxy_manifest_sha256": proxy_manifest_hash,
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
        if not pointer_swapped:
            shutil.rmtree(generation_dir, ignore_errors=True)
        raise
    return {
        "status": "READY",
        "manifest": str(output_path),
        "ndx_artifact": str(final_artifact),
        "ndx_sha256": hashlib.sha256(payload).hexdigest(),
        "ndx_member_count": len(ndx_child["members"]),
        "observed_at": observed_at,
        "generation_id": generation_id,
        "generation_key": generation_key,
        "market_date": market_date,
        "reused": False,
    }


def refresh(
    *,
    state_root: Path,
    proxy_manifest: Path | None,
    ndx_artifact: Path | None,
    market_date: str = SUPPORTED_MARKET_DATE,
) -> dict[str, object]:
    if market_date != SUPPORTED_MARKET_DATE:
        raise RuntimeError(
            "no governed current NDX source is available for market date "
            f"{market_date}; Aug-27 evidence cannot certify a later session"
        )
    state_root = state_root.resolve()
    with _refresh_lock(state_root / "config"):
        return _refresh_locked(
            state_root=state_root,
            proxy_manifest=proxy_manifest,
            ndx_artifact=ndx_artifact,
            market_date=market_date,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", default=r"C:\r\dawnstrike-state")
    parser.add_argument("--proxy-manifest", default=None)
    parser.add_argument("--ndx-artifact", default=None)
    parser.add_argument("--market-date", required=True)
    args = parser.parse_args()
    try:
        result = refresh(
            state_root=Path(args.state_root),
            proxy_manifest=Path(args.proxy_manifest) if args.proxy_manifest else None,
            ndx_artifact=Path(args.ndx_artifact) if args.ndx_artifact else None,
            market_date=args.market_date,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "DATA_UNAVAILABLE", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
