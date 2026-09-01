from __future__ import annotations

# The embedded PowerShell harness intentionally mirrors production command lines.
# Keep those lines readable in the test while linting the Python itself.
# ruff: noqa: E501
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intraday_scanner.storage.migrations import run_migrations
from scripts.capture_task_contract import (
    CAPTURE_TASK_NAME,
    CaptureTaskContractError,
    load_prepared,
    seal_prepared,
    seal_receipt,
)
from scripts.capture_task_contract import (
    self_hash as capture_self_hash,
)
from scripts.capture_task_contract import (
    validate_prepared as validate_capture_prepared,
)
from scripts.capture_task_contract import (
    validate_receipt as validate_capture_receipt,
)
from scripts.runtime_activation_contract import CI_SCHEMA, SOL_SCHEMA, seal_evidence
from scripts.state_disaster_recovery import (
    RecoveryValidationError,
    create_backup,
    validate_backup,
)
from scripts.state_disaster_recovery import (
    _load_json as load_recovery_json,
)
from scripts.state_preparation import (
    StatePreparationError,
    inspect_live,
    inspect_task_proof,
    inventory,
    prepare_state,
)
from scripts.state_preparation import (
    _load_receipt as load_state_receipt,
)
from scripts.state_preparation import (
    self_hash as state_self_hash,
)
from scripts.state_preparation import (
    validate_receipt as validate_state_receipt,
)

CANDIDATE_SHA = "a" * 40
CANDIDATE_TREE = "b" * 40


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _install_local_origin_fixture_seam(lock_script: Path) -> None:
    """Let a disposable candidate use its local bare remote in integration tests."""

    text = lock_script.read_text(encoding="utf-8")
    start_marker = "function Convert-DawnstrikeCanonicalOriginIdentity([string]$Origin) {"
    end_marker = "\nfunction New-DawnstrikeRuntimeLockPayload"
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    fixture = (
        f"{start_marker}\n"
        "    if ([string]::IsNullOrWhiteSpace($Origin)) { throw 'Fixture origin is empty.' }\n"
        "    return 'github.com/mattfren/dawnstrike'\n"
        "}\n"
    )
    lock_script.write_text(text[:start] + fixture + text[end:], encoding="utf-8")


def _install_capture_task_origin_fixture_seam(rebind_script: Path) -> None:
    """Bind the disposable rebind to the strict receipt's canonical origin."""

    text = rebind_script.read_text(encoding="utf-8")
    original = "-CandidateTree ([string]$runtimeContract.tree) -OriginUrl $origin "
    replacement = (
        "-CandidateTree ([string]$runtimeContract.tree) "
        '-OriginUrl "https://github.com/mattfren/DawnStrike.git" '
    )
    if text.count(original) != 2:
        raise AssertionError("capture rebind origin fixture seam is ambiguous")
    rebind_script.write_text(text.replace(original, replacement), encoding="utf-8")


def _install_capture_task_path_fixture_seam(
    safety_script: Path,
    *,
    db_path: Path,
    evidence_root: Path,
    run_root: Path,
    output_root: Path,
    session_root: Path,
    config_root: Path,
) -> None:
    """Bind the copied safety contract to isolated, exact fixture roots."""

    text = safety_script.read_text(encoding="utf-8")
    replacements = {
        r'C:\r\dawnstrike-forward-db\staging.sqlite': str(db_path),
        r"C:\r\dawnstrike-forward-evidence": str(evidence_root),
        r"C:\r\dawnstrike-forward-runs": str(run_root),
        r"C:\r\dawnstrike-forward-output": str(output_root),
        r"C:\r\dawnstrike-forward-sessions": str(session_root),
        r"C:\r\dawnstrike-capture-config-20260830": str(config_root),
    }
    for production_path, fixture_path in replacements.items():
        assert text.count(production_path) == 1
        text = text.replace(production_path, fixture_path)
    safety_script.write_text(text, encoding="utf-8")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _task_proof(path: Path, *, capture_present: bool = True) -> Path:
    value = {
        "schema_version": "dawnstrike.state_preparation_task_proof.v1",
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "task_count": 5,
        "canonical_running_count": 0,
        "canonical_enabled_count": 5,
        "capture_present": capture_present,
        "capture_running": False,
        "capture_state": "Disabled" if capture_present else "ABSENT",
        "capture_action": "DISABLED_UNTIL_EXACT_SHA_REBIND"
        if capture_present
        else "ABSENT_ALLOWED",
        "capture_xml_sha256": _sha("capture-xml") if capture_present else _sha(""),
        "capture_action_contract_sha256": _sha("capture-action") if capture_present else _sha(""),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _state_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    state = tmp_path / "state"
    state.mkdir()
    db = state / "shadow_real.sqlite"
    connection = sqlite3.connect(db)
    try:
        run_migrations(connection)
    finally:
        connection.close()
    proof = _task_proof(tmp_path / "task-proof.json")
    backup = tmp_path / "backups"
    return db, state, proof, backup


def _make_directory_reparse(link: Path, target: Path) -> None:
    """Create a disposable directory symlink, falling back to a Windows junction."""

    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            raise
    completed = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0 or not link.exists():
        raise OSError(
            f"directory junction creation failed: {completed.stdout} {completed.stderr}"
        )


def _remove_directory_reparse(link: Path) -> None:
    if not link.exists() and not link.is_symlink():
        return
    if link.is_symlink() or os.name != "nt":
        link.unlink()
    else:
        os.rmdir(link)


def _prepare(tmp_path: Path) -> tuple[dict[str, object], Path, Path, Path, Path]:
    db, state, proof, backup = _state_fixture(tmp_path)
    receipt = prepare_state(
        db,
        state_root=state,
        backup_root=backup,
        candidate_sha=CANDIDATE_SHA,
        candidate_tree=CANDIDATE_TREE,
        task_proof=proof,
    )
    return receipt, db, state, proof, backup


def test_state_preparation_is_additive_complete_and_idempotent(tmp_path: Path) -> None:
    receipt, db, state, proof, backup = _prepare(tmp_path)

    assert receipt["status"] == "COMPLETE"
    assert receipt["state_schema_version"] == 30
    assert receipt["initialization_idempotent"] is True
    assert len(str(receipt["before_db_sha256"])) == 64
    assert len(str(receipt["backup_db_sha256"])) == 64
    assert backup != state and (backup / str(receipt["backup_id"])).is_dir()

    second = prepare_state(
        db,
        state_root=state,
        backup_root=backup,
        candidate_sha=CANDIDATE_SHA,
        candidate_tree=CANDIDATE_TREE,
        task_proof=proof,
    )
    assert second == receipt
    with sqlite3.connect(db) as connection:
        current = inventory(connection)
    assert current["inventory_contract_sha256"] == receipt["inventory_sha256"]


def test_online_backup_is_exactly_three_files_and_hashes_logical_wal_view(
    tmp_path: Path,
) -> None:
    db, state, _proof, backup = _state_fixture(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE wal_fixture (value TEXT NOT NULL)")
        connection.execute("INSERT INTO wal_fixture(value) VALUES ('committed')")
        connection.commit()

    result = create_backup(
        db,
        backup,
        state_root=state,
        source_sha=CANDIDATE_SHA,
        backup_id="wal-three-file-fixture",
    )
    bundle = backup / result["backup_id"]
    assert {item.name for item in bundle.iterdir()} == {
        "shadow_real.sqlite",
        "manifest.json",
        "receipt.json",
    }
    assert result["backup_logical_snapshot_sha256"] == result[
        "source_logical_snapshot_sha256"
    ]
    assert validate_backup(bundle, backup_root=backup)[
        "backup_logical_snapshot_sha256"
    ] == result["backup_logical_snapshot_sha256"]
    assert {item.name for item in bundle.iterdir()} == {
        "shadow_real.sqlite",
        "manifest.json",
        "receipt.json",
    }


def test_online_backup_rejects_a_wal_commit_during_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, state, _proof, backup = _state_fixture(tmp_path)
    import scripts.state_disaster_recovery as recovery

    original = recovery._logical_snapshot_sha256
    calls = 0

    def inject_commit(path: Path, *, immutable: bool) -> str:
        nonlocal calls
        if path == db and not immutable:
            calls += 1
            if calls == 2:
                with sqlite3.connect(db) as connection:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("CREATE TABLE hostile_wal_commit (value TEXT)")
                    connection.execute("INSERT INTO hostile_wal_commit(value) VALUES ('late')")
                    connection.commit()
        return original(path, immutable=immutable)

    monkeypatch.setattr(recovery, "_logical_snapshot_sha256", inject_commit)
    with pytest.raises(RecoveryValidationError, match="logical snapshot.*changed"):
        create_backup(
            db,
            backup,
            state_root=state,
            source_sha=CANDIDATE_SHA,
            backup_id="wal-drift-fixture",
        )


def test_online_backup_requires_exact_locked_snapshot_hashes(tmp_path: Path) -> None:
    db, state, _proof, backup = _state_fixture(tmp_path)
    expected = inspect_live(db)
    with pytest.raises(RecoveryValidationError, match="expected locked proof"):
        create_backup(
            db,
            backup,
            state_root=state,
            source_sha=CANDIDATE_SHA,
            backup_id="locked-snapshot-mismatch",
            expected_db_sha256=expected["db_sha256"],
            expected_wal_sha256=expected["wal_sha256"],
            expected_shm_sha256=expected["shm_sha256"],
            expected_logical_snapshot_sha256="f" * 64,
        )


def test_inventory_rejects_weakened_pragma_column_contract(tmp_path: Path) -> None:
    _receipt, db, _state, _proof, _backup = _prepare(tmp_path)
    with sqlite3.connect(db) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            ("expected_market_sessions",),
        ).fetchone()[0]
        weakened = str(sql).replace("exchange TEXT NOT NULL", "exchange TEXT")
        assert weakened != sql
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (weakened, "expected_market_sessions"),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()
    connection.close()
    with sqlite3.connect(db) as connection:
        with pytest.raises(StatePreparationError, match="PRAGMA table_info contract"):
            inventory(connection)


def test_inventory_rejects_null_text_primary_key_identity(tmp_path: Path) -> None:
    _receipt, db, _state, _proof, _backup = _prepare(tmp_path)
    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            connection.execute(
                "INSERT INTO expected_market_sessions "
                "(session_id, market_date, exchange, session_open_utc, session_close_utc, "
                "status, calendar_source, calendar_source_hash_sha256, created_at, "
                "research_only, broker_execution_enabled, payload_json) "
                "VALUES (NULL, '2099-02-02', 'XNYS', '2099-02-02T14:30:00Z', "
                "'2099-02-02T21:00:00Z', 'EXPECTED', 'test', ?, '2099-02-02T00:00:00Z', 1, 0, '{}')",
                ("a" * 64,),
            )
    connection.close()


def test_migration_repairs_legacy_nullable_identity_without_data_loss(
    tmp_path: Path,
) -> None:
    _receipt, db, _state, _proof, _backup = _prepare(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO expected_market_sessions "
            "(session_id, market_date, exchange, session_open_utc, session_close_utc, "
            "status, calendar_source, calendar_source_hash_sha256, created_at, "
            "research_only, broker_execution_enabled, payload_json) "
            "VALUES ('legacy-session', '2099-02-03', 'XNYS', '2099-02-03T14:30:00Z', "
            "'2099-02-03T21:00:00Z', 'EXPECTED', 'test', ?, '2099-02-03T00:00:00Z', 1, 0, '{}')",
            ("b" * 64,),
        )
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            ("expected_market_sessions",),
        ).fetchone()[0]
        weakened = str(sql).replace(
            "session_id TEXT PRIMARY KEY NOT NULL", "session_id TEXT PRIMARY KEY"
        )
        assert weakened != sql
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (weakened, "expected_market_sessions"),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()
    connection.close()
    with sqlite3.connect(db) as connection:
        run_migrations(connection)
        identity = connection.execute(
            "PRAGMA table_info(expected_market_sessions)"
        ).fetchall()[0]
        assert (identity[1], identity[3], identity[5]) == ("session_id", 1, 1)
        assert connection.execute(
            "SELECT session_id FROM expected_market_sessions"
        ).fetchone()[0] == "legacy-session"
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            connection.execute(
                "INSERT INTO expected_market_sessions "
                "(session_id, market_date, exchange, session_open_utc, session_close_utc, "
                "status, calendar_source, calendar_source_hash_sha256, created_at, "
                "research_only, broker_execution_enabled, payload_json) "
                "VALUES (NULL, '2099-02-04', 'XNYS', '2099-02-04T14:30:00Z', "
                "'2099-02-04T21:00:00Z', 'EXPECTED', 'test', ?, '2099-02-04T00:00:00Z', 1, 0, '{}')",
                ("c" * 64,),
            )
        connection.commit()
        assert inventory(connection)["schema_marker"] == 30
    connection.close()


