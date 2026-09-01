"""Hostile, network-free checks for the Vercel publication journal contract."""

import hashlib
import json
import multiprocessing
import os
import subprocess
from pathlib import Path

import pytest

from scripts import vercel_publication_journal as journal


def _competing_stale_adopter(
    lock_path: str, state_root: str, owner: str, start: object, release: object, results: object
) -> None:
    start.wait(10)
    try:
        journal.acquire_lock(
            Path(lock_path), state_root=Path(state_root), owner_id=owner,
            candidate_source_sha="a" * 40, candidate_source_tree="b" * 40,
            candidate_market_date="2026-08-31", journal_path="outputs/journal.json",
        )
        results.put((owner, "acquired"))
        release.wait(10)
        journal.release_lock(Path(lock_path), state_root=Path(state_root), owner_id=owner)
    except Exception as exc:
        results.put((owner, type(exc).__name__))


def _pre_payload() -> dict:
    aliases = [
        "https://dawnstrike-command-center-x3-mattfren-mattfrens-projects.vercel.app",
        "https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app",
    ]
    return {
        "schema_version": journal.SCHEMA,
        "operation": "vercel_publication",
        "phase": "PRE_MUTATION",
        "sequence": 0,
        "project_id": "prj_test",
        "project_name": "dawnstrike-command-center-x3",
        "provider_scope": "mattfrens-projects",
        "production_aliases": aliases,
        "candidate_preview_url": "https://preview.example.vercel.app",
        "candidate_preview_deployment_id": "dpl_preview",
        "candidate_source_sha": "a" * 40,
        "candidate_source_tree": "b" * 40,
        "toolchain_identity_sha256": "9" * 64,
        "candidate_market_date": "2026-08-31",
        "candidate_build_id": "c" * 20,
        "candidate_build_sha": "c" * 64,
        "candidate_build_manifest_sha256": "8" * 64,
        "candidate_release_manifest_sha256": "a" * 64,
        "candidate_manifest_sha256": "d" * 64,
        "candidate_package_manifest_sha256": "e" * 64,
        "prior_aliases": [
            {
                "alias": alias,
                "deployment_id": f"prior-{i}",
                "deployment_url": f"https://prior-{i}.vercel.app",
                "health_status": "alive",
                "readiness_status": "ready",
                "readiness_http_status": 200,
                "source_sha": "1" * 40,
                "source_tree": "2" * 40,
                "source_manifest_sha256": "3" * 64,
                "build_manifest_sha256": "4" * 64,
                "release_manifest_sha256": "5" * 64,
                "artifact_proof": {
                    "endpoint": alias,
                    "build_sha": "6" * 64,
                    "asset_count": 2,
                    "total_bytes": 100,
                    "file_hashes_sha256": "7" * 64,
                },
            }
            for i, alias in enumerate(aliases)
        ],
        "promoted_deployment_id": None,
        "promoted_deployment_url": None,
        "production_result_sha256": journal.EMPTY_SHA256,
        "result_relative_path": "build/daily-deployment-result.json",
        "result_payload": None,
        "prior_journal_file_sha256": journal.EMPTY_SHA256,
        "compensation_relative_path": "NONE",
        "compensation_sha256": journal.EMPTY_SHA256,
        "recorded_at_utc": "2026-08-31T12:00:00.000000Z",
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _seal(path: Path, payload: dict) -> bytes:
    raw = journal.canonical_json(payload)
    path.write_bytes(raw)
    value = dict(payload)
    value["journal_self_sha256"] = hashlib.sha256(raw).hexdigest()
    return journal.canonical_json(value)


def test_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    path = root / "journal.json"
    raw = b'{"a":1,"a":2}'
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="duplicate"):
        journal.validate(raw)


