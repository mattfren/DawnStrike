from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "materialize_dawnstrike_dependencies.py"


def _module():
    spec = importlib.util.spec_from_file_location("dependency_materializer", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hash_spec(contents: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(contents).digest()).decode().rstrip(
        "="
    )


def _fixture(prefix: Path) -> tuple[Path, Path, bytes]:
    site_packages = prefix / "Lib" / "site-packages"
    package = site_packages / "fixture_dep"
    dist_info = site_packages / "fixture_dep-1.0.dist-info"
    package.mkdir(parents=True)
    dist_info.mkdir()
    init_bytes = b"VALUE = 1\n"
    native_bytes = b"exact-native-payload"
    metadata_bytes = b"Metadata-Version: 2.1\nName: fixture-dep\nVersion: 1.0\n"
    (package / "__init__.py").write_bytes(init_bytes)
    (package / "native.dll").write_bytes(native_bytes)
    (dist_info / "METADATA").write_bytes(metadata_bytes)
    rows = [
        ("fixture_dep/__init__.py", _hash_spec(init_bytes), str(len(init_bytes))),
        ("fixture_dep/native.dll", _hash_spec(native_bytes), str(len(native_bytes))),
        (
            "fixture_dep-1.0.dist-info/METADATA",
            _hash_spec(metadata_bytes),
            str(len(metadata_bytes)),
        ),
        ("fixture_dep-1.0.dist-info/RECORD", "", ""),
    ]
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(rows)
    record_bytes = stream.getvalue().encode()
    (dist_info / "RECORD").write_bytes(record_bytes)
    requirements = prefix / "requirements.lock"
    requirements.write_text(
        "fixture-dep==1.0 \\\n+    --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    return requirements, package / "native.dll", record_bytes


def test_materializer_copies_only_record_owned_verified_payloads(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "source"
    stage = tmp_path / "stage"
    stage.mkdir()
    requirements, _, record_bytes = _fixture(source)
    rogue = source / "Lib" / "site-packages" / "rogue.dll"
    rogue.write_bytes(b"unowned")
    module.APPROVED_RECORD_SET_SHA256 = hashlib.sha256(
        (
            "fixture-dep\0"
            "1.0\0"
            f"{hashlib.sha256(record_bytes).hexdigest()}\n"
        ).encode()
    ).hexdigest()

    result = module.materialize(source, stage, requirements)

    assert result["status"] == "PASS"
    assert result["distribution_count"] == 1
    assert (stage / "Lib" / "site-packages" / "fixture_dep" / "native.dll").read_bytes() == (
        b"exact-native-payload"
    )
    assert not (stage / rogue.relative_to(source)).exists()


def test_materializer_rejects_tampered_native_payload(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "source"
    stage = tmp_path / "stage"
    stage.mkdir()
    requirements, native, record_bytes = _fixture(source)
    module.APPROVED_RECORD_SET_SHA256 = hashlib.sha256(
        (
            "fixture-dep\0"
            "1.0\0"
            f"{hashlib.sha256(record_bytes).hexdigest()}\n"
        ).encode()
    ).hexdigest()
    native.write_bytes(b"tampered-native-payload")

    with pytest.raises(RuntimeError, match="payload (size|hash) changed"):
        module.materialize(source, stage, requirements)