def test_inventory_rejects_comment_hidden_weakened_safety_check(tmp_path: Path) -> None:
    _receipt, db, _state, _proof, _backup = _prepare(tmp_path)
    with sqlite3.connect(db) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            ("expected_market_sessions",),
        ).fetchone()[0]
        weakened = str(sql).replace(
            "CHECK (research_only = 1)",
            "CHECK (research_only <> 0) /* research_only INTEGER NOT NULL CHECK (research_only = 1) */",
        )
        assert weakened != sql
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (weakened, "expected_market_sessions"),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()
    with sqlite3.connect(db) as connection:
        with pytest.raises(StatePreparationError, match="safety constraint"):
            inventory(connection)


@pytest.mark.parametrize("field", ["manifest", "receipt"])
def test_backup_rejects_contradictory_extra_keys(tmp_path: Path, field: str) -> None:
    _result, _db, _state, _proof, backup = _prepare(tmp_path)
    bundle = next(backup.glob("state-preparation-*"))
    path = bundle / f"{field}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["contradictory_extra"] = True
    _write_json(path, value)
    with pytest.raises(RecoveryValidationError, match="strict contract"):
        validate_backup(bundle, backup_root=backup)


def test_idempotent_preparation_revalidates_the_complete_backup_bundle(
    tmp_path: Path,
) -> None:
    _receipt, db, state, proof, backup = _prepare(tmp_path)
    bundle = next(backup.glob("state-preparation-*"))
    (bundle / "receipt.json").unlink()
    with pytest.raises(RecoveryValidationError, match="incomplete"):
        prepare_state(
            db,
            state_root=state,
            backup_root=backup,
            candidate_sha=CANDIDATE_SHA,
            candidate_tree=CANDIDATE_TREE,
            task_proof=proof,
        )


@pytest.mark.parametrize("loader", [load_recovery_json, load_state_receipt])
def test_json_loaders_reject_unescaped_and_case_colliding_duplicate_keys(
    tmp_path: Path, loader: object
) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"capture_running":false,"capture_\\u0072unning":false}\n',
        encoding="utf-8",
    )
    expected = (
        RecoveryValidationError
        if loader is load_recovery_json
        else StatePreparationError
    )
    with pytest.raises(expected, match="duplicate"):
        loader(path)  # type: ignore[operator]


def test_task_proof_loader_rejects_case_colliding_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-proof.json"
    path.write_text('{"task_count":5,"Task_Count":5}\n', encoding="utf-8")
    with pytest.raises(StatePreparationError, match="duplicate"):
        inspect_task_proof(path)


def test_state_preparation_fails_closed_on_wal_drift(tmp_path: Path) -> None:
    receipt, db, state, proof, backup = _prepare(tmp_path)
    wal = db.with_name("shadow_real.sqlite-wal")
    wal.write_bytes(b"unexpected wal drift")

    with pytest.raises(StatePreparationError, match="database hashes"):
        prepare_state(
            db,
            state_root=state,
            backup_root=backup,
            candidate_sha=CANDIDATE_SHA,
            candidate_tree=CANDIDATE_TREE,
            task_proof=proof,
        )
    assert receipt["status"] == "COMPLETE"


def test_state_preparation_backup_and_receipt_descendant_reparse_fail_closed(
    tmp_path: Path,
) -> None:
    receipt, _db, state, _proof, backup = _prepare(tmp_path)
    bundle = backup / str(receipt["backup_id"])
    outside = tmp_path / "outside-backup-bundle"
    shutil.copytree(bundle, outside)
    shutil.rmtree(bundle)
    try:
        _make_directory_reparse(bundle, outside)
        with pytest.raises(RecoveryValidationError, match="reparse"):
            validate_backup(bundle, backup_root=backup)
    finally:
        _remove_directory_reparse(bundle)

    reparse_root = tmp_path / "receipt-reparse"
    reparse_root.mkdir()
    db, state2, proof, backup2 = _state_fixture(reparse_root)
    receipts = state2 / "receipts"
    receipts.mkdir()
    target = tmp_path / "receipt-target"
    target.mkdir()
    prep_dir = receipts / "state-preparation"
    try:
        _make_directory_reparse(prep_dir, target)
        with pytest.raises(StatePreparationError, match="reparse"):
            prepare_state(
                db,
                state_root=state2,
                backup_root=backup2,
                candidate_sha=CANDIDATE_SHA,
                candidate_tree=CANDIDATE_TREE,
                task_proof=proof,
            )
    finally:
        _remove_directory_reparse(prep_dir)


