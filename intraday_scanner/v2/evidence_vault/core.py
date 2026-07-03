"""Tamper-evident frozen-pick storage for Forward Autopilot."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvidenceVaultPaths:
    root: Path
    frozen_picks: Path
    pick_hashes: Path
    evaluations: Path
    manifests: Path
    reports: Path
    reconciliation: Path
    shadow_replay: Path
    logs: Path
    calendar: Path
    riskhub: Path
    strategy_evidence: Path


@dataclass(frozen=True)
class FrozenWriteResult:
    status: str
    date: str
    evidence_mode: str
    frozen_json_path: Path
    frozen_csv_path: Path
    hash_path: Path
    manifest_path: Path
    pick_set_hash: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "date": self.date,
            "evidence_mode": self.evidence_mode,
            "frozen_csv_path": self.frozen_csv_path.as_posix(),
            "frozen_json_path": self.frozen_json_path.as_posix(),
            "hash_path": self.hash_path.as_posix(),
            "manifest_path": self.manifest_path.as_posix(),
            "pick_set_hash": self.pick_set_hash,
            "reason": self.reason,
            "status": self.status,
        }


def create_paths(root: Path = Path("data/v2_forward_evidence")) -> EvidenceVaultPaths:
    paths = EvidenceVaultPaths(
        root=root,
        frozen_picks=root / "frozen_picks",
        pick_hashes=root / "pick_hashes",
        evaluations=root / "evaluations",
        manifests=root / "manifests",
        reports=root / "reports",
        reconciliation=root / "reconciliation",
        shadow_replay=root / "shadow_replay",
        logs=root / "logs",
        calendar=root / "calendar",
        riskhub=root / "riskhub",
        strategy_evidence=root / "strategy_evidence",
    )
    for path in (
        paths.root,
        paths.frozen_picks,
        paths.pick_hashes,
        paths.evaluations,
        paths.manifests,
        paths.reports,
        paths.reconciliation,
        paths.shadow_replay,
        paths.logs,
        paths.calendar,
        paths.riskhub,
        paths.strategy_evidence,
    ):
        path.mkdir(parents=True, exist_ok=True)
    for child in ("frozen_picks", "ledger", "calendar", "reports"):
        (paths.shadow_replay / child).mkdir(parents=True, exist_ok=True)
    (paths.reports / "daily").mkdir(parents=True, exist_ok=True)
    return paths


def write_frozen_pick_set(
    *,
    payload: dict[str, object],
    date_value: str,
    evidence_mode: str,
    paths: EvidenceVaultPaths,
) -> FrozenWriteResult:
    target_dirs = _target_dirs(paths, evidence_mode)
    canonical_payload = _hash_scope(payload)
    pick_set_hash = canonical_hash(canonical_payload)
    payload = {
        **payload,
        "pick_set_hash": pick_set_hash,
        "hash_scope": (
            "canonical frozen pick payload excluding pick_set_hash and "
            "output_artifact_hashes"
        ),
        "output_artifact_hashes": {"pick_set_sha256": pick_set_hash},
    }
    base_name = f"{date_value}_picks"
    json_path = target_dirs["frozen_picks"] / f"{base_name}.json"
    csv_path = target_dirs["frozen_picks"] / f"{base_name}.csv"
    hash_path = target_dirs["pick_hashes"] / f"{date_value}_hash.json"
    manifest_path = target_dirs["manifests"] / f"{date_value}_manifest.json"
    status = "written"
    reason = "new_frozen_pick_set"
    if json_path.exists():
        existing_hash = _existing_hash(json_path, hash_path)
        if existing_hash == pick_set_hash:
            status = "verified_existing"
            reason = "same_hash"
            return FrozenWriteResult(
                status=status,
                date=date_value,
                evidence_mode=evidence_mode,
                frozen_json_path=json_path,
                frozen_csv_path=csv_path,
                hash_path=hash_path,
                manifest_path=manifest_path,
                pick_set_hash=pick_set_hash,
                reason=reason,
            )
        reason = _classify_hash_change(json_path, canonical_payload)
        suffix = pick_set_hash[:12]
        json_path = target_dirs["frozen_picks"] / f"{base_name}_superseding_{suffix}.json"
        csv_path = target_dirs["frozen_picks"] / f"{base_name}_superseding_{suffix}.csv"
        hash_path = target_dirs["pick_hashes"] / f"{date_value}_hash_superseding_{suffix}.json"
        manifest_path = (
            target_dirs["manifests"] / f"{date_value}_manifest_superseding_{suffix}.json"
        )
        if json_path.exists():
            existing_superseding_hash = _existing_hash(json_path, hash_path)
            if existing_superseding_hash == pick_set_hash:
                return FrozenWriteResult(
                    status="verified_existing",
                    date=date_value,
                    evidence_mode=evidence_mode,
                    frozen_json_path=json_path,
                    frozen_csv_path=csv_path,
                    hash_path=hash_path,
                    manifest_path=manifest_path,
                    pick_set_hash=pick_set_hash,
                    reason="same_superseding_hash",
                )
        status = "superseding_written"
    _write_json(json_path, payload)
    _write_csv(csv_path, _pick_rows(payload))
    artifact_hashes = {
        json_path.as_posix(): file_sha256(json_path),
        csv_path.as_posix(): file_sha256(csv_path),
    }
    hash_payload = {
        "artifact_hashes": artifact_hashes,
        "date": date_value,
        "evidence_mode": evidence_mode,
        "hash_algorithm": "sha256",
        "pick_set_hash": pick_set_hash,
        "reason": reason,
        "schema_version": "v2.forward_evidence_hash.v1",
        "status": status,
    }
    _write_json(hash_path, hash_payload)
    manifest_payload = {
        "artifact_hashes": {
            **artifact_hashes,
            hash_path.as_posix(): file_sha256(hash_path),
        },
        "date": date_value,
        "evidence_mode": evidence_mode,
        "frozen_pick_path": json_path.as_posix(),
        "hash_path": hash_path.as_posix(),
        "pick_count": len(_pick_rows(payload)),
        "pick_set_hash": pick_set_hash,
        "schema_version": "v2.forward_evidence_manifest.v1",
        "status": status,
    }
    _write_json(manifest_path, manifest_payload)
    return FrozenWriteResult(
        status=status,
        date=date_value,
        evidence_mode=evidence_mode,
        frozen_json_path=json_path,
        frozen_csv_path=csv_path,
        hash_path=hash_path,
        manifest_path=manifest_path,
        pick_set_hash=pick_set_hash,
        reason=reason,
    )


def verify_frozen_pick_hashes(paths: EvidenceVaultPaths) -> dict[str, object]:
    checked: list[dict[str, object]] = []
    failures: list[str] = []
    for json_path in sorted(paths.frozen_picks.glob("*_picks*.json")) + sorted(
        (paths.shadow_replay / "frozen_picks").glob("*_picks*.json")
    ):
        payload = _read_json(json_path, {})
        if not isinstance(payload, dict):
            failures.append(f"{json_path.as_posix()}: not a JSON object")
            continue
        expected = _declared_hash(payload)
        actual = _actual_hash(payload)
        status = "passed" if expected == actual else "failed"
        if status == "failed":
            failures.append(f"{json_path.as_posix()}: hash mismatch")
        checked.append(
            {
                "actual_hash": actual,
                "expected_hash": expected,
                "path": json_path.as_posix(),
                "status": status,
            }
        )
    if not checked:
        failures.append("no frozen pick files found")
    return {
        "checked": checked,
        "failure_count": len(failures),
        "failures": failures,
        "status": "passed" if not failures else "failed",
    }


def canonical_json(payload: object) -> str:
    return json.dumps(_plain(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_hash(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _target_dirs(paths: EvidenceVaultPaths, evidence_mode: str) -> dict[str, Path]:
    if evidence_mode == "shadow_forward_replay":
        frozen = paths.shadow_replay / "frozen_picks"
        hashes = paths.shadow_replay / "pick_hashes"
        manifests = paths.shadow_replay / "manifests"
        for path in (frozen, hashes, manifests):
            path.mkdir(parents=True, exist_ok=True)
        return {"frozen_picks": frozen, "pick_hashes": hashes, "manifests": manifests}
    return {
        "frozen_picks": paths.frozen_picks,
        "pick_hashes": paths.pick_hashes,
        "manifests": paths.manifests,
    }


def _hash_scope(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"hash_scope", "output_artifact_hashes", "pick_set_hash"}
    }


def _legacy_hash_scope(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"hash_scope", "output_artifact_hashes"}
    }


def _actual_hash(payload: dict[str, object]) -> str:
    actual = canonical_hash(_hash_scope(payload))
    if actual == _declared_hash(payload):
        return actual
    legacy_scope = "canonical frozen pick payload excluding output_artifact_hashes"
    if payload.get("hash_scope") == legacy_scope:
        return canonical_hash(_legacy_hash_scope(payload))
    return actual


def _existing_hash(json_path: Path, hash_path: Path) -> str:
    hash_payload = _read_json(hash_path, {})
    if isinstance(hash_payload, dict) and isinstance(hash_payload.get("pick_set_hash"), str):
        return str(hash_payload["pick_set_hash"])
    payload = _read_json(json_path, {})
    return canonical_hash(_hash_scope(payload)) if isinstance(payload, dict) else ""


def _declared_hash(payload: dict[str, object]) -> str:
    hashes = payload.get("output_artifact_hashes")
    if isinstance(hashes, dict):
        value = hashes.get("pick_set_sha256")
        if isinstance(value, str):
            return value
    return ""


def _classify_hash_change(existing_path: Path, new_payload: dict[str, object]) -> str:
    existing_payload = _read_json(existing_path, {})
    if not isinstance(existing_payload, dict):
        return "manual_override_required"
    current_scope = (
        "canonical frozen pick payload excluding pick_set_hash and output_artifact_hashes"
    )
    existing_scope = existing_payload.get("hash_scope")
    if isinstance(existing_scope, str) and existing_scope != current_scope:
        return "hash_scope_changed"
    if existing_payload.get("data_snapshot_id") != new_payload.get("data_snapshot_id"):
        return "data_snapshot_changed"
    if existing_payload.get("code_version") != new_payload.get("code_version"):
        return "code_changed"
    if existing_payload.get("accepted_end_date") != new_payload.get("accepted_end_date"):
        return "expected_new_data"
    return "nondeterminism_detected"


def _pick_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for key in (
        "accepted_candidates",
        "blocked_candidates",
        "watchlist_candidates",
        "near_setup_candidates",
        "no_setup_explanations",
        "candidates",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    return rows


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = tuple(sorted({key for row in rows for key in row})) or ("empty",)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(_plain(payload), handle, indent=2, sort_keys=True)
        handle.write("\n")


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return " | ".join(str(item) for item in value)
    if isinstance(value, dict):
        return canonical_json(value)
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)


def _plain(value: object) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value
