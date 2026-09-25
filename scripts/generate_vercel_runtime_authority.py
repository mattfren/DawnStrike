"""Generate the committed authority for Vercel's exact Python runtime output."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA = "dawnstrike.vercel_generated_runtime_authority.v1"
NODE = {
    "archive_sha256": "6cac9ffbca8f6a47091e4b5c772e0606049c3871cb67d900c0cedde630e545ba",
    "archive_size": 37_539_751,
    "executable_sha256": "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5",
    "executable_size": 93_381_448,
    "version": "24.20.0",
}
UV = {
    "executable_sha256": "b5d230c79ffa3629422f48bfce0766e9827769608a79bbb4e4e540081f59d97c",
    "executable_size": 41_386_496,
    "version": "0.12.9",
}
VERCEL = {
    "entry_sha256": "2dd6e7c273a24bf4317af867d9b7bacb4db35487b42ea77912e2e7c33fa0c152",
    "tree_file_count": 7_131,
    "tree_sha256": "3bfb7509c4bf6a8fec920566c290a385c8160b9851b2350a655f0fd8b6c9e069",
    "version": "59.11.2",
}
ROUTES = ("api/health.func", "api/readiness.func")
VENDOR_SOURCE = re.compile(
    r"^_vendor/(?:pip|pip-26\.2\.1\.dist-info|vercel_runtime|"
    r"vercel_runtime-0\.22\.1\.dist-info)/"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(
    stage: Path,
    *,
    kind: str,
    target: str,
    logical_sources: list[str],
    route_bindings: list[str],
) -> dict[str, Any]:
    if (
        not target
        or "\\" in target
        or target.startswith("/")
        or any(part in {"", ".", ".."} for part in target.split("/"))
    ):
        raise ValueError(f"unsafe generated-runtime target: {target}")
    path = stage / Path(target)
    if not path.is_file():
        raise ValueError(f"generated-runtime target is missing: {target}")
    return {
        "kind": kind,
        "logical_sources": sorted(logical_sources),
        "route_bindings": sorted(route_bindings),
        "sha256": _sha256(path),
        "size": path.stat().st_size,
        "target": target,
    }


def _map_hash(records: list[dict[str, Any]]) -> str:
    lines = [
        "|".join(
            (
                str(record["kind"]),
                str(record["target"]),
                ",".join(record["logical_sources"]),
                ",".join(record["route_bindings"]),
                str(record["size"]),
                str(record["sha256"]),
            )
        )
        for record in records
    ]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _authority_hash(payload: dict[str, Any]) -> str:
    lines = [
        str(payload["schema_version"]),
        "node|" + "|".join(str(payload["node"][key]) for key in sorted(NODE)),
        "uv|" + "|".join(str(payload["uv"][key]) for key in sorted(UV)),
        "vercel|"
        + "|".join(str(payload["vercel_cli"][key]) for key in sorted(VERCEL)),
        f"python_runtime|{payload['python_runtime']}",
        f"generated|{payload['generated_map_sha256']}",
        f"vendor|{payload['vendor_map_sha256']}",
        "research_only|true",
        "broker_execution_enabled|false",
    ]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def generate(stage: Path) -> dict[str, Any]:
    stage = stage.resolve(strict=True)
    vendor_maps: dict[str, dict[str, str]] = {}
    for route in ROUTES:
        route_root = stage / ".vercel" / "output" / "functions" / Path(route)
        config_path = route_root / ".vc-config.json"
        wrapper_path = route_root / "vc__handler__python.py"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not isinstance(config.get("filePathMap"), dict):
            raise ValueError(f"route config lacks filePathMap: {route}")
        wrapper = wrapper_path.read_text(encoding="utf-8")
        api_name = route.removeprefix("api/").removesuffix(".func")
        if (
            f'__VC_HANDLER_ENTRYPOINT": "api/{api_name}.py"' not in wrapper
            or f'__VC_HANDLER_MODULE_NAME": "api.{api_name}"' not in wrapper
        ):
            raise ValueError(f"route wrapper identity is invalid: {route}")
        vendor_map: dict[str, str] = {}
        for source, target_value in config["filePathMap"].items():
            target = str(target_value)
            if source.startswith("_vendor/"):
                if VENDOR_SOURCE.match(source) is None:
                    raise ValueError(f"unexpected generated vendor source: {source}")
                if not target.startswith(".vercel/python/.venv/"):
                    raise ValueError(f"generated vendor target escaped the venv: {source}")
                vendor_map[source] = target
        if not vendor_map:
            raise ValueError(f"route has no generated vendor map: {route}")
        vendor_maps[route] = vendor_map
    first_vendor = vendor_maps[ROUTES[0]]
    if any(vendor_maps[route] != first_vendor for route in ROUTES[1:]):
        raise ValueError("route vendor maps differ")

    generated_files: list[dict[str, Any]] = []
    for route in ROUTES:
        route_prefix = f".vercel/output/functions/{route}"
        generated_files.extend(
            (
                _record(
                    stage,
                    kind="route_config",
                    target=f"{route_prefix}/.vc-config.json",
                    logical_sources=[f"{route_prefix}/.vc-config.json"],
                    route_bindings=[route],
                ),
                _record(
                    stage,
                    kind="route_wrapper",
                    target=f"{route_prefix}/vc__handler__python.py",
                    logical_sources=[f"{route_prefix}/vc__handler__python.py"],
                    route_bindings=[route],
                ),
            )
        )
    generated_files.append(
        _record(
            stage,
            kind="deployment_config",
            target=".vercel/output/config.json",
            logical_sources=[".vercel/output/config.json"],
            route_bindings=[],
        )
    )
    for target in (".python-version", "api/public_state.py", "uv.lock"):
        generated_files.append(
            _record(
                stage,
                kind="runtime_support",
                target=target,
                logical_sources=[target],
                route_bindings=list(ROUTES),
            )
        )
    generated_files.sort(key=lambda item: (item["target"], item["kind"]))

    vendor_files = [
        _record(
            stage,
            kind="vendor_file",
            target=target,
            logical_sources=[source],
            route_bindings=list(ROUTES),
        )
        for source, target in sorted(first_vendor.items())
    ]
    vendor_files.sort(key=lambda item: (item["logical_sources"], item["target"]))
    payload: dict[str, Any] = {
        "broker_execution_enabled": False,
        "generated_files": generated_files,
        "generated_map_sha256": _map_hash(generated_files),
        "node": NODE,
        "python_runtime": "python3.13",
        "research_only": True,
        "schema_version": SCHEMA,
        "uv": UV,
        "vendor_files": vendor_files,
        "vendor_map_sha256": _map_hash(vendor_files),
        "vercel_cli": VERCEL,
    }
    payload["authority_contract_sha256"] = _authority_hash(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = generate(args.stage)
    args.output.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