def test_state_preparation_accepts_only_its_owned_atomic_lock(tmp_path: Path) -> None:
    db, state, proof, backup = _state_fixture(tmp_path)
    lock_root = state / "locks"
    lock_root.mkdir()
    lock = lock_root / "dawnstrike-runtime-activation.lock"
    _write_json(
        lock,
        {
            "schema_version": "dawnstrike.runtime_activation_lock.v1",
            "lock_token": "d" * 32,
            "research_only": True,
            "broker_execution_enabled": False,
        },
    )
    receipt = prepare_state(
        db,
        state_root=state,
        backup_root=backup,
        candidate_sha=CANDIDATE_SHA,
        candidate_tree=CANDIDATE_TREE,
        task_proof=proof,
        preparation_lock=lock,
    )
    assert receipt["status"] == "COMPLETE"
    (state / "receipts" / "state-preparation" / f"state-preparation-{CANDIDATE_SHA}.json").unlink()
    (lock_root / "unexpected.lock").write_text("active", encoding="utf-8")
    with pytest.raises(StatePreparationError, match="no active locks"):
        prepare_state(
            db,
            state_root=state,
            backup_root=backup,
            candidate_sha=CANDIDATE_SHA,
            candidate_tree=CANDIDATE_TREE,
            task_proof=proof,
            preparation_lock=lock,
        )


def test_state_inventory_requires_canonical_indexes_and_live_snapshot(tmp_path: Path) -> None:
    receipt, db, _state, _proof, _backup = _prepare(tmp_path)
    assert len(str(receipt["after_logical_snapshot_sha256"])) == 64
    live = inspect_live(db)
    assert live["logical_snapshot_sha256"] == receipt["after_logical_snapshot_sha256"]
    with sqlite3.connect(db) as connection:
        connection.execute("DROP INDEX idx_experiment_trial_ledger_attempt")
        connection.commit()
        with pytest.raises(StatePreparationError, match="expression index"):
            inventory(connection)


