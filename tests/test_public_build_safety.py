import os
from pathlib import Path

import pytest

from scripts import build_public
from scripts.build_public import (
    _promote_public_artifact,
    _PublicBuildOperation,
    _resolve_repository_database,
)
from scripts.public_artifact_inventory import (
    PUBLIC_ARTIFACT_FILES,
    PublicArtifactInventoryError,
    assert_exact_public_inventory,
)


def _write_exact_public(root: Path, marker: str) -> None:
    for relative in PUBLIC_ARTIFACT_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{marker}:{relative}", encoding="utf-8")


def test_persistence_database_must_stay_inside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "checkout"
    repository.mkdir()

    assert _resolve_repository_database(repository, "data/shadow.sqlite") == (
        repository / "data" / "shadow.sqlite"
    ).resolve()

    with pytest.raises(ValueError, match="must be inside an approved root"):
        _resolve_repository_database(repository, str(tmp_path / "shared.sqlite"))


def test_explicit_durable_state_root_is_an_approved_boundary(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "checkout"
    state = tmp_path / "state"
    repository.mkdir()
    state.mkdir()
    database = state / "shadow_real.sqlite"

    assert _resolve_repository_database(
        repository,
        str(database),
        state_root=state,
    ) == database.resolve()


def test_exact_public_inventory_rejects_any_extra_file(tmp_path: Path) -> None:
    public = tmp_path / "public"
    _write_exact_public(public, "safe")
    (public / "daily-finalize.jsonl").write_text("private", encoding="utf-8")

    with pytest.raises(PublicArtifactInventoryError, match="unexpected"):
        assert_exact_public_inventory(public)


def test_public_promotion_converges_crash_after_prior_directory_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = tmp_path / "public"
    _write_exact_public(final, "prior")
    operation = _PublicBuildOperation(tmp_path, final)
    stage = operation.begin(source_sha="a" * 40, market_date="2026-09-01")
    _write_exact_public(stage, "candidate")
    operation.mark("STAGED")
    operation.mark("PRE_SWAP")
    os.replace(final, operation.backup_root)
    operation.mark("PRIOR_MOVED")
    monkeypatch.setattr(
        build_public,
        "_is_recoverable_candidate_public_artifact",
        lambda path, journal: True,
    )

    operation.close()

    assert (final / "index.html").read_text(encoding="utf-8").startswith("candidate:")
    assert not operation.stage_root.exists()
    assert not operation.backup_root.exists()
    assert not operation.journal_path.exists()


def test_public_recovery_restores_prior_when_exact_shaped_stage_is_unverified(
    tmp_path: Path,
) -> None:
    final = tmp_path / "public"
    _write_exact_public(final, "prior")
    operation = _PublicBuildOperation(tmp_path, final)
    stage = operation.begin(source_sha="a" * 40, market_date="2026-09-01")
    _write_exact_public(stage, "attacker")
    operation.mark("STAGED")
    operation.mark("PRE_SWAP")
    os.replace(final, operation.backup_root)
    operation.mark("PRIOR_MOVED")

    operation.close()

    assert (final / "index.html").read_text(encoding="utf-8").startswith("prior:")
    assert not operation.stage_root.exists()
    assert not operation.backup_root.exists()
    assert not operation.journal_path.exists()


def test_public_promotion_rejects_ungoverned_previous_artifact(tmp_path: Path) -> None:
    final = tmp_path / "public"
    _write_exact_public(final, "prior")
    (final / "runtime.env").write_text("secret", encoding="utf-8")
    operation = _PublicBuildOperation(tmp_path, final)
    stage = operation.begin(source_sha="a" * 40, market_date="2026-09-01")
    _write_exact_public(stage, "candidate")
    operation.mark("STAGED")

    with pytest.raises(PublicArtifactInventoryError, match="previous public"):
        _promote_public_artifact(tmp_path, stage, final, operation=operation)
    with pytest.raises(PublicArtifactInventoryError, match="previous public"):
        operation.close()
    assert (final / "runtime.env").read_text(encoding="utf-8") == "secret"