def test_exact_keys_self_hash_and_tamper_are_enforced(tmp_path: Path) -> None:
    payload = _pre_payload()
    path = tmp_path / "pre.json"
    path.write_bytes(_seal(path, payload))
    validated = journal.validate(path.read_bytes())
    assert validated["phase"] == "PRE_MUTATION"
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["candidate_source_sha"] = "f" * 40
    with pytest.raises(ValueError, match="self hash"):
        journal.validate(journal.canonical_json(tampered))
    tampered.pop("operation")
    tampered["journal_self_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="keys are not exact"):
        journal.validate(journal.canonical_json(tampered))


def test_transitions_are_adjacent_and_atomic(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    pre = root / "journal.json"
    authorization = "f" * 64
    pre_payload = _pre_payload()
    pre_payload.update(
        {
            "expected_market_date": "2026-08-31",
            "prepublication_authorization_id": authorization,
            "daily_ledger_authorization_id": authorization,
        }
    )
    pre.write_bytes(_seal(pre, pre_payload))
    prior_hash = hashlib.sha256(pre.read_bytes()).hexdigest()
    result = {
        "status": "PRODUCTION_VERIFIED",
        "source_sha": "a" * 40,
        "expected_market_date": "2026-08-31",
        "prepublication_authorization_id": authorization,
        "daily_ledger_authorization_id": authorization,
    }
    post = dict(pre_payload)
    post.update(
        {
            "phase": "POST_ALIASES",
            "sequence": 1,
            "promoted_deployment_id": "dpl_promoted",
            "promoted_deployment_url": "https://promoted.example.vercel.app",
            "production_result_sha256": hashlib.sha256(journal.canonical_json(result)).hexdigest(),
            "result_payload": result,
            "prior_journal_file_sha256": prior_hash,
        }
    )
    source = root / "post-input.json"
    source.write_bytes(journal.canonical_json(post))
    sealed = journal.transition(source, pre, pre)
    assert sealed["payload"]["phase"] == "POST_ALIASES"
    assert journal.validate(pre.read_bytes())["sequence"] == 1
    bad = dict(post)
    bad["phase"] = "COMPLETE"
    bad["sequence"] = 9
    bad["prior_journal_file_sha256"] = hashlib.sha256(pre.read_bytes()).hexdigest()
    bad_source = root / "bad-input.json"
    bad_source.write_bytes(journal.canonical_json(bad))
    with pytest.raises(ValueError, match="adjacent"):
        journal.transition(bad_source, pre, pre)


def test_post_aliases_requires_current_publication_authorization(tmp_path: Path) -> None:
    payload = _pre_payload()
    result = {"status": "PRODUCTION_VERIFIED", "source_sha": "a" * 40}
    payload.update(
        {
            "phase": "POST_ALIASES",
            "sequence": 1,
            "promoted_deployment_id": "dpl_promoted",
            "promoted_deployment_url": "https://promoted.example.vercel.app",
            "production_result_sha256": hashlib.sha256(
                journal.canonical_json(result)
            ).hexdigest(),
            "result_payload": result,
        }
    )
    path = tmp_path / "post.json"
    path.write_bytes(_seal(path, payload))
    with pytest.raises(ValueError, match="prepublication_authorization_id"):
        journal.validate(path.read_bytes())


def _authorization_fields() -> dict[str, str]:
    authorization = "f" * 64
    return {
        "expected_market_date": "2026-08-31",
        "prepublication_authorization_id": authorization,
        "daily_ledger_authorization_id": authorization,
    }


def _complete_payload() -> dict:
    payload = _pre_payload()
    payload.update(_authorization_fields())
    payload.update(
        {
            "phase": "COMPLETE",
            "sequence": 2,
            "promoted_deployment_id": "dpl_promoted",
            "promoted_deployment_url": "https://promoted.example.vercel.app",
        }
    )
    payload["result_payload"] = {
        "schema_version": "dawnstrike.daily_deployment.v1",
        "preview_url": payload["candidate_preview_url"],
        "preview_deployment_id": payload["candidate_preview_deployment_id"],
        "source_sha": payload["candidate_source_sha"],
        "source_tree": payload["candidate_source_tree"],
        "market_date": payload["candidate_market_date"],
        "build_id": payload["candidate_build_id"],
        "build_sha": payload["candidate_build_sha"],
        "project_id": payload["project_id"],
        "provider_scope": payload["provider_scope"],
        "promoted_deployment_id": payload["promoted_deployment_id"],
        "production_deployment_id": payload["promoted_deployment_id"],
        "vercel_source_manifest_sha256": payload["candidate_manifest_sha256"],
        "vercel_package_manifest_sha256": payload["candidate_package_manifest_sha256"],
        "authorized_build_manifest_sha256": payload[
            "candidate_build_manifest_sha256"
        ],
        "authorized_release_manifest_sha256": payload[
            "candidate_release_manifest_sha256"
        ],
        "toolchain_identity_sha256": payload["toolchain_identity_sha256"],
        "build_manifest_sha256": "8" * 64,
        "allow_degraded": False,
        "promoted": True,
        "live_trading_enabled": False,
        "research_only": True,
        "status": "PRODUCTION_VERIFIED",
        "preview_artifact_proof": {
            "endpoint": payload["candidate_preview_url"],
            "build_sha": payload["candidate_build_sha"],
            "asset_count": 2,
            "total_bytes": 100,
            "file_hashes_sha256": "1" * 64,
        },
        "production_artifact_proofs": [
            {
                "endpoint": alias,
                "build_sha": payload["candidate_build_sha"],
                "asset_count": 2,
                "total_bytes": 100,
                "file_hashes_sha256": "1" * 64,
            }
            for alias in payload["production_aliases"]
        ],
        **_authorization_fields(),
    }
    payload["production_result_sha256"] = hashlib.sha256(
        journal.canonical_json(payload["result_payload"])
    ).hexdigest()
    return payload


def test_reordered_terminal_json_is_rejected_even_with_valid_self_hash(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    result_path = root / "build" / "daily-deployment-result.json"
    result_path.parent.mkdir()
    payload = _complete_payload()
    result_path.write_bytes(journal.canonical_json(payload["result_payload"]))
    payload["journal_self_sha256"] = hashlib.sha256(journal.canonical_json(payload)).hexdigest()
    reordered = json.dumps(
        dict(reversed(payload.items())), ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    with pytest.raises(ValueError, match="canonical JSON"):
        journal.validate(reordered, state_root=root, journal_path=root / "journal.json")


def test_complete_terminal_and_result_are_bound_byte_for_byte(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    result_path = root / "build" / "daily-deployment-result.json"
    result_path.parent.mkdir()
    payload = _complete_payload()
    result_raw = journal.canonical_json(payload["result_payload"])
    result_path.write_bytes(result_raw)
    payload["journal_self_sha256"] = hashlib.sha256(journal.canonical_json(payload)).hexdigest()
    journal_path = root / "journal.json"
    journal_path.write_bytes(journal.canonical_json(payload))
    assert journal.validate(journal_path.read_bytes(), state_root=root, journal_path=journal_path)

    tampered_result = dict(payload["result_payload"])
    tampered_result["build_id"] = "tampered"
    result_path.write_bytes(journal.canonical_json(tampered_result))
    with pytest.raises(ValueError, match="production result raw hash"):
        journal.validate(journal_path.read_bytes(), state_root=root, journal_path=journal_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "incomplete"),
        ("extra", "incomplete"),
        ("mismatch", "proof tuples diverge"),
        ("count_mismatch", "proof tuples diverge"),
        ("oversize", "byte count"),
    ],
)
def test_complete_rejects_hostile_governed_artifact_proofs(
    tmp_path: Path, mutation: str, message: str
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    payload = _complete_payload()
    proofs = payload["result_payload"]["production_artifact_proofs"]
    if mutation == "missing":
        proofs.pop()
    elif mutation == "extra":
        proofs.append(dict(proofs[-1], endpoint="https://extra.example"))
    elif mutation == "mismatch":
        proofs[-1]["file_hashes_sha256"] = "2" * 64
    elif mutation == "count_mismatch":
        proofs[-1]["asset_count"] = 3
    else:
        proofs[-1]["total_bytes"] = 134_217_729
    payload["production_result_sha256"] = hashlib.sha256(
        journal.canonical_json(payload["result_payload"])
    ).hexdigest()
    result_path = root / payload["result_relative_path"]
    result_path.parent.mkdir(parents=True)
    result_path.write_bytes(journal.canonical_json(payload["result_payload"]))
    payload["journal_self_sha256"] = hashlib.sha256(journal.canonical_json(payload)).hexdigest()
    with pytest.raises(ValueError, match=message):
        journal.validate(
            journal.canonical_json(payload), state_root=root, journal_path=root / "journal.json"
        )


def test_publication_lock_rejects_live_owner_and_adopts_dead_owner(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    lock = root / "outputs" / "publication.lock"
    kwargs = {
        "state_root": root,
        "candidate_source_sha": "a" * 40,
        "candidate_source_tree": "b" * 40,
        "candidate_market_date": "2026-08-31",
        "journal_path": "outputs/journal.json",
    }
    journal.acquire_lock(lock, owner_id="owner-a", pid=os.getpid(), **kwargs)
    with pytest.raises(ValueError, match="live owner"):
        journal.acquire_lock(lock, owner_id="owner-b", pid=os.getpid(), **kwargs)
    journal.release_lock(lock, state_root=root, owner_id="owner-a", pid=os.getpid())

    journal.acquire_lock(lock, owner_id="dead-owner", pid=2_000_000, **kwargs)
    adopted = journal.acquire_lock(lock, owner_id="owner-c", pid=os.getpid(), **kwargs)
    assert adopted["owner_id"] == "owner-c"
    journal.release_lock(lock, state_root=root, owner_id="owner-c", pid=os.getpid())


def test_publication_lock_release_never_unlinks_a_replacement_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    lock = root / "outputs" / "publication.lock"
    replacement_source = root / "outputs" / "replacement.lock"
    kwargs = {
        "state_root": root,
        "candidate_source_sha": "a" * 40,
        "candidate_source_tree": "b" * 40,
        "candidate_market_date": "2026-08-31",
        "journal_path": "outputs/journal.json",
    }
    journal.acquire_lock(lock, owner_id="owner-a", pid=os.getpid(), **kwargs)
    journal.acquire_lock(
        replacement_source, owner_id="owner-b", pid=os.getpid(), **kwargs
    )
    replacement_raw = replacement_source.read_bytes()
    journal.release_lock(
        replacement_source, state_root=root, owner_id="owner-b", pid=os.getpid()
    )

    real_replace = journal.os.replace

    def replace_then_reacquire(source: str | os.PathLike, target: str | os.PathLike) -> None:
        real_replace(source, target)
        Path(source).write_bytes(replacement_raw)

    with monkeypatch.context() as patch:
        patch.setattr(journal.os, "replace", replace_then_reacquire)
        journal.release_lock(lock, state_root=root, owner_id="owner-a", pid=os.getpid())

    remaining, _ = journal._read_lock(lock, root)
    assert remaining["owner_id"] == "owner-b"
    journal.release_lock(lock, state_root=root, owner_id="owner-b", pid=os.getpid())


def test_publication_lock_os_gate_allows_only_one_of_two_stale_adopters(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    lock = root / "publication.lock"
    journal.acquire_lock(
        lock, state_root=root, owner_id="stale", pid=999_999_991,
        candidate_source_sha="a" * 40, candidate_source_tree="b" * 40,
        candidate_market_date="2026-08-31", journal_path="outputs/journal.json",
    )
    context = multiprocessing.get_context("spawn")
    start, release, results = context.Event(), context.Event(), context.Queue()
    workers = [
        context.Process(
            target=_competing_stale_adopter,
            args=(str(lock), str(root), owner, start, release, results),
        )
        for owner in ("adopter-a", "adopter-b")
    ]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = [results.get(timeout=15)[1], results.get(timeout=15)[1]]
    release.set()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert outcomes.count("acquired") == 1
    assert outcomes.count("ValueError") == 1


def test_containment_rejects_an_internal_reparse_component(tmp_path: Path) -> None:
    root = tmp_path / "state"
    target = root / "real"
    root.mkdir()
    target.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this Windows host")
    with pytest.raises(ValueError, match="reparse component"):
        journal._contained(link / "journal.json", root)


def test_publisher_binds_every_recovery_phase_to_the_current_invocation() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("$existingJournal = Get-VercelPublicationJournal", 1)[1].split(
        "function Get-VercelGovernedAssetProof", 1
    )[0]
    assert function.index("Existing Vercel recovery journal") < function.index("$recoveryAliases")
    for binding in (
        "candidate_market_date",
        "project_id",
        "project_name",
        "provider_scope",
        "production_aliases",
    ):
        assert binding in function


def test_publisher_crash_and_failure_seams_are_environment_guarded() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    guard = '$env:DAWNSTRIKE_TEST_VERCEL_PUBLICATION -ne "1"'
    assert guard in script
    assert script.index(guard) < script.index("function Test-VercelPromotionSeam")
    assert "Vercel publication failure and crash injection are test-only." in script


def test_publisher_uses_dated_state_history_and_byte_exact_runtime_result_copy() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    assert '$journalRoot = Join-Path $journalHistoryRoot $journalMarketKey' in script
    assert 'vercel-publication/$journalMarketKey/daily-deployment-result.json' in script
    assert "Assert-VercelPriorJournalHistoryTerminal" in script
    assert "prior_date_interrupted_rollover" in script
    assert "Prior-date terminal compensation verification" in script
    prior_recovery = script.split("foreach ($entry in $priorNonterminal)", 1)[1].split(
        "function Assert-VercelJournalMatchesInvocation", 1
    )[0]
    assert "$savedResultPath = $script:resultPath" in prior_recovery
    assert "$script:resultPath = Join-Path $resolvedStateRoot" in prior_recovery
    assert "$script:resultPath = $savedResultPath" in prior_recovery
    writer = script.split("function Write-VercelResultAtomic", 1)[1].split(
        "function Assert-LowerHex64", 1
    )[0]
    assert "WriteAllBytes($temporary, $bytes)" in writer
    assert "WriteAllBytes($runtimeTemporary, $bytes)" in writer
    assert "Durable StateRoot and runtime Vercel result copies diverge" in writer


def test_scheduled_finalizer_runs_recovery_only_before_current_day_gates() -> None:
    publisher = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    finalizer = (Path(__file__).parents[1] / "scripts" / "run_daily_finalize.ps1").read_text(
        encoding="utf-8"
    )
    assert "[switch]$RecoveryOnly" in publisher
    assert "Recovery-only Vercel convergence reached a fresh publication path" in publisher
    assert "Archive-VercelCompensatedCurrentJournal" in publisher
    recovery = finalizer.index("-RecoveryOnly")
    release = finalizer.index("Resolve-DawnstrikeReleaseSha")
    boundary = finalizer.index("Resolve-DawnstrikeFinalizeMarketBoundary `", recovery)
    database = finalizer.index('if (-not (Test-Path -LiteralPath $dbPath -PathType Leaf))')
    assert release < recovery < boundary < database


def test_recovery_bootstraps_exact_clean_origin_before_loading_helpers() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    bootstrap = script.index("$bootstrapSource = Assert-VercelRecoveryBootstrapSource")
    helper = script.index('. (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")')
    assert bootstrap < helper
    assert "Vercel publisher must execute from the exact ProjectRoot being admitted." in script
    assert script.index("$executingRoot =") < bootstrap
    function = script.split("function Assert-VercelRecoveryBootstrapSource", 1)[1].split(
        "$resolvedRoot =", 1
    )[0]
    for required in (
        "refs/remotes/origin/main",
        "--untracked-files=all",
        "--ignored",
        "Get-AuthenticodeSignature",
        "core.fsmonitor=false",
        "core.hooksPath=NUL",
        "remote.origin.url",
        "$headAfter -cne $headBefore",
    ):
        assert required in function
    provider = script.split("function Invoke-VercelProcess", 1)[1].split(
        "function Assert-RemoteVercelSourceManifest", 1
    )[0]
    assert provider.index("Assert-VercelPublicationSourceStable") < provider.index(
        "Invoke-DawnstrikeJobProcess"
    )


def test_terminal_complete_is_revalidated_before_failure_compensation() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    catch = script.split("catch {\n    $publicationError = $_.Exception.Message", 1)[1]
    reread = catch.index("$caughtJournal = Get-VercelPublicationJournal")
    complete = catch.index("Resolve-VercelCompletePublicationJournal")
    rollback = catch.index('Arguments @("rollback", [string]$priorPrimary.id, "--yes")')
    assert reread < complete < rollback
    assert "no provider compensation was attempted" in catch[:rollback]


def test_prior_terminal_history_allows_governed_toolchain_upgrade() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Assert-VercelPriorJournalHistoryTerminal", 1)[1].split(
        "function Assert-VercelJournalMatchesInvocation", 1
    )[0]
    equality = function.index("A nonterminal prior-date Vercel journal requires")
    nonterminal_loop = function.index("foreach ($entry in $priorNonterminal)")
    assert equality > nonterminal_loop
    before_classification = function[: function.index("$history +=")]
    assert "toolchain_identity_sha256 -cne $toolchainIdentitySha256" not in before_classification


def test_prior_alias_snapshot_uses_immutable_deployment_and_cas() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    snapshot = script.split("if ($Promote) {", 1)[1].split("try {\n    if ($Promote)", 1)[0]
    assert "$snapshotBaseUrl = Get-VercelImmutableDeploymentBaseUrl" in snapshot
    assert "-AliasUrl $snapshotBaseUrl" in snapshot
    assert '"$snapshotBaseUrl/build-manifest.json' in snapshot
    assert '"$snapshotBaseUrl/release-manifest.json' in snapshot
    assert "Get-VercelGovernedAssetProof -BaseUrl $snapshotBaseUrl" in snapshot
    assert snapshot.count("Assert-VercelPriorAliasSnapshotsCurrent") >= 1
    promotion = script.split("try {\n    if ($Promote)", 1)[1]
    assert promotion.index("Assert-VercelPriorAliasSnapshotsCurrent") < promotion.index(
        "$promoted = $true"
    )


def test_compensation_never_overwrites_foreign_alias_state() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    compensation = script.split("function Invoke-VercelPublicationCompensation", 1)[1].split(
        "function Get-VercelJournalPreviewEvidence", 1
    )[0]
    assert compensation.index("Get-VercelCompensationPlan") < compensation.index(
        "Set-VercelAlias"
    )
    assert compensation.index("foreign_count -gt 0") < compensation.index(
        "Set-VercelAlias"
    )
    assert "Alias changed to a foreign deployment before compensation." in compensation
    catch = script.split("catch {\n    $publicationError = $_.Exception.Message", 1)[1]
    assert catch.index("Get-VercelCompensationPlan") < catch.index(
        'Arguments @("rollback", [string]$priorPrimary.id, "--yes")'
    )
    assert "no provider rollback was attempted" in catch


def test_every_terminal_success_rechecks_alias_and_promotion_identity() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    complete = script.split("function Complete-VercelJournalRecovery", 1)[1].split(
        "function Resolve-VercelCompletePublicationJournal", 1
    )[0]
    assert complete.index("New-VercelRecoveredResultPayload") < complete.index(
        "Test-VercelAliasSetMatches"
    ) < complete.index('$complete.phase = "COMPLETE"')
    normal = script.split("$preResultJournal = Get-VercelPublicationJournal", 1)[1]
    assert normal.index("Test-VercelPromotedCandidateSetMatchesJournal") < normal.index(
        "$result = [ordered]@{"
    )
    assert normal.index("$preCompleteJournal = Get-VercelPublicationJournal") < normal.index(
        '-Phase "COMPLETE"'
    )


def test_recovery_retry_reauthorizes_current_session_before_mutation() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    recovery_guard = script.index(
        'if ($RecoveryOnly) {\n'
        '    throw "Recovery-only Vercel convergence reached a fresh publication path."'
    )
    authorization = script.index("Assert-GovernedPublicationAuthorization", recovery_guard)
    fresh_build_guard = script.index("if (-not $recoveryRetry)", recovery_guard)
    assert recovery_guard < authorization < fresh_build_guard
    promotion = script.index('$promoted = $true')
    assert script.rfind("Assert-GovernedPublicationAuthorization", 0, promotion) > fresh_build_guard


def test_result_atomic_publish_executes_under_windows_powershell_51(tmp_path: Path) -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Publish-VercelFileAtomic", 1)[1].split(
        "function Write-VercelResultAtomic", 1
    )[0]
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"first")
    destination.write_bytes(b"old")
    source2 = tmp_path / "source2.bin"
    destination2 = tmp_path / "destination2.bin"
    source2.write_bytes(b"second")
    def quote(path: Path) -> str:
        return str(path).replace("'", "''")

    command = (
        "function Publish-VercelFileAtomic" + function + ";"
        f"Publish-VercelFileAtomic -TemporaryPath '{quote(source)}' "
        f"-DestinationPath '{quote(destination)}';"
        f"Publish-VercelFileAtomic -TemporaryPath '{quote(source2)}' "
        f"-DestinationPath '{quote(destination2)}'"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert destination.read_bytes() == b"first"
    assert destination2.read_bytes() == b"second"


def test_publisher_governed_asset_proof_is_bounded_and_covers_every_alias() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    proof = script.split("function Get-VercelGovernedAssetProof", 1)[1].split(
        "function Assert-PublicationState", 1
    )[0]
    for marker in (
        "$properties.Count -gt 256",
        "$length -gt 16777216",
        "$totalBytes -gt 134217728",
        "$relative.Contains('\\')",
        "$_ -in @('', '.', '..')",
        "governed asset hash mismatch",
        '"--max-filesize", "16777216"',
    ):
        assert marker in proof
    assert "preview_artifact_proof = $previewArtifactProof" in script
    assert "production_artifact_proofs = @($productionArtifactProofs)" in script
    alias_loop = script.split("$productionArtifactProofs += Get-VercelGovernedAssetProof", 1)[0]
    assert "foreach ($alias in $allProductionAliases)" in alias_loop
    assert "$vercel + $vercelAuth + $Arguments" in script
    assert "$vercel + $Arguments + $vercelAuth" not in script
    assert script.index("$vercel + $vercelAuth + $Arguments") < script.index(
        '"--", "--silent", "--show-error"'
    )


def test_vercel_token_precedes_native_curl_separator_under_powershell_51() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Invoke-VercelProcess", 1)[1].split(
        "function Assert-RemoteVercelSourceManifest", 1
    )[0]
    command = (
        "$vercelEntryPath='vc.js';$vercel=@('--scope','scope');"
        "$vercelAuth=@('--token','sentinel');"
        "$nodePath='C:\\Program Files\\nodejs\\node.exe';"
        "$expectedCurlPath='C:\\Windows\\System32\\curl.exe';"
        "$gitPath='C:\\Program Files\\Git\\cmd\\git.exe';"
        "$uvPath='C:\\Users\\MattFields\\AppData\\Local\\Programs\\Python\\Python313\\Scripts\\uv.exe';"
        "$approvedPython=[pscustomobject]@{path='C:\\Users\\MattFields\\AppData\\Local\\Programs\\Python\\Python313\\python.exe'};"
        "function Assert-VercelPublicationToolchainStable {};"
        "function Assert-DawnstrikeSharedLockNoReparse { param($Path,$Label) };"
        "function Invoke-DawnstrikeJobProcess { param($FilePath,$ArgumentList,$WorkingDirectory,"
        "$Label,$TimeoutSeconds,$OutputDrainTimeoutSeconds,$EnvironmentOverrides);"
        "$script:captured=@($ArgumentList);[pscustomobject]@{ExitCode=0;Stdout='';Stderr=''} };"
        "function Invoke-VercelProcess" + function + ";"
        "$null=Invoke-VercelProcess -Arguments @('curl','/asset','--','--output','x') "
        "-Label test -TimeoutSeconds 1;$captured -join [char]31"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    argv = completed.stdout.strip().split(chr(31))
    assert argv.index("--token") < argv.index("curl") < argv.index("--")
    assert argv[argv.index("--token") + 1] == "sentinel"


def test_state_relative_path_executes_under_windows_powershell_51(tmp_path: Path) -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Get-VercelStateRelativePath", 1)[1].split(
        "function ConvertTo-VercelCanonicalObject", 1
    )[0]
    root = tmp_path / "state"
    target = root / "outputs" / "2026-09-01" / "journal.json"
    target.parent.mkdir(parents=True)

    def quote(path: Path) -> str:
        return str(path).replace("'", "''")

    command = (
        "function Get-VercelStateRelativePath" + function + ";"
        f"Get-VercelStateRelativePath -StateRootPath '{quote(root)}' "
        f"-TargetPath '{quote(target)}'"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "outputs/2026-09-01/journal.json"
    assert "[System.IO.Path]::GetRelativePath" not in script


def test_publication_write_guard_rejects_windows_reparse_component(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    linked = root / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows directory reparse creation is unavailable")
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Assert-VercelContainedNonReparsePath", 1)[1].split(
        "function ConvertTo-VercelCanonicalObject", 1
    )[0]

    def quote(path: Path) -> str:
        return str(path).replace("'", "''")

    command = (
        "function Assert-VercelContainedNonReparsePath" + function + ";"
        f"Assert-VercelContainedNonReparsePath -RootPath '{quote(root)}' "
        f"-TargetPath '{quote(linked / 'escape.json')}'"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode != 0
    assert "reparse component" in completed.stderr


@pytest.mark.parametrize("foreign", ["project", "aliases"])
def test_prior_date_foreign_identity_never_calls_alias_provider(
    tmp_path: Path, foreign: str
) -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Assert-VercelPriorJournalHistoryTerminal", 1)[1].split(
        "function Assert-VercelJournalMatchesInvocation", 1
    )[0]
    history = tmp_path / "history"
    dated = history / "2026-08-31"
    dated.mkdir(parents=True)
    (dated / "vercel-publication-operation.json").write_text("{}", encoding="utf-8")
    payload = _pre_payload()
    payload.update(_authorization_fields())
    if foreign == "project":
        payload["project_id"] = "prj_foreign"
    else:
        payload["production_aliases"] = [*payload["production_aliases"], "https://evil.example"]
    payload_json = json.dumps(payload, separators=(",", ":")).replace("'", "''")
    history_text = str(history).replace("'", "''")
    state_text = str(tmp_path).replace("'", "''")
    command = (
        f"$journalHistoryRoot='{history_text}';$resolvedStateRoot='{state_text}';"
        "$resolvedExpectedMarketDate='2026-09-01';$ProjectId='prj_test';"
        "$ProjectName='dawnstrike-command-center-x3';$ProviderScope='mattfrens-projects';"
        "$allProductionAliases=@('https://dawnstrike-command-center-x3-mattfren-mattfrens-projects.vercel.app',"
        "'https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app');"
        f"$script:hostilePayload=ConvertFrom-Json '{payload_json}';$script:setCalls=0;"
        "function Invoke-VercelJournalTool {[pscustomobject]@{payload=$script:hostilePayload}};"
        "function Set-VercelAlias {$script:setCalls++};"
        "function Invoke-VercelPublicationCompensation {Set-VercelAlias};"
        "function Assert-VercelPriorJournalHistoryTerminal" + function + ";"
        "try {Assert-VercelPriorJournalHistoryTerminal}catch{};$setCalls"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "0"
    assert "$script:allProductionAliases = @($verified.payload.production_aliases)" not in script


@pytest.mark.parametrize("scenario", ["multiple_prior", "future"])
def test_ambiguous_or_future_nonterminal_history_never_calls_alias_provider(
    tmp_path: Path, scenario: str
) -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    function = script.split("function Assert-VercelPriorJournalHistoryTerminal", 1)[1].split(
        "function Assert-VercelJournalMatchesInvocation", 1
    )[0]
    history = tmp_path / "history"
    dates = ["2026-08-30", "2026-08-31"] if scenario == "multiple_prior" else ["2026-09-02"]
    for date in dates:
        dated = history / date
        dated.mkdir(parents=True)
        (dated / "vercel-publication-operation.json").write_text("{}", encoding="utf-8")
    payload = _pre_payload()
    payload.update(_authorization_fields())
    payload_json = json.dumps(payload, separators=(",", ":")).replace("'", "''")
    history_text = str(history).replace("'", "''")
    state_text = str(tmp_path).replace("'", "''")
    command = (
        f"$journalHistoryRoot='{history_text}';"
        f"$resolvedStateRoot='{state_text}';"
        "$resolvedExpectedMarketDate='2026-09-01';$ProjectId='prj_test';"
        "$ProjectName='dawnstrike-command-center-x3';$ProviderScope='mattfrens-projects';"
        "$allProductionAliases=@('https://dawnstrike-command-center-x3-mattfren-mattfrens-projects.vercel.app',"
        "'https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app');"
        f"$script:basePayload=ConvertFrom-Json '{payload_json}';$script:setCalls=0;"
        "function Invoke-VercelJournalTool {"
        "$copy=$script:basePayload|ConvertTo-Json -Depth 20|ConvertFrom-Json;"
        "$copy.candidate_market_date=Split-Path (Split-Path $Arguments[1] -Parent) -Leaf;"
        "[pscustomobject]@{payload=$copy}};function Set-VercelAlias {$script:setCalls++};"
        "function Invoke-VercelPublicationCompensation {Set-VercelAlias};"
        "function Assert-VercelPriorJournalHistoryTerminal" + function + ";"
        "try {Assert-VercelPriorJournalHistoryTerminal}catch{};$setCalls"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "0"


def test_complete_and_interrupted_recovery_repair_both_result_copies() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish_vercel_public.ps1").read_text(
        encoding="utf-8"
    )
    repair = script.split("function Repair-VercelResultCopies", 1)[1].split(
        "function Assert-LowerHex64", 1
    )[0]
    assert "$resultPath" in repair and "$runtimeResultPath" in repair
    assert "Write-VercelResultAtomic -Payload $Payload" in repair
    assert script.count("Repair-VercelResultCopies -Payload") >= 2
    recovery = script.split("function Complete-VercelJournalRecovery", 1)[1].split(
        "$recoveryRetry", 1
    )[0]
    assert "New-VercelRecoveredResultPayload" in recovery
    assert "$complete.result_payload = $freshResult" in recovery
    recovered = script.split("function New-VercelRecoveredResultPayload", 1)[1].split(
        "if ($Promote -or $PrepublicationAuthorizationId)", 1
    )[0]
    assert "Recovered preview governed asset manifest" in recovered
    assert "foreach ($alias in @($Journal.production_aliases))" in recovered
    authorization_calls = [
        index
        for index in range(len(script))
        if script.startswith("Assert-GovernedPublicationAuthorization", index)
    ]
    recovery_guard = script.index(
        'if ($RecoveryOnly) {\n'
        '    throw "Recovery-only Vercel convergence reached a fresh publication path."'
    )
    assert any(
        recovery_guard < index
        < script.index("scripts\\build_vercel_public_stage.ps1")
        for index in authorization_calls
    )
    assert any(
        script.index("scripts\\build_vercel_public_stage.ps1") < index
        < script.index('Invoke-VercelProcess `\n            -Arguments @("promote"')
        for index in authorization_calls
    )
    existing_boundary = script.split("$existingJournal = Get-VercelPublicationJournal", 1)[1].split(
        "function Get-VercelGovernedAssetProof", 1
    )[0]
    assert (
        "prepublication_authorization_id -ne $PrepublicationAuthorizationId"
        not in existing_boundary
    )


def test_compensated_terminal_dereferences_and_binds_receipt(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    prior = _pre_payload()
    prior.update(_authorization_fields())
    prior_path = root / "journal.json"
    prior_path.write_bytes(_seal(prior_path, prior))
    evidence = [
        {
            "alias": item["alias"],
            "expected_deployment_id": item["deployment_id"],
            "expected_deployment_url": item["deployment_url"],
            "observed_deployment_id": item["deployment_id"],
            "observed_deployment_url": item["deployment_url"],
            "restored": True,
            "health_status": item["health_status"],
            "readiness_status": item["readiness_status"],
            "readiness_http_status": item["readiness_http_status"],
            "source_sha": item["source_sha"],
            "source_tree": item["source_tree"],
            "source_manifest_sha256": item["source_manifest_sha256"],
            "build_manifest_sha256": item["build_manifest_sha256"],
            "release_manifest_sha256": item["release_manifest_sha256"],
            "artifact_proof": item["artifact_proof"],
        }
        for item in prior["prior_aliases"]
    ]
    compensation = {
        "schema_version": journal.COMPENSATION_SCHEMA,
        "status": "COMPENSATED",
        "operation": "vercel_publication",
        "candidate_source_sha": prior["candidate_source_sha"],
        "candidate_source_tree": prior["candidate_source_tree"],
        "candidate_preview_deployment_id": prior["candidate_preview_deployment_id"],
        "promoted_deployment_id": None,
        "promoted_deployment_url": None,
        "prior_aliases": prior["prior_aliases"],
        "rollback_evidence": evidence,
        "rollback_status": "ROLLED_BACK",
        "failure_type": "hostile_test",
        "research_only": True,
        "broker_execution_enabled": False,
        "recorded_at_utc": "2026-08-31T12:00:00.000000Z",
    }
    compensation["receipt_self_sha256"] = hashlib.sha256(
        journal.canonical_json(compensation)
    ).hexdigest()
    compensation_path = root / "outputs" / "compensation.json"
    compensation_path.parent.mkdir()
    compensation_path.write_bytes(journal.canonical_json(compensation))
    terminal = dict(prior)
    terminal.update(
        {
            "schema_version": journal.COMPENSATED_SCHEMA,
            "phase": "COMPENSATED",
            "sequence": 3,
            "compensation_relative_path": "outputs/compensation.json",
            "compensation_sha256": hashlib.sha256(compensation_path.read_bytes()).hexdigest(),
            "prior_journal_file_sha256": hashlib.sha256(prior_path.read_bytes()).hexdigest(),
        }
    )
    source = root / "terminal-input.json"
    source.write_bytes(journal.canonical_json(terminal))
    journal.transition(source, prior_path, prior_path, state_root=root)
    validated = journal.validate(
        prior_path.read_bytes(), state_root=root, journal_path=prior_path
    )
    assert validated["phase"] == "COMPENSATED"

    for field, hostile, message in (
        ("observed_deployment_id", "wrong-deployment", "observed deployment mismatch"),
        ("observed_deployment_url", "https://wrong.example", "observed URL mismatch"),
    ):
        hostile_compensation = json.loads(json.dumps(compensation))
        hostile_compensation["rollback_evidence"][0][field] = hostile
        unsigned = {
            key: value
            for key, value in hostile_compensation.items()
            if key != "receipt_self_sha256"
        }
        hostile_compensation["receipt_self_sha256"] = hashlib.sha256(
            journal.canonical_json(unsigned)
        ).hexdigest()
        with pytest.raises(ValueError, match=message):
            journal.validate_compensation(journal.canonical_json(hostile_compensation))

    tampered = dict(compensation)
    tampered["failure_type"] = "tampered"
    tampered["receipt_self_sha256"] = hashlib.sha256(
        journal.canonical_json({k: v for k, v in tampered.items() if k != "receipt_self_sha256"})
    ).hexdigest()
    compensation_path.write_bytes(journal.canonical_json(tampered))
    with pytest.raises(ValueError, match="compensation receipt raw hash"):
        journal.validate(prior_path.read_bytes(), state_root=root, journal_path=prior_path)