def test_inspect_live_cli_requires_only_database_argument(tmp_path: Path) -> None:
    _receipt, db, _state, _proof, _backup = _prepare(tmp_path)
    result = subprocess.run(
        [sys.executable, "scripts/state_preparation.py", "--db-path", str(db), "--inspect-live"],
        cwd=Path.cwd(),
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(result.stdout)
    assert value["quick_check"] == "ok"
    assert len(value["logical_snapshot_sha256"]) == 64


def test_direct_state_clis_prefer_candidate_modules_over_stale_pythonpath(
    tmp_path: Path,
) -> None:
    """A mounted runtime package must not bypass the candidate migration repair."""

    db, state, proof, backup = _state_fixture(tmp_path)
    # Reproduce the published schema-30 identity spelling from the mounted
    # runtime without weakening any non-identity constraint or touching rows.
    identity_columns = {
        "expected_market_sessions": "session_id",
        "intraday_capture_runs": "capture_run_id",
        "committed_fill_truth_receipts": "receipt_id",
        "no_trade_session_receipts": "receipt_id",
        "experiment_trial_ledger": "trial_id",
    }
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        try:
            for table, identity in identity_columns.items():
                sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()[0]
                weakened = str(sql).replace(
                    f"{identity} TEXT PRIMARY KEY NOT NULL",
                    f"{identity} TEXT PRIMARY KEY",
                )
                assert weakened != sql
                connection.execute(
                    "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
                    (weakened, table),
                )
        finally:
            connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()

    stale = tmp_path / "stale-package"
    stale_storage = stale / "intraday_scanner" / "storage"
    stale_storage.mkdir(parents=True)
    (stale / "intraday_scanner" / "__init__.py").write_text("\n", encoding="utf-8")
    (stale_storage / "__init__.py").write_text("\n", encoding="utf-8")
    (stale_storage / "migrations.py").write_text(
        "raise RuntimeError('stale migration package selected')\n", encoding="utf-8"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(stale)

    result = subprocess.run(
        [
            sys.executable,
            str((Path.cwd() / "scripts" / "prepare_dawnstrike_state.py").resolve()),
            "--db-path",
            str(db),
            "--state-root",
            str(state),
            "--backup-root",
            str(backup),
            "--candidate-sha",
            CANDIDATE_SHA,
            "--candidate-tree",
            CANDIDATE_TREE,
            "--task-proof",
            str(proof),
        ],
        cwd=stale,
        env=environment,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    preparation = json.loads(result.stdout.strip().splitlines()[-1])
    assert preparation["status"] == "COMPLETE"
    assert preparation["state_schema_version"] == 30

    for script, arguments in (
        ("state_preparation.py", ["--db-path", str(db), "--inspect-live"]),
        (
            "runtime_activation_contract.py",
            ["inspect-state", "--db-path", str(db)],
        ),
    ):
        inspected = subprocess.run(
            [
                sys.executable,
                str((Path.cwd() / "scripts" / script).resolve()),
                *arguments,
            ],
            cwd=stale,
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert inspected.returncode == 0, (script, inspected.stdout, inspected.stderr)
        payload = json.loads(inspected.stdout.strip().splitlines()[-1])
        assert payload["quick_check"] == "ok"
        if script == "runtime_activation_contract.py":
            assert payload["schema_version"] == 30


def test_state_preparation_rejects_partial_inventory_then_repairs_idempotently(
    tmp_path: Path,
) -> None:
    receipt, db, state, proof, backup = _prepare(tmp_path)
    (state / "receipts" / "state-preparation" / f"state-preparation-{CANDIDATE_SHA}.json").unlink()
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TRIGGER expected_market_sessions_no_update")
        connection.commit()
        with pytest.raises(StatePreparationError, match="triggers are missing"):
            inventory(connection)

    repaired = prepare_state(
        db,
        state_root=state,
        backup_root=backup,
        candidate_sha=CANDIDATE_SHA,
        candidate_tree=CANDIDATE_TREE,
        task_proof=proof,
    )
    assert repaired["status"] == "COMPLETE"
    assert repaired["inventory_sha256"] == receipt["inventory_sha256"]


def test_state_preparation_retains_recovery_evidence_on_injected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, state, proof, backup = _state_fixture(tmp_path)
    import scripts.state_disaster_recovery as recovery

    def fail_restore(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected restore verification failure")

    monkeypatch.setattr(recovery, "restore_verify", fail_restore)
    with pytest.raises(StatePreparationError, match="recovery evidence"):
        prepare_state(
            db,
            state_root=state,
            backup_root=backup,
            candidate_sha=CANDIDATE_SHA,
            candidate_tree=CANDIDATE_TREE,
            task_proof=proof,
        )
    failed = (
        state / "receipts" / "state-preparation" / f"state-preparation-{CANDIDATE_SHA}.failed.json"
    )
    evidence = json.loads(failed.read_text(encoding="utf-8"))
    assert evidence["status"] == "FAILED_BEFORE_COMPLETE"
    assert evidence["recovery_evidence"] == "online_backup_restore_verify_required"
    assert list(backup.glob("state-preparation-*"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("canonical_running_count", 1, "not be running"),
        ("capture_state", "Ready", "present and Disabled"),
        ("capture_running", True, "present and Disabled"),
        ("research_only", False, "safety flags"),
        ("broker_execution_enabled", True, "safety flags"),
    ],
)
def test_task_proof_hostile_mutations_fail_closed(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    proof = _task_proof(tmp_path / "task-proof.json")
    payload = json.loads(proof.read_text(encoding="utf-8"))
    payload[field] = value
    proof.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StatePreparationError, match=message):
        inspect_task_proof(proof)


def test_task_proof_is_bound_to_candidate_and_absence_policy(tmp_path: Path) -> None:
    proof = _task_proof(tmp_path / "task-proof.json", capture_present=False)
    assert inspect_task_proof(
        proof, candidate_sha=CANDIDATE_SHA, candidate_tree=CANDIDATE_TREE
    )["capture_action"] == "ABSENT_ALLOWED"
    payload = json.loads(proof.read_text(encoding="utf-8"))
    payload["candidate_sha"] = "c" * 40
    _write_json(proof, payload)
    with pytest.raises(StatePreparationError, match="candidate_sha"):
        inspect_task_proof(proof, candidate_sha=CANDIDATE_SHA, candidate_tree=CANDIDATE_TREE)


def _capture_receipt() -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "dawnstrike.capture_task_rebind_receipt.v1",
        "status": "COMPLETE",
        "task_name": CAPTURE_TASK_NAME,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "activation_id": "1" * 24,
        "activation_receipt_name": "runtime-activation-" + ("1" * 24) + ".json",
        "activation_receipt_sha256": _sha("activation-receipt"),
        "runtime_origin_sha256": _sha("origin"),
        "previous_candidate_sha": "c" * 40,
        "xml_before_sha256": _sha("xml-before"),
        "xml_after_sha256": _sha("xml-after"),
        "action_before_sha256": _sha("action-before"),
        "action_after_sha256": _sha("action-after"),
        "definition_before_sha256": _sha("definition-before"),
        "definition_after_sha256": _sha("definition-after"),
        "principal_sha256": _sha("principal"),
        "trigger_sha256": _sha("trigger"),
        "settings_sha256": _sha("settings"),
        "symbols_manifest_sha256": _sha("symbols"),
        "entitlement_receipt_sha256": _sha("entitlement"),
        "source_config_sha256": _sha("source"),
        "enablement_before": "Disabled",
        "enablement_after": "Ready",
        "changed_field": "candidate_sha",
        "preserved_contract": True,
        "completed_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    value["receipt_sha256"] = capture_self_hash(value, "receipt_sha256")
    return value


def test_capture_receipt_rejects_xml_action_and_safety_tamper() -> None:
    receipt = _capture_receipt()
    assert validate_capture_receipt(receipt, candidate_sha=CANDIDATE_SHA)
    for field, value in (
        ("xml_after_sha256", "f" * 64),
        ("action_after_sha256", "e" * 64),
        ("principal_sha256", "d" * 64),
        ("trigger_sha256", "c" * 64),
        ("settings_sha256", "b" * 64),
    ):
        tampered = dict(receipt)
        tampered[field] = value
        with pytest.raises(CaptureTaskContractError, match="self-hash"):
            validate_capture_receipt(tampered)
    for field, value, message in (
        ("research_only", False, "safety flags"),
        ("broker_execution_enabled", True, "safety flags"),
        ("enablement_before", "Ready", "disabled before"),
        ("changed_field", "principal", "more than candidate"),
    ):
        tampered = dict(receipt)
        tampered[field] = value
        tampered["receipt_sha256"] = capture_self_hash(tampered, "receipt_sha256")
        with pytest.raises(CaptureTaskContractError, match=message):
            validate_capture_receipt(tampered)


def test_capture_receipt_writer_rejects_reparse_output_root(tmp_path: Path) -> None:
    target = tmp_path / "outside-receipts"
    target.mkdir()
    link = tmp_path / "receipt-link"
    try:
        _make_directory_reparse(link, target)
        with pytest.raises(CaptureTaskContractError, match="reparse"):
            seal_receipt(_capture_receipt(), link / "capture.json")
    finally:
        _remove_directory_reparse(link)


def _capture_prepared(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": "dawnstrike.capture_task_rebind_prepared.v1",
        "status": "PREPARED",
        "task_name": CAPTURE_TASK_NAME,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "activation_id": "1" * 24,
        "activation_receipt_name": "runtime-activation-" + ("1" * 24) + ".json",
        "activation_receipt_sha256": _sha("activation-receipt"),
        "previous_candidate_sha": "c" * 40,
        "xml_before_sha256": _sha("xml-before"),
        "action_before_sha256": _sha("action-before"),
        "definition_before_sha256": _sha("definition-before"),
        "normalized_definition_before_sha256": _sha("normalized-definition-before"),
        "principal_sha256": _sha("principal"),
        "trigger_sha256": _sha("trigger"),
        "settings_sha256": _sha("settings"),
        "symbols_manifest_path": str((tmp_path / "symbols.json").resolve()),
        "symbols_manifest_sha256": _sha("symbols"),
        "entitlement_receipt_path": str((tmp_path / "entitlement.json").resolve()),
        "entitlement_receipt_sha256": _sha("entitlement"),
        "source_config_path": str((tmp_path / "source-config.json").resolve()),
        "source_config_sha256": _sha("source"),
        "enablement_before": "Disabled",
        "compensation": "RESTORE_EXACT_XML_AND_DISABLED",
        "prepared_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def test_capture_prepared_contract_is_self_hashed_and_exact(tmp_path: Path) -> None:
    payload = _capture_prepared(tmp_path)
    prepared_path = tmp_path / "capture.prepared.json"
    sealed = seal_prepared(payload, prepared_path)
    assert sealed["prepared_sha256"]
    assert load_prepared(
        prepared_path, candidate_sha=CANDIDATE_SHA, candidate_tree=CANDIDATE_TREE
    )["status"] == "PREPARED"
    tampered = dict(sealed)
    tampered["source_config_sha256"] = _sha("hostile-mismatch")
    with pytest.raises(CaptureTaskContractError, match="self-hash"):
        validate_capture_prepared(tampered)


def test_capture_contract_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"status":"COMPLETE","status":"PREPARED"}\n', encoding="utf-8")
    with pytest.raises(CaptureTaskContractError, match="duplicate JSON field"):
        load_prepared(duplicate)


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_powershell_lock_handshake_rejects_both_lock_orders(tmp_path: Path) -> None:
    source = Path.cwd()
    stage_q = str(source / "scripts" / "invoke_dawnstrike_stage.ps1").replace("'", "''")
    activation_q = str(source / "scripts" / "activate_dawnstrike_runtime.ps1").replace("'", "''")
    state_q = str(tmp_path / "state").replace("'", "''")
    command = rf"""
. '{activation_q}'
. '{stage_q}'
$state = '{state_q}'
New-Item -ItemType Directory -Path $state -Force | Out-Null
try {{
    $dailyFirst = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate '2026-08-31' -Owner 'normal_stage'
    if (-not $dailyFirst.acquired) {{ throw 'daily-first lock was not acquired' }}
    $dailyFirstFailure = ''
    try {{ $null = Enter-DawnstrikeRuntimeActivationLock -StateRoot $state; throw 'activation acquired during daily-first order' }} catch {{ $dailyFirstFailure = $_.Exception.Message }}
    if ($dailyFirstFailure -notmatch 'daily run lock') {{ throw ('unexpected daily-first failure: ' + $dailyFirstFailure) }}
    Exit-DawnstrikeDailyRunLock $dailyFirst
    $activationFirst = Enter-DawnstrikeRuntimeActivationLock -StateRoot $state
    $normalAfterActivation = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate '2026-08-31' -Owner 'normal_stage'
    if ($normalAfterActivation.acquired -or $normalAfterActivation.reason -notmatch 'runtime_activation') {{ throw 'normal stage acquired during activation-first order' }}
    Exit-DawnstrikeRuntimeActivationLock $activationFirst
    [pscustomobject]@{{ daily_first = $dailyFirst.acquired; activation_first = $activationFirst.acquired; activation_reason = $normalAfterActivation.reason }} | ConvertTo-Json -Compress
}} finally {{ if (Test-Path -LiteralPath $state) {{ Remove-Item -LiteralPath $state -Recurse -Force }} }}
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=source,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "daily_first": True,
        "activation_first": True,
        "activation_reason": "runtime_activation_lock",
    }


def test_state_receipt_candidate_and_broker_tamper_fail_closed(tmp_path: Path) -> None:
    receipt, _db, _state, _proof, _backup = _prepare(tmp_path)
    with pytest.raises(StatePreparationError, match="candidate SHA"):
        validate_state_receipt(receipt, candidate_sha="f" * 40, candidate_tree=CANDIDATE_TREE)
    tampered = dict(receipt)
    tampered["broker_execution_enabled"] = True
    tampered["receipt_sha256"] = state_self_hash(tampered, "receipt_sha256")
    with pytest.raises(StatePreparationError, match="safety flags"):
        validate_state_receipt(tampered)


def test_state_receipt_rejects_backup_bundle_outside_supplied_root(tmp_path: Path) -> None:
    receipt, db, state, proof, backup = _prepare(tmp_path)
    outside = tmp_path / str(receipt["backup_id"])
    shutil.copytree(backup / str(receipt["backup_id"]), outside)
    receipt_path = state / "receipts" / "state-preparation" / f"state-preparation-{CANDIDATE_SHA}.json"
    forged = dict(receipt)
    forged["backup_bundle_path"] = str(outside)
    forged["receipt_sha256"] = state_self_hash(forged, "receipt_sha256")
    _write_json(receipt_path, forged)
    with pytest.raises(StatePreparationError, match="outside the supplied backup root"):
        prepare_state(
            db,
            state_root=state,
            backup_root=backup,
            candidate_sha=CANDIDATE_SHA,
            candidate_tree=CANDIDATE_TREE,
            task_proof=proof,
        )


def test_capture_rebind_journal_has_guarded_process_kill_seams() -> None:
    script = Path("scripts/rebind_intraday_capture_task.ps1").read_text(encoding="utf-8")
    for marker in (
        'DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_init"',
        'DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_set"',
        'DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_post_enable"',
        'DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_complete"',
        'DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1"',
        "Enter-DawnstrikeGovernedRuntimeLockWithJournal",
        "Adopt-DawnstrikeGovernedRuntimeLockWithJournal",
        "-Phase PRE_ENABLE",
        "-Phase POST_ENABLE",
        "-Phase COMPLETE",
        "Existing COMPLETE capture-task receipt has no durable operation journal.",
    ):
        assert marker in script
    assert script.count("-Phase PRE_ENABLE") >= 1
    assert script.count("-Phase POST_ENABLE") >= 1
    assert script.count("-Phase COMPLETE") >= 2


@pytest.mark.parametrize("initial_aux_state", ["Disabled", "Ready"])
@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_powershell_sidecar_activation_and_rollback_keep_auxiliary_disabled(
    tmp_path: Path, initial_aux_state: str
) -> None:
    """Exercise the governed six-task boundary with a deterministic scheduler mock."""

    source = Path.cwd()
    candidate = tmp_path / "candidate"
    runtime = tmp_path / "dawnstrike-runtime"
    state = tmp_path / "state"
    backup = tmp_path / "backups"
    remote = tmp_path / "origin.git"
    forward_db = tmp_path / "forward-db" / "staging.sqlite"
    forward_evidence = tmp_path / "forward-evidence"
    forward_runs = tmp_path / "forward-runs"
    forward_output = tmp_path / "forward-output"
    forward_sessions = tmp_path / "forward-sessions"
    capture_config = tmp_path / "capture-config"
    candidate.mkdir()
    runtime.mkdir()
    state.mkdir()

    (candidate / "scripts").mkdir()
    for name in (
        "activate_dawnstrike_runtime.ps1",
        "runtime_activation_lock.ps1",
            "runtime_activation_lock_contract.py",
            "runtime_operation_journal.py",
        "runtime_operation_journal.py",
        "capture_task_safety.ps1",
        "rollback_dawnstrike_runtime.ps1",
        "rebind_intraday_capture_task.ps1",
        "runtime_activation_contract.py",
        "state_preparation.py",
        "prepare_dawnstrike_state.ps1",
        "prepare_dawnstrike_state.py",
        "capture_task_contract.py",
        "capture_task_hardening_contract.py",
        "resolve_dawnstrike_task_principal.ps1",
        "dawnstrike_job_process.ps1",
        "invoke_dawnstrike_stage.ps1",
        "state_disaster_recovery.py",
        "run_daily_intraday_capture.py",
    ):
        shutil.copy2(source / "scripts" / name, candidate / "scripts" / name)
    _install_local_origin_fixture_seam(candidate / "scripts" / "runtime_activation_lock.ps1")
    _install_capture_task_origin_fixture_seam(
        candidate / "scripts" / "rebind_intraday_capture_task.ps1"
    )
    _install_capture_task_path_fixture_seam(
        candidate / "scripts" / "capture_task_safety.ps1",
        db_path=forward_db,
        evidence_root=forward_evidence,
        run_root=forward_runs,
        output_root=forward_output,
        session_root=forward_sessions,
        config_root=capture_config,
    )
    shutil.copytree(
        source / "intraday_scanner",
        candidate / "intraday_scanner",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    (candidate / "config").mkdir()
    shutil.copy2(
        source / "config" / "state_preparation_contract.json",
        candidate / "config" / "state_preparation_contract.json",
    )
    shutil.copy2(source / ".gitignore", candidate / ".gitignore")

    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(candidate)],
        check=True,
        capture_output=True,
    )
    _git(candidate, "config", "user.email", "capture-test@example.invalid")
    _git(candidate, "config", "user.name", "Capture Test")
    _git(candidate, "add", ".")
    _git(candidate, "commit", "-m", "capture candidate")
    _git(candidate, "remote", "add", "origin", str(remote))
    _git(candidate, "push", "-u", "origin", "main")
    candidate_sha = _git(candidate, "rev-parse", "HEAD")
    candidate_tree = _git(candidate, "rev-parse", "HEAD^{tree}")

    subprocess.run(
        ["git", "init", "--initial-branch=main", str(runtime)],
        check=True,
        capture_output=True,
    )
    _git(runtime, "config", "user.email", "capture-test@example.invalid")
    _git(runtime, "config", "user.name", "Capture Test")
    (runtime / "previous.txt").write_text("previous\n", encoding="utf-8")
    (runtime / "scripts").mkdir()
    shutil.copy2(source / "scripts" / "run_daily_intraday_capture.py", runtime / "scripts")
    _git(runtime, "add", "previous.txt", "scripts/run_daily_intraday_capture.py")
    _git(runtime, "commit", "-m", "previous")
    _git(runtime, "remote", "add", "origin", str(remote))
    previous_sha = _git(runtime, "rev-parse", "HEAD")
    previous_tree = _git(runtime, "rev-parse", "HEAD^{tree}")

    with sqlite3.connect(state / "shadow_real.sqlite") as connection:
        run_migrations(connection)
    evidence = state / "evidence"
    evidence.mkdir()
    completed = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    ci = {
        "schema_version": CI_SCHEMA,
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "conclusion": "SUCCESS",
        "status": "COMPLETED",
        "head_branch": "main",
        "run_url": "https://github.com/example/dawnstrike/actions/runs/12345",
        "checks_total": 19,
        "checks_succeeded": 19,
        "completed_at_utc": completed,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    sol = {
        "schema_version": SOL_SCHEMA,
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "auditor_model": "gpt-5.6-sol",
        "verdict": "ZERO_CRITICAL_HIGH",
        "critical_findings": 0,
        "high_findings": 0,
        "completed_at_utc": completed,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    _write_json(evidence / "ci.json", seal_evidence(ci))
    _write_json(evidence / "sol.json", seal_evidence(sol))
    for directory in (
        forward_db.parent,
        forward_evidence,
        forward_runs,
        forward_output,
        forward_sessions,
        capture_config,
        state / "secrets",
        state / "capture-bytecode" / candidate_sha,
        state / "capture-bytecode" / previous_sha,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    forward_db.write_bytes(b"")
    (state / "secrets" / "runtime.env").write_text("", encoding="utf-8")
    symbols_manifest = capture_config / "symbols.json"
    entitlement_receipt = capture_config / "entitlement.json"
    source_config = capture_config / "source-config.json"
    _write_json(
        symbols_manifest,
        {
            "membership_policy": "research_control_only",
            "point_in_time_membership": "research_control_only",
            "symbols": ["AAPL"],
        },
    )
    _write_json(
        entitlement_receipt,
        {
            "provider": "alpaca",
            "feed": "sip",
            "probe_status": "PASS",
            "proven_endpoints": ["bars", "trades", "quotes"],
            "retention_allowed": True,
            "approved_plan": True,
            "research_only": True,
            "broker_execution": "disabled",
            "entitlement": "fixture-entitlement",
            "receipt": "fixture-receipt",
        },
    )
    _write_json(source_config, {"source": "fixture", "research_only": True})

    def file_sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def quote(path: Path) -> str:
        return str(path).replace("'", "''")

    candidate_q = quote(candidate)
    runtime_q = quote(runtime)
    state_q = quote(state)
    backup_q = quote(backup)
    ci_q = quote(evidence / "ci.json")
    sol_q = quote(evidence / "sol.json")
    symbols_q = quote(symbols_manifest)
    entitlement_q = quote(entitlement_receipt)
    source_config_q = quote(source_config)
    forward_db_q = quote(forward_db)
    forward_evidence_q = quote(forward_evidence)
    forward_runs_q = quote(forward_runs)
    forward_output_q = quote(forward_output)
    forward_sessions_q = quote(forward_sessions)
    env_file_q = quote(state / "secrets" / "runtime.env")
    bytecode_prefix_q = quote(state / "capture-bytecode" / candidate_sha)
    runtime_runner_q = quote(runtime / "scripts" / "run_daily_intraday_capture.py")
    symbols_sha = file_sha(symbols_manifest)
    entitlement_sha = file_sha(entitlement_receipt)
    source_config_sha = file_sha(source_config)
    runner_sha = file_sha(candidate / "scripts" / "run_daily_intraday_capture.py")
    interpreter_declaration = json.loads(
        (candidate / "config" / "state_preparation_contract.json").read_text(encoding="utf-8")
    )
    activation_q = quote(candidate / "scripts" / "activate_dawnstrike_runtime.ps1")
    rollback_q = quote(candidate / "scripts" / "rollback_dawnstrike_runtime.ps1")
    prep_q = quote(candidate / "scripts" / "prepare_dawnstrike_state.ps1")
    command = rf"""
    . '{activation_q}'
    $global:MockRuntime = '{runtime_q}'
$global:MockState = '{state_q}'
    $global:MockAuxState = 'Disabled'
$global:MockTaskStates = @{{}}
$global:TaskEvents = @()
foreach ($name in $script:DawnstrikeCanonicalTaskNames) {{ $global:MockTaskStates[$name] = 'Ready' }}
    $global:MockAuxPrincipal = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $global:MockAuxExecute = '{interpreter_declaration["capture_interpreter_path"]}'
    function ConvertTo-TestXmlText {{
      param([string]$Value)
      [System.Security.SecurityElement]::Escape($Value)
    }}
    function New-TestCaptureTaskXml {{
      param([string]$BoundSha, [bool]$Enabled)
      $bytecodePrefix = Join-Path '{state_q}' ('capture-bytecode\' + $BoundSha)
      $tokens = @(
        '-I', '-B', '-X', ('pycache_prefix=' + $bytecodePrefix), '-u', '{runtime_runner_q}',
        '--candidate-sha', $BoundSha,
        '--repo-root', '{runtime_q}',
        '--db-path', '{forward_db_q}',
        '--evidence-root', '{forward_evidence_q}',
        '--run-root', '{forward_runs_q}',
        '--output-root', '{forward_output_q}',
        '--session-root', '{forward_sessions_q}',
        '--symbols-manifest', '{symbols_q}',
        '--symbols-manifest-sha256', '{symbols_sha}',
        '--entitlement-receipt', '{entitlement_q}',
        '--entitlement-receipt-sha256', '{entitlement_sha}',
        '--source-config', '{source_config_q}',
        '--source-config-sha256', '{source_config_sha}',
        '--env-file', '{env_file_q}',
        '--max-pages', '100',
        '--retries', '3',
        '--execute'
      )
      $arguments = (($tokens | ForEach-Object {{ '"' + [string]$_ + '"' }}) -join ' ')
      $enabledText = if ($Enabled) {{ 'true' }} else {{ 'false' }}
      $xml = '<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">' +
        '<RegistrationInfo><Description>Dawnstrike delayed SIP research capture; no broker execution.</Description><URI>\Dawnstrike Delayed SIP Capture</URI></RegistrationInfo>' +
        '<Principals><Principal id="Author"><UserId>' + (ConvertTo-TestXmlText $global:MockAuxPrincipal) + '</UserId><LogonType>Password</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>' +
        '<Settings><Enabled>' + $enabledText + '</Enabled><StartWhenAvailable>true</StartWhenAvailable><WakeToRun>true</WakeToRun><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT3H</ExecutionTimeLimit><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><RestartOnFailure><Interval>PT15M</Interval><Count>3</Count></RestartOnFailure><UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine></Settings>' +
        '<Triggers><CalendarTrigger><StartBoundary>2026-08-31T15:20:00-05:00</StartBoundary><Enabled>true</Enabled><ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek><Monday/><Tuesday/><Wednesday/><Thursday/><Friday/></DaysOfWeek></ScheduleByWeek></CalendarTrigger></Triggers>' +
        '<Actions Context="Author"><Exec><Command>' + (ConvertTo-TestXmlText $global:MockAuxExecute) + '</Command><Arguments>' + (ConvertTo-TestXmlText $arguments) + '</Arguments><WorkingDirectory>' + (ConvertTo-TestXmlText '{runtime_q}') + '</WorkingDirectory></Exec></Actions></Task>'
      $document = [System.Xml.XmlDocument]::new()
      $document.PreserveWhitespace = $true
      $document.LoadXml($xml)
      return [string]$document.OuterXml
    }}
    $preHardeningXml = New-TestCaptureTaskXml -BoundSha '{previous_sha}' -Enabled:${str(initial_aux_state == "Ready").lower()}
    $global:MockAuxXml = New-TestCaptureTaskXml -BoundSha '{candidate_sha}' -Enabled:$false
    $mockDocument = [System.Xml.XmlDocument]::new()
    $mockDocument.LoadXml($global:MockAuxXml)
    $global:MockAuxArguments = [string](@($mockDocument.SelectNodes("//*[local-name()='Arguments']"))[0].InnerText)
    $fixtureSecure = [System.Security.SecureString]::new()
    $fixtureSecure.AppendChar('f'); $fixtureSecure.AppendChar('i'); $fixtureSecure.AppendChar('x'); $fixtureSecure.MakeReadOnly()
    $global:TestCredential = [PSCredential]::new($global:MockAuxPrincipal, $fixtureSecure)
$global:MockAuxLastTaskResult = 267011
$global:MockAuxLastRunTime = [DateTime]::MinValue
function Get-ScheduledTask {{
  [CmdletBinding()] param([string]$TaskName)
  if ($TaskName -eq 'Dawnstrike Delayed SIP Capture') {{
        return [pscustomobject]@{{ State=$global:MockAuxState; TaskPath='\'; Actions=@([pscustomobject]@{{ Execute=$global:MockAuxExecute; Arguments=$global:MockAuxArguments; WorkingDirectory=$global:MockRuntime }}) }}
  }}
  return [pscustomobject]@{{ State=$global:MockTaskStates[$TaskName]; TaskPath='\'; Actions=@([pscustomobject]@{{ Execute='powershell.exe'; Arguments=('-RuntimeRoot "' + $global:MockRuntime + '" -StateRoot "' + $global:MockState + '"'); WorkingDirectory=$global:MockRuntime }}) }}
}}
function Export-ScheduledTask {{
  [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
  if ($TaskName -eq 'Dawnstrike Delayed SIP Capture') {{ return $global:MockAuxXml }}
  $enabled = if ($global:MockTaskStates[$TaskName] -eq 'Disabled') {{ 'false' }} else {{ 'true' }}
  return "<Task><Name>$TaskName</Name><Runtime>$global:MockRuntime</Runtime><State>$global:MockState</State><Settings><Enabled>$enabled</Enabled></Settings></Task>"
}}
function Get-ScheduledTaskInfo {{
  [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
  [pscustomobject]@{{ LastTaskResult=$global:MockAuxLastTaskResult; LastRunTime=$global:MockAuxLastRunTime }}
}}
function Unregister-ScheduledTask {{
  [CmdletBinding()] param([string]$TaskName,[string]$TaskPath,[switch]$Confirm)
  if ($TaskName -eq 'Dawnstrike Delayed SIP Capture') {{ $global:MockAuxState='Missing' }}
}}
function Set-TestAuxEnabled {{
  param([bool]$Enabled)
  $document = [System.Xml.XmlDocument]::new()
  $document.PreserveWhitespace = $true
  $document.LoadXml($global:MockAuxXml)
  $settings = @($document.SelectNodes("/*[local-name()='Task']/*[local-name()='Settings']/*[local-name()='Enabled']"))
  if ($settings.Count -ne 1) {{ throw 'Fixture auxiliary Settings/Enabled is ambiguous.' }}
  $settings[0].InnerText = if ($Enabled) {{ 'true' }} else {{ 'false' }}
  $global:MockAuxXml = [string]$document.OuterXml
}}
function Disable-ScheduledTask {{
  [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
  if ($TaskName -eq 'Dawnstrike Delayed SIP Capture') {{ $global:MockAuxState='Disabled'; Set-TestAuxEnabled $false; $global:TaskEvents += 'disable:aux' }} else {{ $global:MockTaskStates[$TaskName]='Disabled'; $global:TaskEvents += ('disable:' + $TaskName) }}
  [pscustomobject]@{{ TaskName=$TaskName }}
}}
    function Enable-ScheduledTask {{
  [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
  if ($TaskName -eq 'Dawnstrike Delayed SIP Capture') {{ $global:MockAuxState='Ready'; Set-TestAuxEnabled $true; $global:TaskEvents += 'enable:aux' }} else {{ $global:MockTaskStates[$TaskName]='Ready'; $global:TaskEvents += ('enable:' + $TaskName) }}
      [pscustomobject]@{{ TaskName=$TaskName }}
    }}
    function New-ScheduledTaskAction {{
      [CmdletBinding()] param([string]$Execute,[string]$Argument,[string]$WorkingDirectory)
      [pscustomobject]@{{ Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory }}
    }}
    function Set-ScheduledTask {{
      [CmdletBinding()] param([string]$TaskName,[string]$TaskPath,[object[]]$Action,[string]$User,[string]$Password)
      if ($TaskName -eq 'Dawnstrike Delayed SIP Capture') {{
        $global:MockAuxArguments = [string]$Action[0].Arguments
        $global:MockAuxExecute = [string]$Action[0].Execute
        $global:MockAuxXml = $global:MockAuxXml -replace '(?s)(<Arguments>).*?(</Arguments>)', ('$1' + $global:MockAuxArguments + '$2')
      }}
      [pscustomobject]@{{ TaskName=$TaskName }}
    }}
    function Register-ScheduledTask {{
      [CmdletBinding()] param([string]$TaskName,[string]$TaskPath,[string]$Xml,[switch]$Force)
      $global:MockAuxXml=$Xml
      $registeredDocument = [System.Xml.XmlDocument]::new()
      $registeredDocument.LoadXml($Xml)
      $global:MockAuxArguments=[string](@($registeredDocument.SelectNodes("//*[local-name()='Arguments']"))[0].InnerText)
      $global:MockAuxExecute=[string](@($registeredDocument.SelectNodes("//*[local-name()='Command']"))[0].InnerText)
      $registeredEnabled = @($registeredDocument.SelectNodes("/*[local-name()='Task']/*[local-name()='Settings']/*[local-name()='Enabled']"))
      if ($registeredEnabled.Count -ne 1) {{ throw 'registered task settings enablement is ambiguous' }}
      $global:MockAuxState=if ([string]$registeredEnabled[0].InnerText -eq 'true') {{ 'Ready' }} else {{ 'Disabled' }}
  [pscustomobject]@{{ TaskName=$TaskName }}
}}
function Get-TestSectionHash {{
  param([string]$Xml, [string]$Name)
  $document = [System.Xml.XmlDocument]::new()
  $document.PreserveWhitespace = $true
  $document.LoadXml($Xml)
  $nodes = @($document.SelectNodes("//*[local-name()='$Name']"))
  if ($nodes.Count -ne 1) {{ throw "test hardening section is ambiguous: $Name" }}
  Get-DawnstrikeSha256Text ([string]$nodes[0].OuterXml)
}}
$hardeningRoot = Join-Path '{state_q}' 'receipts\capture-task'
$hardeningPersistRoot = Join-Path '{state_q}' 'scheduler-backups\capture-hardening-{candidate_sha}'
New-Item -ItemType Directory -Path $hardeningRoot -Force | Out-Null
New-Item -ItemType Directory -Path $hardeningPersistRoot -Force | Out-Null
$hardeningBackupPath = Join-Path $hardeningPersistRoot 'Dawnstrike_Delayed_SIP_Capture.xml'
$hardeningPreparedPath = Join-Path $hardeningRoot 'capture-task-hardening-{candidate_sha}.prepared.json'
$hardeningReceiptPath = Join-Path $hardeningRoot 'capture-task-hardening-{candidate_sha}.json'
[System.IO.File]::WriteAllText($hardeningBackupPath, $preHardeningXml, [System.Text.UTF8Encoding]::new($false))
$beforeXmlHash = Get-DawnstrikeSha256Text $preHardeningXml
$afterXmlHash = Get-DawnstrikeSha256Text $global:MockAuxXml
$beforeActionHash = Get-TestSectionHash $preHardeningXml 'Actions'
$afterActionHash = Get-TestSectionHash $global:MockAuxXml 'Actions'
$beforePrincipalHash = Get-TestSectionHash $preHardeningXml 'Principal'
$afterPrincipalHash = Get-TestSectionHash $global:MockAuxXml 'Principal'
$beforeSettingsHash = Get-TestSectionHash $preHardeningXml 'Settings'
$afterSettingsHash = Get-TestSectionHash $global:MockAuxXml 'Settings'
$triggerHash = Get-TestSectionHash $global:MockAuxXml 'Triggers'
$hardeningOrigin = 'https://github.com/mattfren/DawnStrike.git'
$hardeningTimestamp = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
$hardeningContractPath = '{quote(candidate / "scripts" / "capture_task_hardening_contract.py")}'
$hardeningPython = '{interpreter_declaration["capture_interpreter_path"]}'
$preparedInput = Join-Path $hardeningRoot '.hardening-prepared-test-input.json'
$preparedPayload = [ordered]@{{
  schema_version = 'dawnstrike.capture_task_hardening_prepared.v2'
  status = 'PREPARED'
  task_name = 'Dawnstrike Delayed SIP Capture'
  task_path = '\'
  candidate_sha = '{candidate_sha}'
  candidate_tree = '{candidate_tree}'
  original_state = '{initial_aux_state}'
  backup_path = $hardeningBackupPath
  backup_xml_sha256 = $beforeXmlHash
  backup_xml_file_sha256 = Get-DawnstrikeSha256File $hardeningBackupPath
  xml_before_sha256 = $beforeXmlHash
  xml_after_sha256 = $afterXmlHash
  action_sha256 = $beforeActionHash
  trigger_sha256 = $triggerHash
  principal_sha256 = $beforePrincipalHash
  settings_sha256 = $beforeSettingsHash
  action_before_sha256 = $beforeActionHash
  action_after_sha256 = $afterActionHash
  runtime_head = '{previous_sha}'
  runtime_tree = '{previous_tree}'
  runtime_origin = $hardeningOrigin
  runtime_origin_sha256 = Get-DawnstrikeSha256Text $hardeningOrigin
  lock_token = ('a' * 32)
  lock_bytes_sha256 = Get-DawnstrikeSha256Text 'fixture-governed-lock'
  interpreter_path = $hardeningPython
  interpreter_sha256 = '{interpreter_declaration["capture_interpreter_sha256"]}'
  interpreter_version = '{interpreter_declaration["capture_interpreter_version"]}'
  interpreter_signer_subject = '{interpreter_declaration["capture_interpreter_signer_subject"]}'
  interpreter_signer_thumbprint = '{interpreter_declaration["capture_interpreter_signer_thumbprint"]}'
  runner_before_sha256 = '{runner_sha}'
  runner_target_sha256 = '{runner_sha}'
  old_last_task_result = 2147942402
  old_last_run_time = '2026-08-31T15:20:00.0000000Z'
  intended_receipt_path = $hardeningReceiptPath
  rollback_contract = 'RESTORE_EXACT_XML_AND_ENABLEMENT_HISTORY_NOT_RESTORED'
  research_only = $true
  broker_execution_enabled = $false
  prepared_at_utc = $hardeningTimestamp
  input_stage = 'CANONICAL_REPIN'
}}
Write-DawnstrikeActivationJson $preparedPayload $preparedInput
$preparedOutput = @(& $hardeningPython -I -B $hardeningContractPath 'seal-prepared' '--input' $preparedInput '--output' $hardeningPreparedPath 2>&1)
if ($LASTEXITCODE -ne 0) {{ throw ('Test hardening PREPARED sealing failed: ' + ($preparedOutput -join ' ')) }}
Remove-Item -LiteralPath $preparedInput -Force
$preparedRecord = Get-Content -LiteralPath $hardeningPreparedPath -Raw | ConvertFrom-Json
$changedFields = @()
if ($beforePrincipalHash -ne $afterPrincipalHash) {{ $changedFields += 'principal' }}
if ($beforeSettingsHash -ne $afterSettingsHash) {{ $changedFields += 'settings' }}
if ($beforeActionHash -ne $afterActionHash) {{ $changedFields += 'action' }}
$hardeningPayload = [ordered]@{{
  schema_version = 'dawnstrike.capture_task_hardening_receipt.v2'
  status = 'COMPLETE'
  task_name = 'Dawnstrike Delayed SIP Capture'
  task_path = '\'
  candidate_sha = '{candidate_sha}'
  candidate_tree = '{candidate_tree}'
  original_state = '{initial_aux_state}'
  final_state = 'Disabled'
  backup_name = 'Dawnstrike_Delayed_SIP_Capture.xml'
  backup_relative_path = 'scheduler-backups/capture-hardening-{candidate_sha}/Dawnstrike_Delayed_SIP_Capture.xml'
  prepared_relative_path = 'receipts/capture-task/capture-task-hardening-{candidate_sha}.prepared.json'
  backup_xml_sha256 = $beforeXmlHash
  backup_xml_file_sha256 = Get-DawnstrikeSha256File $hardeningBackupPath
  xml_before_sha256 = $beforeXmlHash
  xml_after_sha256 = $afterXmlHash
  action_sha256 = $beforeActionHash
  trigger_sha256 = $triggerHash
  principal_before_sha256 = $beforePrincipalHash
  principal_after_sha256 = $afterPrincipalHash
  settings_before_sha256 = $beforeSettingsHash
  settings_after_sha256 = $afterSettingsHash
  prepared_record_sha256 = Get-DawnstrikeSha256File $hardeningPreparedPath
  origin_main_refreshed_at_utc = $hardeningTimestamp
  origin_url = $hardeningOrigin
  origin_url_sha256 = Get-DawnstrikeSha256Text $hardeningOrigin
  old_last_task_result = 2147942402
  old_last_run_time = '2026-08-31T15:20:00.0000000Z'
  new_last_task_result = 267011
  new_last_run_time = $null
  history_reset_proven = $true
  changed_fields = $changedFields
  preserved_action = $false
  preserved_trigger = $true
  preserved_input_bindings = $true
  logon_type = 'Password'
  network_capable = $true
  start_when_available = $true
  wake_to_run = $true
  battery_safe = $true
  restart_count = 3
  restart_interval = 'PT15M'
  execution_time_limit = 'PT3H'
  multiple_instances = 'IgnoreNew'
  rollback_contract = 'RESTORE_EXACT_XML_AND_ENABLEMENT_HISTORY_NOT_RESTORED'
  research_only = $true
  broker_execution_enabled = $false
  completed_at_utc = $hardeningTimestamp
  receipt_relative_path = 'receipts/capture-task/capture-task-hardening-{candidate_sha}.json'
  interpreter_path = $hardeningPython
  interpreter_sha256 = '{interpreter_declaration["capture_interpreter_sha256"]}'
  interpreter_version = '{interpreter_declaration["capture_interpreter_version"]}'
  interpreter_signer_subject = '{interpreter_declaration["capture_interpreter_signer_subject"]}'
  interpreter_signer_thumbprint = '{interpreter_declaration["capture_interpreter_signer_thumbprint"]}'
  runner_path = '{runtime_runner_q}'
  runner_before_sha256 = '{runner_sha}'
  runner_sha256 = '{runner_sha}'
  action_bindings = [ordered]@{{
    candidate_sha = '{candidate_sha}'
    bytecode_prefix = '{bytecode_prefix_q}'
    runner_path = '{runtime_runner_q}'
    runner_sha256 = '{runner_sha}'
    symbols_manifest_path = '{symbols_q}'
    symbols_manifest_sha256 = '{symbols_sha}'
    entitlement_receipt_path = '{entitlement_q}'
    entitlement_receipt_sha256 = '{entitlement_sha}'
    source_config_path = '{source_config_q}'
    source_config_sha256 = '{source_config_sha}'
  }}
  previous_candidate_sha = '{previous_sha}'
  action_before_sha256 = $beforeActionHash
  action_after_sha256 = $afterActionHash
  action_migrated = $true
  history_evidence_preserved = $true
  history_disposition = 'RESET_AS_UPDATE_SIDE_EFFECT'
  input_stage = 'CANONICAL_REPIN'
}}
$hardeningInput = Join-Path $hardeningRoot '.hardening-complete-test-input.json'
Write-DawnstrikeActivationJson $hardeningPayload $hardeningInput
$hardeningOutput = @(& $hardeningPython -I -B $hardeningContractPath 'seal-hardening' '--input' $hardeningInput '--output' $hardeningReceiptPath 2>&1)
if ($LASTEXITCODE -ne 0) {{ throw ('Test hardening COMPLETE sealing failed: ' + ($hardeningOutput -join ' ')) }}
Remove-Item -LiteralPath $hardeningInput -Force
$hardeningReceipt = Get-Content -LiteralPath $hardeningReceiptPath -Raw | ConvertFrom-Json
& '{prep_q}' -CandidateRoot '{candidate_q}' -RuntimeRoot '{runtime_q}' -StateRoot '{state_q}' -BackupRoot '{backup_q}' -CandidateSha '{candidate_sha}' -ProcessTimeoutSeconds 120 | Out-Null
$activated = Invoke-DawnstrikeRuntimeActivation -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' -CiEvidencePath '{ci_q}' -SolEvidencePath '{sol_q}' -CandidateRoot '{candidate_q}' -RuntimeRoot '{runtime_q}' -StateRoot '{state_q}' -BackupRoot '{backup_q}' -BackupRetention 5 -ProcessTimeoutSeconds 120 -RunAsCredential $global:TestCredential
$activationAuxState = $global:MockAuxState
                $rebindScript = '{quote(candidate / "scripts" / "rebind_intraday_capture_task.ps1")}'
            $rebindFailureCaught = $false
            $rebindFailureMessage = ''
            try {{ & $rebindScript -RuntimeRoot '{runtime_q}' -StateRoot '{state_q}' -CandidateSha '{candidate_sha}' -SymbolsManifest '{symbols_q}' -SymbolsManifestSha256 '{symbols_sha}' -EntitlementReceipt '{entitlement_q}' -EntitlementReceiptSha256 '{entitlement_sha}' -SourceConfig '{source_config_q}' -SourceConfigSha256 '{source_config_sha}' -RunAsCredential $global:TestCredential -Enable -InjectFailureAfterMutation -ProcessTimeoutSeconds 120 | Out-Null }} catch {{ $rebindFailureCaught = $true; $rebindFailureMessage = $_.Exception.Message }}
            $rebindFailurePath = Join-Path '{state_q}' ('receipts\capture-task\capture-task-rebind-' + '{candidate_sha}' + '.failed.json')
            if (-not (Test-Path -LiteralPath $rebindFailurePath -PathType Leaf)) {{ throw ('Injected rebind did not seal failure evidence: ' + $rebindFailureMessage) }}
        $rebindFailure = Get-Content -LiteralPath $rebindFailurePath -Raw | ConvertFrom-Json
        $rebound = & $rebindScript -RuntimeRoot '{runtime_q}' -StateRoot '{state_q}' -CandidateSha '{candidate_sha}' -SymbolsManifest '{symbols_q}' -SymbolsManifestSha256 '{symbols_sha}' -EntitlementReceipt '{entitlement_q}' -EntitlementReceiptSha256 '{entitlement_sha}' -SourceConfig '{source_config_q}' -SourceConfigSha256 '{source_config_sha}' -RunAsCredential $global:TestCredential -Enable -ProcessTimeoutSeconds 120
    $rebindReceiptPath = Join-Path '{state_q}' ('receipts\capture-task\capture-task-rebind-' + '{candidate_sha}' + '.json')
    $rebindReceipt = Get-Content -LiteralPath $rebindReceiptPath -Raw | ConvertFrom-Json
    $rebindActionArguments = $global:MockAuxArguments
    . '{rollback_q}'
$receiptPath = Join-Path '{state_q}' ('receipts\runtime-activation\runtime-activation-' + $activated.activation_id + '.json')
$rolledBack = Invoke-DawnstrikeRuntimeRollback -ActivationReceipt $receiptPath -ContractRoot '{candidate_q}' -RuntimeRoot '{runtime_q}' -StateRoot '{state_q}' -BackupRoot '{backup_q}' -ProcessTimeoutSeconds 120 -RunAsCredential $global:TestCredential
    $output = [pscustomobject]@{{ hardening=$hardeningReceipt; prepared=$preparedRecord; activated=$activated; rebound=$rebindReceipt; rebind_failure=$rebindFailure; rebind_failure_caught=$rebindFailureCaught; rebind_action_arguments=$rebindActionArguments; rolled_back=$rolledBack; activation_aux_state=$activationAuxState; final_aux_state=$global:MockAuxState; task_events=$global:TaskEvents }}
    $output | ConvertTo-Json -Depth 12 -Compress
"""
    environment = os.environ.copy()
    windows_modules = (
        Path(environment.get("WINDIR", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "Modules"
    )
    environment["PSModulePath"] = os.pathsep.join(
        [str(windows_modules), environment.get("PSModulePath", "")]
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=source,
        env=environment,
        text=True,
        capture_output=True,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["prepared"]["schema_version"] == "dawnstrike.capture_task_hardening_prepared.v2"
    assert payload["prepared"]["status"] == "PREPARED"
    assert payload["prepared"]["candidate_sha"] == candidate_sha
    assert payload["hardening"]["schema_version"] == "dawnstrike.capture_task_hardening_receipt.v2"
    assert payload["hardening"]["status"] == "COMPLETE"
    assert payload["hardening"]["candidate_sha"] == candidate_sha
    assert payload["hardening"]["candidate_tree"] == candidate_tree
    assert payload["hardening"]["original_state"] == initial_aux_state
    assert payload["hardening"]["final_state"] == "Disabled"
    assert payload["hardening"]["logon_type"] == "Password"
    assert payload["hardening"]["action_bindings"]["runner_path"] == str(
        runtime / "scripts" / "run_daily_intraday_capture.py"
    )
    assert payload["hardening"]["action_bindings"]["bytecode_prefix"] == str(
        state / "capture-bytecode" / candidate_sha
    )
    assert (
        f'"-I" "-B" "-X" "pycache_prefix={state / "capture-bytecode" / candidate_sha}" '
        f'"-u" "{runtime / "scripts" / "run_daily_intraday_capture.py"}"'
        in payload["rebind_action_arguments"]
    )
    assert payload["activated"]["status"] == "COMPLETE"
    assert payload["activated"]["state_preparation_required"] is True
    assert payload["activated"]["auxiliary_capture_present"] is True
    assert payload["activated"]["auxiliary_capture_state_after"] == "Disabled"
    assert payload["activated"]["auxiliary_capture_action"] == "DISABLED_UNTIL_EXACT_SHA_REBIND"
    assert payload["activation_aux_state"] == "Disabled"
    assert payload["rebind_failure_caught"] is True
    assert payload["rebind_failure"]["status"] == "FAILED_RESTORED_EXACT_DISABLED"
    assert payload["rebound"]["status"] == "COMPLETE"
    assert payload["rebound"]["changed_field"] == "candidate_sha_and_input_bindings"
    assert payload["rebound"]["previous_candidate_sha"] == candidate_sha
    for binding in (
        '"--candidate-sha" "' + candidate_sha + '"',
        '"--symbols-manifest" "' + str(symbols_manifest) + '"',
        '"--symbols-manifest-sha256" "' + symbols_sha + '"',
        '"--entitlement-receipt" "' + str(entitlement_receipt) + '"',
        '"--entitlement-receipt-sha256" "' + entitlement_sha + '"',
        '"--source-config" "' + str(source_config) + '"',
        '"--source-config-sha256" "' + source_config_sha + '"',
    ):
        assert binding in payload["rebind_action_arguments"]
    assert payload["rolled_back"]["status"] == "ROLLED_BACK"
    assert payload["rolled_back"]["auxiliary_capture_action"] == "RESTORED_EXACT"
    for field in (
        "capture_hardening_receipt_relative_path",
        "capture_hardening_receipt_raw_sha256",
        "capture_hardening_receipt_sha256",
        "capture_hardening_xml_sha256",
        "capture_hardening_action_sha256",
        "capture_hardening_principal_sha256",
        "capture_hardening_trigger_sha256",
        "capture_hardening_settings_sha256",
        "capture_hardening_runner_before_sha256",
        "capture_hardening_runner_target_sha256",
    ):
        assert payload["rolled_back"][field] == payload["activated"][field]
    assert payload["final_aux_state"] == "Disabled"
    assert (
        len(
            list(
                (state / "scheduler-backups" / payload["activated"]["scheduler_backup_name"]).glob(
                    "*.xml"
                )
            )
        )
        == 6
    )
