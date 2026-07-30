from pathlib import Path

import pytest

from scripts.build_public import _resolve_repository_database


def test_persistence_database_must_stay_inside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "checkout"
    repository.mkdir()

    assert _resolve_repository_database(repository, "data/shadow.sqlite") == (
        repository / "data" / "shadow.sqlite"
    ).resolve()

    with pytest.raises(ValueError, match="must be inside the repository"):
        _resolve_repository_database(repository, str(tmp_path / "shared.sqlite"))
