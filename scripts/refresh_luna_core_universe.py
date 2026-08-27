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
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.services.luna_core_universe_service import (
    _TRUSTED_SOURCE_ROOTS,
    NASDAQ_NDX_SOD_2026_08_27_URL,
    STATE_STREET_SPY_HOLDINGS_URL,
    _canonical_member_hash,
    _parse_nasdaq_sod_weightings_xlsx,
    build_core_universe_contract,
)

MAX_DOWNLOAD_BYTES = 2_000_000
NDX_SOURCE_ID = "nasdaq-ndx-point-in-time-2026-08-27"


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
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"proxy manifest unreadable: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("proxy manifest must be a JSON object")
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


def _ndx_manifest(*, artifact_path: Path, payload: bytes, observed_at: str) -> dict[str, object]:
    root = _TRUSTED_SOURCE_ROOTS[NDX_SOURCE_ID]
    symbols = _parse_nasdaq_sod_weightings_xlsx(payload)
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
    raw_hash = hashlib.sha256(payload).hexdigest()
    expected_hash = str(root["raw_artifact_hashes"][0])
    if raw_hash != expected_hash:
        raise RuntimeError(
            "NDX raw SHA-256 is not the governed 2026-08-27 capture: "
            f"expected {expected_hash}, observed {raw_hash}"
        )
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
        "source_artifacts": [
            {
                "uri": NASDAQ_NDX_SOD_2026_08_27_URL,
                "path": str(artifact_path),
                "sha256": raw_hash,
                "byte_count": len(payload),
            }
        ],
        "canonical_member_set_hash_sha256": member_hash,
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


def refresh(
    *, state_root: Path, proxy_manifest: Path | None, ndx_artifact: Path | None
) -> dict[str, object]:
    state_root = state_root.resolve()
    config_root = state_root / "config"
    evidence_root = config_root / "luna_core_universe_evidence"
    output_path = config_root / "luna_core_universe.json"
    source_path = proxy_manifest or output_path
    if not source_path.is_file():
        raise RuntimeError(f"proxy manifest missing: {source_path}")
    wrapper = _read_json(source_path)
    children = wrapper.get("manifests")
    if not isinstance(children, list):
        raise RuntimeError("proxy manifest must contain a manifests list")
    proxy_children = [
        _resolve_proxy_paths(child, source_path.parent)
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
    final_artifact = evidence_root / "ndx-sod-2026-08-27.xlsx"
    # Validate and construct against a staged path first.  The old manifest is
    # not replaced until the complete two-index contract is READY.
    with tempfile.TemporaryDirectory(prefix="luna-core-refresh-", dir=config_root) as staging:
        staged_evidence = Path(staging) / final_artifact.name
        staged_evidence.write_bytes(payload)
        ndx_child = _ndx_manifest(
            artifact_path=staged_evidence,
            payload=payload,
            observed_at=observed_at,
        )
        candidate = {**wrapper, "manifests": [proxy_children[0], ndx_child]}
        candidate_path = Path(staging) / "luna_core_universe.json"
        candidate_path.write_text(
            json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        contract = build_core_universe_contract(
            candidate_path,
            observed_at=observed_at,
            market_date="2026-08-27",
        )
        if contract.get("status") != "READY":
            raise RuntimeError(
                "candidate core manifest did not reach READY: "
                + str(contract.get("reason") or contract.get("blockers"))
            )
        final_ndx_child = dict(ndx_child)
        final_ndx_child["source_artifacts"] = [
            {**dict(entry), "path": str(final_artifact)}
            for entry in ndx_child["source_artifacts"]
            if isinstance(entry, dict)
        ]
        final_candidate = {
            **candidate,
            "manifests": [proxy_children[0], final_ndx_child],
        }
        _replace_bytes(final_artifact, payload)
        _replace_bytes(
            output_path,
            (json.dumps(final_candidate, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    return {
        "status": "READY",
        "manifest": str(output_path),
        "ndx_artifact": str(final_artifact),
        "ndx_sha256": hashlib.sha256(payload).hexdigest(),
        "ndx_member_count": len(ndx_child["members"]),
        "observed_at": observed_at,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", default=r"C:\r\dawnstrike-state")
    parser.add_argument("--proxy-manifest", default=None)
    parser.add_argument("--ndx-artifact", default=None)
    args = parser.parse_args()
    try:
        result = refresh(
            state_root=Path(args.state_root),
            proxy_manifest=Path(args.proxy_manifest) if args.proxy_manifest else None,
            ndx_artifact=Path(args.ndx_artifact) if args.ndx_artifact else None,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "DATA_UNAVAILABLE", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
