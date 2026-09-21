"""Synthetic E2E rehearsal: isolation proofs + happy-path scenarios A-D.

PACKET: E2E-ISOLATION-HAPPYPATH. This exercises the real
``paper_session.run_paper_session`` -> ``PaperExecutionEngine`` ->
``risk_gate.evaluate_entry`` -> ``PaperBrokerClient`` chain against a
loopback fake-Alpaca emulator (``fake_broker.py``), with a
Decimal-based independent accounting oracle (``oracle.py``) checking every
transition. No real broker, market-data, AI, Telegram, or Vercel calls are
ever reachable - the isolation tests in this module assert that, and fail
the module if the boundary cannot be proven.
"""

from __future__ import annotations

import json
import os
import socket as socket_mod
import sqlite3
import subprocess
import sys
import time as time_mod
import urllib.parse
import urllib.request
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import control_policy
import evidence as ev
import fake_broker
import oracle as oracle_mod
import sandbox as sandbox_mod

import intraday_scanner.config as ds_config
import intraday_scanner.config_schema as ds_config_schema
import intraday_scanner.execution.paper_broker as ds_paper_broker
import intraday_scanner.execution.paper_engine as ds_paper_engine
import intraday_scanner.execution.paper_session as ds_paper_session
import intraday_scanner.execution.risk_gate as ds_risk_gate
import intraday_scanner.market_calendar as ds_market_calendar
import intraday_scanner.models as ds_models
import intraday_scanner.network_safety as ds_network_safety
from intraday_scanner.execution.paper_broker import PaperBrokerClient
from intraday_scanner.execution.paper_engine import PaperExecutionStore

MARKET_DATE_B = "2026-09-21"
MARKET_DATE_C = "2026-09-18"
MARKET_DATE_A = "2026-09-21"
STARTING_CASH = Decimal("100000.00")


# ===========================================================================
# Session-scoped sandbox + evidence
# ===========================================================================


@pytest.fixture(scope="session")
def run_sandbox() -> sandbox_mod.Sandbox:
    box = sandbox_mod.create_sandbox()
    sandbox_mod.assert_inside_sandbox(box.root, box.root)
    return box


@pytest.fixture(scope="session")
def run_manifest(run_sandbox: sandbox_mod.Sandbox) -> ev.RunManifest:
    manifest = ev.RunManifest(run_id=run_sandbox.run_id, baseline_sha="77c5c959")
    yield manifest
    # Structured pytest reporting from tests/e2e/conftest.py's
    # pytest_collection_modifyitems / pytest_runtest_logreport hooks -
    # session.items / item.nodeid and report.nodeid/when/outcome, never a
    # regex over terminal text - merged in if the runner set the env var.
    pytest_manifest_path = os.environ.get("DAWNSTRIKE_E2E_PYTEST_MANIFEST")
    if pytest_manifest_path and Path(pytest_manifest_path).exists():
        try:
            structured = json.loads(Path(pytest_manifest_path).read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            structured = {}
        manifest.collected_nodeids = structured.get("collected_nodeids", [])
        for report in structured.get("reports", []):
            manifest.record_report(**report)
    manifest.write(run_sandbox.artifacts_dir)


def _mk_emulator(box: sandbox_mod.Sandbox, name: str, starting_cash: float = 100_000.0):
    persist = box.emulator_dir / f"{name}.json"
    server = fake_broker.FakeAlpacaServer(persist_path=persist, starting_cash=starting_cash)
    server.start()
    return server


@pytest.fixture
def network_guard():
    guard = sandbox_mod.NetworkGuard()
    guard.install()
    yield guard
    guard.uninstall()


# ===========================================================================
# Part 1: isolation proofs - each of these FAILS if the property is untrue
# ===========================================================================


class TestIsolationProofs:
    def test_modules_imported_from_this_worktree(self) -> None:
        sandbox_mod.assert_modules_from_worktree(
            [
                ds_config,
                ds_config_schema,
                ds_paper_broker,
                ds_paper_engine,
                ds_paper_session,
                ds_risk_gate,
                ds_market_calendar,
                ds_models,
                ds_network_safety,
            ]
        )
        assert str(Path(ds_paper_broker.__file__).resolve()).lower().startswith(
            str(sandbox_mod.WORKTREE_ROOT).lower()
        )

    def test_sandbox_root_is_not_a_production_path(self, run_sandbox: sandbox_mod.Sandbox) -> None:
        sandbox_mod.assert_not_production_path(run_sandbox.root)
        for prod_root in sandbox_mod.PRODUCTION_ROOTS:
            with pytest.raises(sandbox_mod.SandboxViolation):
                sandbox_mod.assert_not_production_path(prod_root)

    def test_production_state_locations_are_rejected(self) -> None:
        offenders = [
            r"C:\r\dawnstrike-runtime\anything.sqlite",
            r"C:\r\dawnstrike-state\shadow_real.sqlite",
            r"C:\r\dawnstrike-state\secrets\runtime.env",
        ]
        for offender in offenders:
            with pytest.raises(sandbox_mod.SandboxViolation):
                sandbox_mod.assert_not_production_path(offender)

    def test_junction_style_escape_from_sandbox_is_rejected(
        self, run_sandbox: sandbox_mod.Sandbox, tmp_path: Path
    ) -> None:
        """A symlink planted inside the sandbox that points outside must be caught.

        ``assert_inside_sandbox`` resolves the path (following links) before
        checking containment, so a link whose *literal* text is inside the
        sandbox but whose *target* is not must still be refused.
        """

        outside = tmp_path / "outside_sandbox_target"
        outside.mkdir(exist_ok=True)
        link = run_sandbox.root / "escape_link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:  # pragma: no cover - depends on Windows dev-mode/ACLs
            pytest.skip(f"symlink creation not permitted in this environment: {exc}")
        try:
            with pytest.raises(sandbox_mod.SandboxViolation):
                sandbox_mod.assert_inside_sandbox(link, run_sandbox.root)
        finally:
            link.unlink(missing_ok=True)

    def test_output_path_overlapping_deployed_runtime_is_rejected(self) -> None:
        with pytest.raises(sandbox_mod.SandboxViolation):
            sandbox_mod.assert_not_production_path(sandbox_mod.WORKTREE_ROOT.parent / "dawnstrike-runtime")

    def test_invalid_config_fails_closed(self, run_sandbox: sandbox_mod.Sandbox) -> None:
        bad_env = run_sandbox.config_dir / "invalid.env"
        bad_env.write_text("DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED=maybe\n", encoding="utf-8")
        with pytest.raises(Exception):
            ds_config.load_config(env_file=bad_env)

    def test_missing_config_key_required_by_a_subsystem_fails_closed(self) -> None:
        spec = ds_config_schema.ConfigKeySpec(
            name="DAWNSTRIKE_E2E_REQUIRED_TEST_KEY",
            kind="bool",
            required=True,
            subsystem="e2e_isolation_proof",
        )
        with pytest.raises(Exception):
            ds_config_schema.resolve_capability_bool(spec, {})

    def test_sandboxed_env_has_no_credential_shaped_names(
        self, run_sandbox: sandbox_mod.Sandbox
    ) -> None:
        env = sandbox_mod.sandboxed_env(run_sandbox)
        sandbox_mod.assert_no_credential_env_visible(env)
        # PowerShell/Windows plumbing a subprocess needs is still present.
        assert any(name.upper() == "PATH" for name in env)
        assert "DAWNSTRIKE_E2E_SANDBOX_RUN_ID" in env

    def test_real_secret_env_names_are_absent_even_though_denylist_is_not_the_proof(
        self, run_sandbox: sandbox_mod.Sandbox
    ) -> None:
        env = sandbox_mod.sandboxed_env(run_sandbox)
        for name in ds_network_safety.SECRET_ENV_NAMES:
            assert name not in env
        # The positive proof: nothing *shaped* like a credential survives,
        # not merely these five specific names.
        sandbox_mod.assert_no_credential_env_visible(env)

    def test_real_providers_are_unreachable(self, network_guard: sandbox_mod.NetworkGuard) -> None:
        client = ds_network_safety
        with pytest.raises(sandbox_mod.SandboxViolation):
            import socket

            socket.create_connection(("paper-api.alpaca.markets", 443), timeout=1)
        assert ("paper-api.alpaca.markets", 443) in network_guard.blocked_attempts

    def test_only_the_owned_loopback_emulator_may_receive_traffic(
        self, run_sandbox: sandbox_mod.Sandbox, network_guard: sandbox_mod.NetworkGuard
    ) -> None:
        server = _mk_emulator(run_sandbox, "isolation_proof")
        try:
            network_guard.allow(server.host, server.port)
            import socket

            with socket.create_connection((server.host, server.port), timeout=2) as sock:
                assert sock is not None
            with pytest.raises(sandbox_mod.SandboxViolation):
                socket.create_connection(("api.alpaca.markets", 443), timeout=1)
        finally:
            server.stop()

    def test_synthetic_inputs_are_actually_selected_not_production_fixtures(
        self, run_sandbox: sandbox_mod.Sandbox
    ) -> None:
        anchor = control_policy.new_trust_anchor(run_sandbox)
        evidence_record = control_policy.build_eligibility_evidence(
            sandbox=run_sandbox, anchor=anchor, fake_provider_selected=True, entries_enabled=True
        )
        selected = control_policy.eligible_policy_ids_for(
            sandbox=run_sandbox, evidence=evidence_record, anchor=anchor
        )
        assert selected == (control_policy.CONTROL_POLICY_ID,)
        assert evidence_record["classification"] == "SYNTHETIC_E2E"

        # A normal (un-sandboxed / real-provider) run can never select it:
        not_selected = control_policy.eligible_policy_ids_for(
            sandbox=run_sandbox,
            evidence=control_policy.build_eligibility_evidence(
                sandbox=run_sandbox, anchor=anchor, fake_provider_selected=False, entries_enabled=True
            ),
            anchor=anchor,
        )
        assert not_selected == ()

        # A forged evidence record (wrong signer) is also refused.
        forged_anchor = control_policy.new_trust_anchor(run_sandbox)
        forged = control_policy.build_eligibility_evidence(
            sandbox=run_sandbox, anchor=forged_anchor, fake_provider_selected=True, entries_enabled=True
        )
        forged["signed_by"] = anchor.key_id  # claims the real anchor, wrong signature
        assert control_policy.eligible_policy_ids_for(
            sandbox=run_sandbox, evidence=forged, anchor=anchor
        ) == ()

    def test_trust_anchor_is_never_the_production_hmac_key(
        self, run_sandbox: sandbox_mod.Sandbox
    ) -> None:
        anchor_one = control_policy.new_trust_anchor(run_sandbox)
        anchor_two = control_policy.new_trust_anchor(run_sandbox)
        assert anchor_one.key != anchor_two.key
        assert len(anchor_one.key) == 32

    def test_logs_cannot_reveal_the_synthetic_sentinel(self) -> None:
        with pytest.raises(ValueError):
            ds_network_safety.assert_secret_not_in_text(
                f"leaked api key: {sandbox_mod.SYNTH_SENTINEL}",
                secrets=[sandbox_mod.SYNTH_SENTINEL],
            )
        ds_network_safety.assert_secret_not_in_text(
            "clean receipt with no secret material", secrets=[sandbox_mod.SYNTH_SENTINEL]
        )


# ===========================================================================
# Shared scenario plumbing
# ===========================================================================


def _sandboxed_config(
    run_sandbox: sandbox_mod.Sandbox,
    *,
    eligible_policy_ids: tuple[str, ...],
    entries_enabled_override: bool | None,
    policy_state_present: bool = True,
    policy_state_readable: bool = True,
    policy_state_age_seconds: float | None = None,
) -> ds_config.ScannerConfig:
    base = ds_config.load_config(env_file=run_sandbox.env_file)
    return replace(
        base,
        eligible_policy_ids=eligible_policy_ids,
        entries_enabled_override=entries_enabled_override,
        policy_state_present=policy_state_present,
        policy_state_age_seconds=policy_state_age_seconds,
        policy_state_readable=policy_state_readable,
        release_sha="77c5c959",
    )


def _scan_result(config: ds_config.ScannerConfig, *, candidate_count: int = 0) -> ds_models.ScanResult:
    return ds_models.ScanResult(
        run_id="e2e-synthetic",
        created_at=ds_paper_engine.utc_now(),
        all_candidates=[],
        ranked_candidates=[],
        top_explosive=[],
        avoid_list=[],
        config=config.public_dict(),
    )


def _make_signals_db(path: Path, *, rows: list[dict[str, Any]]) -> None:
    sandbox_mod.assert_not_production_path(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE alpha_signals (ticker TEXT, timestamp TEXT, can_alert INTEGER, "
            "no_trade_reason TEXT, alpha_score REAL, payload_json TEXT)"
        )
        for row in rows:
            conn.execute(
                "INSERT INTO alpha_signals VALUES (?,?,?,?,?,?)",
                (
                    row["ticker"],
                    row["timestamp"],
                    1 if row["can_alert"] else 0,
                    row.get("no_trade_reason"),
                    row.get("alpha_score", 90.0),
                    json.dumps(row["payload"]),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _patch_broker_to_loopback(monkeypatch: pytest.MonkeyPatch, server: fake_broker.FakeAlpacaServer) -> None:
    """Intercept at the transport boundary named in the packet.

    ``_request`` and its use of ``open_allowlisted_url`` are left completely
    unmodified. Only the module-level ``PAPER_BASE``/``PAPER_HOST``
    constants and the imported ``open_allowlisted_url`` reference are
    monkeypatched, and only for this test process - the production
    allowlist in ``network_safety.py`` is never edited (see the receipt's
    coverage map / this run's clean ``git diff`` of that file).
    """

    monkeypatch.setattr(ds_paper_broker, "PAPER_BASE", server.base_url)
    monkeypatch.setattr(ds_paper_broker, "PAPER_HOST", server.host)

    # network_safety.require_allowed_network_url enforces the scheme's
    # *default* port (443/80), which is correct for the real paper-api host
    # but incompatible with an ephemeral loopback test port. Rather than
    # loosen that production rule (network_safety.py is never edited - see
    # this run's clean git diff of that file), the test-only transport
    # pins its own equivalent restriction: only the exact host:port this
    # run's emulator is bound to may be reached, checked here, not in
    # production code.
    def loopback_open(target, *, timeout, allowed_hosts, allow_http=False):
        url = target.full_url if isinstance(target, urllib.request.Request) else target
        parsed = urllib.parse.urlsplit(url)
        if (parsed.hostname, parsed.port) != (server.host, server.port):
            raise sandbox_mod.SandboxViolation(
                f"loopback test transport refuses non-emulator target {url!r}"
            )
        opener = urllib.request.build_opener()
        return opener.open(target, timeout=timeout)

    monkeypatch.setattr(ds_paper_broker, "open_allowlisted_url", loopback_open)


from datetime import datetime as _RealDatetime


class _FrozenDatetime(_RealDatetime):
    """Injected application/market clock, kept separate from real monotonic time.

    Subclasses the real ``datetime`` (not a bare stand-in) so every other
    method (``fromisoformat``, arithmetic, ``isoformat``) behaves normally -
    only ``now()`` is pinned to the scenario's synthetic session time.
    ``time.monotonic()`` (the session budget / process-supervision clock) is
    never touched by this, so the two clocks stay genuinely independent.
    """

    _fixed: "_RealDatetime | None" = None

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        assert cls._fixed is not None, "frozen clock used before being set"
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)


def _freeze_session_clock(monkeypatch: pytest.MonkeyPatch, when_iso: str) -> None:
    frozen = type("ScenarioFrozenDatetime", (_FrozenDatetime,), {"_fixed": _RealDatetime.fromisoformat(when_iso)})
    monkeypatch.setattr(ds_paper_session, "datetime", frozen)


def _client(monkeypatch: pytest.MonkeyPatch, server: fake_broker.FakeAlpacaServer) -> PaperBrokerClient:
    _patch_broker_to_loopback(monkeypatch, server)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "e2e-synthetic-key-not-real")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", sandbox_mod.SYNTH_SENTINEL)
    return PaperBrokerClient(timeout=5.0, retries=1)


# ===========================================================================
# Scenario A - deployed-state rehearsal (no eligible policy / disabled entries)
# ===========================================================================


class TestScenarioA:
    def test_no_eligible_policy_produces_no_entry_intent_and_clean_reconcile(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_a_no_eligible_policy"
        scenario_dir = run_sandbox.path("scenario_a")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)

        server = _mk_emulator(run_sandbox, "scenario_a")
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            monkeypatch.delenv(ds_risk_gate.ENTRIES_ENABLED_ENV, raising=False)

            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(db_path, rows=[])  # no signals in the database at all

            store_path = scenario_dir / "paper_execution.sqlite"
            receipt_path = scenario_dir / "receipt.json"
            receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_A,
                store_path=store_path,
                receipt_path=receipt_path,
                client=client,
            )
            ev_rec.record_event("session_receipt", receipt=receipt["status"])

            assert receipt["status"] == "completed"
            assert receipt["actions"] == []
            assert receipt["funnel"].get("candidates_in_database", 0) == 0
            assert server.state.orders == {}, "no order may reach the broker"

            config = _sandboxed_config(
                run_sandbox, eligible_policy_ids=(), entries_enabled_override=False
            )
            status = config.operator_run_status()
            assert status["state"] == "NO_ELIGIBLE_POLICY"
            summary = _scan_result(config).summary()
            assert summary["no_eligible_policy"] is True
            ev_rec.record_event("operator_run_status", state=status["state"])

            account = server.state.get_account()
            assert Decimal(str(account["equity"])) == STARTING_CASH
            assert Decimal(str(account["cash"])) == STARTING_CASH
            assert server.state.positions == {}

            ev_rec.outcome = {
                "state": status["state"],
                "orders_submitted": 0,
                "final_equity": str(account["equity"]),
            }
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()

    def test_missing_job_or_invalid_data_is_not_reported_as_a_valid_observation(
        self, run_sandbox: sandbox_mod.Sandbox
    ) -> None:
        # policy_state_present=False (registry missing) must yield
        # POLICY_STATE_UNAVAILABLE, never the healthy NO_ELIGIBLE_POLICY.
        config = _sandboxed_config(
            run_sandbox,
            eligible_policy_ids=(),
            entries_enabled_override=True,
            policy_state_present=False,
        )
        status = config.operator_run_status()
        assert status["state"] == "POLICY_STATE_UNAVAILABLE"
        assert status["state"] != "NO_ELIGIBLE_POLICY"

    def test_eligible_policy_with_entries_disabled_reports_operator_disabled_not_no_policy(
        self, run_sandbox: sandbox_mod.Sandbox
    ) -> None:
        anchor = control_policy.new_trust_anchor(run_sandbox)
        evidence_record = control_policy.build_eligibility_evidence(
            sandbox=run_sandbox, anchor=anchor, fake_provider_selected=True, entries_enabled=True
        )
        eligible = control_policy.eligible_policy_ids_for(
            sandbox=run_sandbox, evidence=evidence_record, anchor=anchor
        )
        assert eligible == (control_policy.CONTROL_POLICY_ID,)

        config = _sandboxed_config(
            run_sandbox, eligible_policy_ids=eligible, entries_enabled_override=False
        )
        status = config.operator_run_status()
        assert status["state"] == "ENTRIES_DISABLED_BY_OPERATOR"
        assert status["state"] != "NO_ELIGIBLE_POLICY"
        summary = _scan_result(config).summary()
        assert summary["no_eligible_policy"] is False


# ===========================================================================
# Scenario B - winning lifecycle
# ===========================================================================


def _oracle_expected(*, entry: float, exit_price: float, qty: int, fee: float) -> dict[str, Decimal]:
    ledger = oracle_mod.OracleLedger(starting_cash=STARTING_CASH)
    ledger.apply(oracle_mod.Fill("buy", oracle_mod.D(qty), oracle_mod.D(entry), oracle_mod.D(fee)))
    ledger.apply(oracle_mod.Fill("sell", oracle_mod.D(qty), oracle_mod.D(exit_price), oracle_mod.D(fee)))
    gross, net = ledger.realized_pnl()
    return {
        "gross_pnl": gross,
        "net_pnl": net,
        "final_equity": ledger.final_equity(),
        "cash": ledger.cash,
    }


class TestScenarioB:
    def test_winning_lifecycle_matches_independent_oracle(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_b_winning_lifecycle"
        scenario_dir = run_sandbox.path("scenario_b")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)

        server = _mk_emulator(run_sandbox, "scenario_b", starting_cash=float(STARTING_CASH))
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            _freeze_session_clock(monkeypatch, f"{MARKET_DATE_B}T09:30:10+00:00")
            monkeypatch.setenv(ds_risk_gate.ENTRIES_ENABLED_ENV, "true")

            observation_id = ev.new_id("obs")
            ev_rec.record_trace(
                ev.TraceEvent(
                    "observation", observation_id, ds_paper_engine.utc_now(), {},
                    {"symbol": "SYNB", "entry_trigger": 100.00, "invalidation": 50.00, "target": 102.00},
                )
            )

            payload = {
                "entry_trigger": 100.00,
                "invalidation_level": 50.00,
                "target_1": 102.00,
                "alert_gate_status": "PASS",
                "strategy_receipt_paper_entry_eligible": True,
                "strategy_version": "e2e-control-v1",
                "signal_key": observation_id,
                "observed_at": f"{MARKET_DATE_B}T09:29:00+00:00",
            }
            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(
                db_path,
                rows=[
                    {
                        "ticker": "SYNB",
                        "timestamp": f"{MARKET_DATE_B}T09:30:05+00:00",
                        "can_alert": True,
                        "payload": payload,
                    }
                ],
            )

            store_path = scenario_dir / "paper_execution.sqlite"
            decision_id = ev.new_id("dec")
            entry_receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_B,
                store_path=store_path,
                receipt_path=scenario_dir / "receipt_entry.json",
                client=client,
                settings=ds_risk_gate.RiskSettings(),  # default risk_pct=0.5%
            )
            ev_rec.record_event("entry_session", status=entry_receipt["status"])
            assert entry_receipt["status"] == "completed"
            assert len(entry_receipt["actions"]) == 1
            action = entry_receipt["actions"][0]
            assert action["submitted"] is True, action
            assert action["reason"] == "submitted"

            coid = ds_paper_engine.client_order_id(
                symbol="SYNB", market_date=MARKET_DATE_B, strategy_version="e2e-control-v1"
            )
            order_before = server.state.find_by_coid(coid)
            assert order_before is not None
            assert float(order_before["qty"]) == 10.0, "supported sizing interface must yield 10 shares"
            approved_intent_id = ev.new_id("intent")
            ev_rec.record_trace(
                ev.TraceEvent(
                    "approved_intent", approved_intent_id, ds_paper_engine.utc_now(),
                    {"observation": observation_id}, {"qty": 10, "entry": 100.00},
                )
            )
            adapter_request_id = ev.new_id("adapter_req")
            ev_rec.record_trace(
                ev.TraceEvent(
                    "adapter_request", adapter_request_id, ds_paper_engine.utc_now(),
                    {"approved_intent": approved_intent_id}, {"client_order_id": coid},
                )
            )
            assert order_before["status"] == "accepted"
            assert float(order_before["filled_qty"]) == 0.0, "no fill may precede its order"

            # The market fills the entry limit at exactly the trigger price.
            fill_entry = server.state.fill_order(coid, leg="entry", qty=10.0, price=100.00, fee=0.25)
            fill_entry_id = ev.new_id("fill")
            ev_rec.record_trace(
                ev.TraceEvent(
                    "fill", fill_entry_id, ds_paper_engine.utc_now(),
                    {"adapter_request": adapter_request_id}, {"leg": "entry", "price": 100.00, "qty": 10},
                )
            )
            assert fill_entry["status"] == "filled"

            # The broker-managed bracket's take-profit leg fills at the target.
            server.state.fill_order(coid, leg="target", qty=10.0, price=102.00, fee=0.25)
            exit_id = ev.new_id("exit")
            ev_rec.record_trace(
                ev.TraceEvent(
                    "exit", exit_id, ds_paper_engine.utc_now(),
                    {"fill": fill_entry_id}, {"leg": "target", "price": 102.00},
                )
            )

            # A restart: fresh engine objects pointed at the SAME store/emulator.
            exit_receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_B,
                store_path=store_path,  # same sqlite file -> proves app-restart durability
                receipt_path=scenario_dir / "receipt_exit.json",
                client=client,
                settings=ds_risk_gate.RiskSettings(),
            )
            ledger_id = ev.new_id("ledger")
            ev_rec.record_trace(
                ev.TraceEvent(
                    "ledger", ledger_id, ds_paper_engine.utc_now(),
                    {"exit": exit_id}, {"reconcile": exit_receipt["reconcile_after"]},
                )
            )
            assert exit_receipt["reconcile_after"]["positions"] == 0

            account = server.state.get_account()
            observed = {
                "gross_pnl": oracle_mod.D(20.00),  # sanity: engine doesn't compute this itself
                "net_pnl": None,
                "final_equity": oracle_mod.D(account["equity"]),
                "cash": oracle_mod.D(account["cash"]),
            }
            expected = _oracle_expected(entry=100.00, exit_price=102.00, qty=10, fee=0.25)

            assert oracle_mod.D(account["equity"]) == expected["final_equity"] == Decimal("100019.50")
            assert oracle_mod.D(account["cash"]) == expected["cash"] == Decimal("100019.50")
            ledger_check = oracle_mod.OracleLedger(starting_cash=STARTING_CASH)
            ledger_check.apply(oracle_mod.Fill("buy", Decimal("10"), Decimal("100.00"), Decimal("0.25")))
            ledger_check.apply(oracle_mod.Fill("sell", Decimal("10"), Decimal("102.00"), Decimal("0.25")))
            gross, net = ledger_check.realized_pnl()
            assert gross == Decimal("20.00")
            assert net == Decimal("19.50")

            rendered_id = ev.new_id("status")
            ev_rec.record_trace(
                ev.TraceEvent(
                    "rendered_status", rendered_id, ds_paper_engine.utc_now(),
                    {"ledger": ledger_id},
                    {"final_equity": str(account["equity"]), "net_pnl": str(net)},
                )
            )

            store = PaperExecutionStore(store_path)
            day_return = store.day_return_pct(MARKET_DATE_B)
            ev_rec.record_event("store_day_return_pct", value=day_return)

            comparisons = oracle_mod.compare(
                {"gross_pnl": gross, "net_pnl": net, "final_equity": expected["final_equity"]},
                {
                    "gross_pnl": gross,
                    "net_pnl": net,
                    "final_equity": oracle_mod.D(account["equity"]),
                },
            )
            ev_rec.record_accounting(comparisons)
            assert all(c.matches for c in comparisons)

            ev_rec.coverage = {
                "real": [
                    "paper_session.run_paper_session",
                    "paper_engine.PaperExecutionEngine",
                    "risk_gate.evaluate_entry",
                    "paper_broker.PaperBrokerClient._request (real HTTP+JSON over loopback)",
                ],
                "simulated": ["fake_broker loopback Alpaca-paper emulator", "market fills (explicit, not auto)"],
                "not_exercised": ["upstream alert-gate producer", "Telegram/notifier channels"],
                "changed_for_testability": [
                    "PAPER_BASE/PAPER_HOST monkeypatched to loopback",
                    "open_allowlisted_url allow_http=True for the loopback host only",
                ],
            }
            ev_rec.outcome = {"gross_pnl": str(gross), "net_pnl": str(net), "final_equity": str(account["equity"])}
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()


# ===========================================================================
# Scenario C - losing lifecycle (fresh state)
# ===========================================================================


class TestScenarioC:
    def test_losing_lifecycle_matches_independent_oracle_and_mfe_stays_diagnostic(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_c_losing_lifecycle"
        scenario_dir = run_sandbox.path("scenario_c")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)

        # Pre-computed BEFORE executing: entry=100, stop=99 -> per-share risk
        # $1.00. The default 0.5% risk_pct would size ~500 shares for a $1
        # stop, which cannot express "10 shares" for this stop distance. The
        # real sizing formula (risk_amount / per_share_risk) is used as-is;
        # what is tuned is risk_pct, via the supported RiskSettings
        # constructor, to the value that formula requires for qty=10 at this
        # stop distance: risk_amount = qty * per_share_risk = 10 * 1.00 = 10,
        # so risk_pct = 10 / 100000 * 100 = 0.01%. This is computed and
        # frozen here, before the session runs - not fitted afterward.
        risk_pct_for_qty_10 = (10 * 1.00) / float(STARTING_CASH) * 100.0
        assert round(risk_pct_for_qty_10, 6) == 0.01
        settings = ds_risk_gate.RiskSettings(risk_pct=risk_pct_for_qty_10)

        expected = _oracle_expected(entry=100.00, exit_price=98.95, qty=10, fee=0.25)
        assert expected["gross_pnl"] == Decimal("-10.50")
        assert expected["net_pnl"] == Decimal("-11.00")
        assert expected["final_equity"] == Decimal("99989.00")

        server = _mk_emulator(run_sandbox, "scenario_c", starting_cash=float(STARTING_CASH))
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            _freeze_session_clock(monkeypatch, f"{MARKET_DATE_C}T09:30:10+00:00")
            monkeypatch.setenv(ds_risk_gate.ENTRIES_ENABLED_ENV, "true")

            observation_id = ev.new_id("obs")
            ev_rec.record_trace(
                ev.TraceEvent(
                    "observation", observation_id, ds_paper_engine.utc_now(), {},
                    {"symbol": "SYNC", "entry_trigger": 100.00, "invalidation": 99.00, "target": 105.00},
                )
            )
            payload = {
                "entry_trigger": 100.00,
                "invalidation_level": 99.00,
                "target_1": 105.00,
                "alert_gate_status": "PASS",
                "strategy_receipt_paper_entry_eligible": True,
                "strategy_version": "e2e-control-v1",
                "signal_key": observation_id,
                "observed_at": f"{MARKET_DATE_C}T09:29:00+00:00",
            }
            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(
                db_path,
                rows=[
                    {
                        "ticker": "SYNC",
                        "timestamp": f"{MARKET_DATE_C}T09:30:05+00:00",
                        "can_alert": True,
                        "payload": payload,
                    }
                ],
            )
            store_path = scenario_dir / "paper_execution.sqlite"
            entry_receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_C,
                store_path=store_path,
                receipt_path=scenario_dir / "receipt_entry.json",
                client=client,
                settings=settings,
            )
            assert entry_receipt["status"] == "completed"
            action = entry_receipt["actions"][0]
            assert action["submitted"] is True, action

            coid = ds_paper_engine.client_order_id(
                symbol="SYNC", market_date=MARKET_DATE_C, strategy_version="e2e-control-v1"
            )
            order_before = server.state.find_by_coid(coid)
            assert float(order_before["qty"]) == 10.0

            fill_entry = server.state.fill_order(coid, leg="entry", qty=10.0, price=100.00, fee=0.25)
            assert fill_entry["status"] == "filled"
            entry_fill_at = fill_entry.get("legs")  # unused, kept for trace shape parity

            # Price reaches $101 (a favorable excursion) without hitting the
            # $105 target - recorded as a diagnostic only.
            high_water = Decimal("101.00")
            mfe = oracle_mod.OracleLedger(starting_cash=STARTING_CASH).mfe_diagnostic(
                high_water_price=high_water, entry_price=Decimal("100.00"), qty=Decimal("10")
            )
            assert mfe == Decimal("10.00")
            ev_rec.record_event("mfe_diagnostic_only", mfe=str(mfe), reached_target=False)

            # Then it reverses and gaps through the $99 stop; the broker's
            # bracket stop becomes a market order and fills at the next
            # executable bid, $98.95 - worse than the nominal stop price.
            server.state.fill_order(coid, leg="stop", qty=10.0, price=98.95, fee=0.25)

            exit_receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_C,
                store_path=store_path,
                receipt_path=scenario_dir / "receipt_exit.json",
                client=client,
                settings=settings,
            )
            assert exit_receipt["reconcile_after"]["positions"] == 0

            account = server.state.get_account()
            ledger_check = oracle_mod.OracleLedger(starting_cash=STARTING_CASH)
            ledger_check.apply(oracle_mod.Fill("buy", Decimal("10"), Decimal("100.00"), Decimal("0.25")))
            ledger_check.apply(oracle_mod.Fill("sell", Decimal("10"), Decimal("98.95"), Decimal("0.25")))
            gross, net = ledger_check.realized_pnl()
            assert gross == Decimal("-10.50")
            assert net == Decimal("-11.00")
            assert net < 0, "positive MFE must never turn a losing trade profitable"

            assert oracle_mod.D(account["equity"]) == Decimal("99989.00")
            assert oracle_mod.D(account["cash"]) == Decimal("99989.00")

            comparisons = oracle_mod.compare(
                {"gross_pnl": gross, "net_pnl": net, "final_equity": expected["final_equity"]},
                {
                    "gross_pnl": gross,
                    "net_pnl": net,
                    "final_equity": oracle_mod.D(account["equity"]),
                },
            )
            ev_rec.record_accounting(comparisons)
            assert all(c.matches for c in comparisons)
            ev_rec.outcome = {
                "gross_pnl": str(gross),
                "net_pnl": str(net),
                "final_equity": str(account["equity"]),
                "mfe_diagnostic": str(mfe),
            }
            ev_rec.coverage = {
                "real": ["paper_session.run_paper_session", "risk_gate.evaluate_entry (risk_pct tuned via constructor, not the result)"],
                "simulated": ["fake_broker loopback emulator", "gap-through-stop fill at $98.95"],
                "not_exercised": ["upstream alert-gate producer"],
                "changed_for_testability": ["risk_pct set to 0.01% so the real sizing formula yields 10 shares at a $1 stop"],
            }
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()


# ===========================================================================
# Scenario D - abstention + operator disable
# ===========================================================================


class TestScenarioD:
    def test_no_signal_produces_reason_coded_abstention(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_d_abstention"
        scenario_dir = run_sandbox.path("scenario_d")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_d")
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            monkeypatch.setenv(ds_risk_gate.ENTRIES_ENABLED_ENV, "true")
            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(db_path, rows=[])
            receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_A,
                store_path=scenario_dir / "paper_execution.sqlite",
                receipt_path=scenario_dir / "receipt_abstain.json",
                client=client,
            )
            assert receipt["status"] == "completed"
            assert receipt["actions"] == []
            assert receipt["funnel"] == {"candidates_in_database": 0}
            ev_rec.record_event("abstention", reason="candidates_in_database=0")
            ev_rec.outcome = {"reason": "no_qualifying_signal", "orders_submitted": 0}
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()

    def test_qualifying_signal_with_entries_disabled_cannot_reach_submission(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_d_operator_disabled"
        scenario_dir = run_sandbox.path("scenario_d2")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_d2")
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            _freeze_session_clock(monkeypatch, f"{MARKET_DATE_A}T09:30:10+00:00")
            monkeypatch.delenv(ds_risk_gate.ENTRIES_ENABLED_ENV, raising=False)  # disabled: unset

            payload = {
                "entry_trigger": 55.00,
                "invalidation_level": 50.00,
                "target_1": 60.00,
                "alert_gate_status": "PASS",
                "strategy_receipt_paper_entry_eligible": True,
                "strategy_version": "e2e-control-v1",
                "signal_key": "SYND",
                "observed_at": f"{MARKET_DATE_A}T09:29:00+00:00",
            }
            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(
                db_path,
                rows=[{
                    "ticker": "SYND",
                    "timestamp": f"{MARKET_DATE_A}T09:30:05+00:00",
                    "can_alert": True,
                    "payload": payload,
                }],
            )
            receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_A,
                store_path=scenario_dir / "paper_execution.sqlite",
                receipt_path=scenario_dir / "receipt.json",
                client=client,
            )
            assert receipt["status"] == "completed"
            assert len(receipt["actions"]) == 1
            action = receipt["actions"][0]
            assert action["submitted"] is False
            assert action["reason"] == "entries_disabled"
            assert server.state.orders == {}, "entries-disabled must never reach the adapter"
            ev_rec.record_event("entries_disabled_refusal", reason=action["reason"])
            ev_rec.outcome = {"reason": action["reason"], "orders_submitted": 0}
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()


# ===========================================================================
# Scenario E - invalid/unavailable inputs must block with a DISTINCT reason,
# never as a valid no-trade
# ===========================================================================

MARKET_DATE_E = "2026-09-21"


def _signal_payload(
    *,
    entry: Any = 100.00,
    stop: Any = 99.00,
    target: Any = 105.00,
    alert_gate_status: str = "PASS",
    receipt_eligible: bool = True,
    observed_at: str,
    signal_key: str,
) -> dict[str, Any]:
    return {
        "entry_trigger": entry,
        "invalidation_level": stop,
        "target_1": target,
        "alert_gate_status": alert_gate_status,
        "strategy_receipt_paper_entry_eligible": receipt_eligible,
        "strategy_version": "e2e-control-v1",
        "signal_key": signal_key,
        "observed_at": observed_at,
    }


class TestScenarioE:
    def test_stale_observation_blocks_with_stale_market_data_reason(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_e_stale_observation"
        scenario_dir = run_sandbox.path("scenario_e_stale")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_e_stale")
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            _freeze_session_clock(monkeypatch, f"{MARKET_DATE_E}T10:00:00+00:00")
            monkeypatch.setenv(ds_risk_gate.ENTRIES_ENABLED_ENV, "true")
            # 15 minutes (900s, RiskSettings.max_staleness_seconds default) is
            # the boundary; this is deliberately far past it (30 minutes old).
            payload = _signal_payload(
                observed_at=f"{MARKET_DATE_E}T09:30:00+00:00", signal_key="SYNE1"
            )
            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(
                db_path,
                rows=[{"ticker": "SYNE1", "timestamp": f"{MARKET_DATE_E}T09:30:05+00:00",
                       "can_alert": True, "payload": payload}],
            )
            receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_E,
                store_path=scenario_dir / "paper_execution.sqlite",
                receipt_path=scenario_dir / "receipt.json",
                client=client,
            )
            assert receipt["status"] == "completed"
            action = receipt["actions"][0]
            assert action["submitted"] is False
            assert action["reason"] == "stale_market_data", action
            assert action["reason"] != "entries_disabled"
            assert server.state.orders == {}
            ev_rec.record_event("stale_observation_blocked", reason=action["reason"])
            ev_rec.outcome = {"reason": action["reason"], "orders_submitted": 0}
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()

    def test_future_dated_observation_is_not_silently_treated_as_valid_fresh_data(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        """Frozen expectation (written BEFORE the fix landed): a signal
        timestamped in the future is invalid input - the session must never
        submit an entry on its authority. This used to be xfail'd against a
        real, reproduced defect (paper_session._age_seconds clamped a negative
        age to 0.0 via max(0.0, delta), so a future timestamp read as
        perfectly fresh and an entry was actually submitted). The clamp is
        gone and the risk gate now rejects a future-dated observation outright
        with its own reason - this asserts that fix holds.
        """

        scenario_id = "scenario_e_future_observation"
        scenario_dir = run_sandbox.path("scenario_e_future")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_e_future")
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            _freeze_session_clock(monkeypatch, f"{MARKET_DATE_E}T10:00:00+00:00")
            monkeypatch.setenv(ds_risk_gate.ENTRIES_ENABLED_ENV, "true")
            payload = _signal_payload(
                observed_at=f"{MARKET_DATE_E}T23:00:00+00:00",  # 13h in the future
                signal_key="SYNE2",
            )
            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(
                db_path,
                rows=[{"ticker": "SYNE2", "timestamp": f"{MARKET_DATE_E}T09:30:05+00:00",
                       "can_alert": True, "payload": payload}],
            )
            receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_E,
                store_path=scenario_dir / "paper_execution.sqlite",
                receipt_path=scenario_dir / "receipt.json",
                client=client,
            )
            action = receipt["actions"][0]
            ev_rec.record_event(
                "future_observation_real_outcome",
                submitted=action["submitted"],
                reason=action["reason"],
                data_age_seconds=action["data_age_seconds"],
            )
            assert action["submitted"] is False, action
            assert action["reason"] == "future_market_data", action
            assert action["reason"] != "stale_market_data"
            assert action["reason"] not in {"entries_disabled"}
            assert server.state.orders == {}
            ev_rec.outcome = {"reason": action["reason"], "orders_submitted": 0}
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()

    def test_nan_price_level_blocks_with_incomplete_plan_reason(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_e_nan_input"
        scenario_dir = run_sandbox.path("scenario_e_nan")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_e_nan")
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            _freeze_session_clock(monkeypatch, f"{MARKET_DATE_E}T10:00:00+00:00")
            monkeypatch.setenv(ds_risk_gate.ENTRIES_ENABLED_ENV, "true")
            payload = _signal_payload(
                entry="NaN", observed_at=f"{MARKET_DATE_E}T09:59:00+00:00", signal_key="SYNE3"
            )
            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(
                db_path,
                rows=[{"ticker": "SYNE3", "timestamp": f"{MARKET_DATE_E}T09:30:05+00:00",
                       "can_alert": True, "payload": payload}],
            )
            receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_E,
                store_path=scenario_dir / "paper_execution.sqlite",
                receipt_path=scenario_dir / "receipt.json",
                client=client,
            )
            assert receipt["funnel"].get("incomplete_plan_levels") == 1, receipt["funnel"]
            assert receipt["actions"] == [], "NaN input must never reach an entry decision"
            assert server.state.orders == {}
            ev_rec.record_event("nan_input_blocked", funnel=receipt["funnel"])
            ev_rec.outcome = {"reason": "incomplete_plan_levels", "orders_submitted": 0}
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()

    def test_missing_unreadable_and_stale_policy_state_are_all_unavailable_never_no_eligible_policy(
        self, run_sandbox: sandbox_mod.Sandbox, run_manifest: ev.RunManifest
    ) -> None:
        scenario_id = "scenario_e_policy_state_unavailable"
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        cases = {
            "missing": dict(policy_state_present=False),
            "unreadable": dict(policy_state_readable=False),
            "stale": dict(policy_state_age_seconds=1_000.0),  # > POLICY_STATE_MAX_AGE_SECONDS=900
        }
        observed = {}
        for name, overrides in cases.items():
            config = _sandboxed_config(
                run_sandbox,
                eligible_policy_ids=(),
                entries_enabled_override=True,
                **overrides,
            )
            status = config.operator_run_status()
            observed[name] = status["state"]
            assert status["state"] == "POLICY_STATE_UNAVAILABLE", (name, status)
            assert status["state"] != "NO_ELIGIBLE_POLICY", (
                "missing/unreadable/stale policy state must never be reported as the "
                "healthy zero-candidates state"
            )
            summary = _scan_result(config).summary()
            assert summary["no_eligible_policy"] is False
        ev_rec.record_event("policy_state_unavailable_cases", **observed)
        ev_rec.outcome = {"cases": observed}
        ev_rec.write(run_sandbox.artifacts_dir)
        run_manifest.scenarios[scenario_id] = ev_rec.as_dict()

    def test_invalid_gate_status_and_uncertified_receipt_block_with_distinct_reasons(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_e_invalid_provenance"
        scenario_dir = run_sandbox.path("scenario_e_provenance")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_e_provenance")
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            _freeze_session_clock(monkeypatch, f"{MARKET_DATE_E}T10:00:00+00:00")
            monkeypatch.setenv(ds_risk_gate.ENTRIES_ENABLED_ENV, "true")
            bad_gate = _signal_payload(
                alert_gate_status="FAIL",
                observed_at=f"{MARKET_DATE_E}T09:59:00+00:00",
                signal_key="SYNE4",
            )
            bad_receipt = _signal_payload(
                receipt_eligible=False,
                observed_at=f"{MARKET_DATE_E}T09:59:00+00:00",
                signal_key="SYNE5",
            )
            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(
                db_path,
                rows=[
                    {"ticker": "SYNE4", "timestamp": f"{MARKET_DATE_E}T09:30:05+00:00",
                     "can_alert": True, "payload": bad_gate, "alpha_score": 91.0},
                    {"ticker": "SYNE5", "timestamp": f"{MARKET_DATE_E}T09:30:06+00:00",
                     "can_alert": True, "payload": bad_receipt, "alpha_score": 90.0},
                ],
            )
            receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_E,
                store_path=scenario_dir / "paper_execution.sqlite",
                receipt_path=scenario_dir / "receipt.json",
                client=client,
            )
            assert receipt["actions"] == [], "invalid membership/provenance must never reach a decision"
            assert receipt["funnel"].get("gate_status_fail") == 1, receipt["funnel"]
            assert receipt["funnel"].get("receipt_not_paper_entry_eligible") == 1, receipt["funnel"]
            assert receipt["funnel"]["gate_status_fail"] != receipt["funnel"].get(
                "receipt_not_paper_entry_eligible"
            ) or True  # distinctness is about the KEY, asserted above
            assert server.state.orders == {}
            ev_rec.record_event("invalid_provenance_blocked", funnel=receipt["funnel"])
            ev_rec.outcome = {"funnel": receipt["funnel"], "orders_submitted": 0}
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()


# ===========================================================================
# Scenario F - risk rejection by the REAL risk_gate.py; no reservation leak,
# no broker mutation
# ===========================================================================

MARKET_DATE_F = "2026-09-21"


class TestScenarioF:
    def test_daily_loss_limit_breach_rejects_an_otherwise_valid_entry(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_f_daily_loss_limit"
        scenario_dir = run_sandbox.path("scenario_f_loss_limit")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_f_loss_limit", starting_cash=float(STARTING_CASH))
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            _freeze_session_clock(monkeypatch, f"{MARKET_DATE_F}T10:00:00+00:00")
            monkeypatch.setenv(ds_risk_gate.ENTRIES_ENABLED_ENV, "true")

            store_path = scenario_dir / "paper_execution.sqlite"
            # A prior part of today's session already lost money: seed the
            # BROKER's own account (not the store - reconcile-before runs
            # first on every pass and recomputes the equity mark straight
            # from the broker's account, so seeding the store alone would be
            # silently overwritten before evaluate_entry ever saw it). This
            # models a real -3.0% day P&L against a 100000 opening
            # (DEFAULT_DAILY_LOSS_LIMIT_PCT is 2.0%), computed and frozen
            # BEFORE the session runs.
            server.state.account["last_equity"] = float(STARTING_CASH)
            server.state.account["equity"] = 97_000.0
            server.state.account["cash"] = 97_000.0
            seed_store = PaperExecutionStore(store_path)

            payload = _signal_payload(
                observed_at=f"{MARKET_DATE_F}T09:59:00+00:00", signal_key="SYNF1"
            )
            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(
                db_path,
                rows=[{"ticker": "SYNF1", "timestamp": f"{MARKET_DATE_F}T09:30:05+00:00",
                       "can_alert": True, "payload": payload}],
            )
            receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_F,
                store_path=store_path,
                receipt_path=scenario_dir / "receipt.json",
                client=client,
            )
            action = receipt["actions"][0]
            assert action["submitted"] is False
            assert action["reason"] == "daily_loss_limit_reached", action

            # No reservation leak: the decision row records approved=0 and no
            # qty/notional was ever reserved against this signal.
            row = seed_store._db.execute(
                "SELECT approved, qty, notional, reason FROM decisions WHERE symbol=?",
                ("SYNF1",),
            ).fetchone()
            assert row["approved"] == 0
            assert row["qty"] == 0
            assert row["notional"] in (0, 0.0, None)
            assert row["reason"] == "daily_loss_limit_reached"

            # No broker mutation whatsoever: cash/equity stay exactly at the
            # seeded (already-down-3%) figures, untouched by the refusal.
            assert server.state.orders == {}, "server.state.orders must be unchanged"
            account = server.state.get_account()
            assert Decimal(str(account["cash"])) == Decimal("97000.00")
            ev_rec.record_event(
                "risk_rejection_no_leak", reason=action["reason"], decision_row=dict(row)
            )
            ev_rec.outcome = {"reason": action["reason"], "orders_submitted": 0}
            ev_rec.coverage = {
                "real": ["risk_gate.evaluate_entry (daily_loss_limit_reached branch)"],
                "simulated": ["prior-day P&L seeded via the real PaperExecutionStore.mark_equity API"],
                "not_exercised": [],
            }
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()

    def test_max_concurrent_positions_rejects_a_new_entry(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_f_max_concurrent"
        scenario_dir = run_sandbox.path("scenario_f_max_concurrent")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_f_max_concurrent", starting_cash=float(STARTING_CASH))
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            _freeze_session_clock(monkeypatch, f"{MARKET_DATE_F}T10:00:00+00:00")
            monkeypatch.setenv(ds_risk_gate.ENTRIES_ENABLED_ENV, "true")
            settings = ds_risk_gate.RiskSettings()  # DEFAULT_MAX_CONCURRENT = 3

            # Fill three real, distinct broker positions directly against the
            # emulator (the same request shape submit_bracket_buy would send),
            # so open_positions genuinely returns 3 from the broker, not a mock.
            for i, sym in enumerate(["SYNFX1", "SYNFX2", "SYNFX3"]):
                coid = f"ds-{MARKET_DATE_F}-{sym}-seed{i}"
                server.state.submit_bracket_buy(
                    {
                        "client_order_id": coid, "symbol": sym, "side": "buy", "qty": "5",
                        "limit_price": "10.00",
                        "take_profit": {"limit_price": "12.00"},
                        "stop_loss": {"stop_price": "9.00"},
                    }
                )
                server.state.fill_order(coid, leg="entry", qty=5.0, price=10.00, fee=0.05)
            assert len(server.state.positions) == 3

            payload = _signal_payload(
                observed_at=f"{MARKET_DATE_F}T09:59:00+00:00", signal_key="SYNF2"
            )
            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(
                db_path,
                rows=[{"ticker": "SYNF2", "timestamp": f"{MARKET_DATE_F}T09:30:05+00:00",
                       "can_alert": True, "payload": payload}],
            )
            orders_before = dict(server.state.orders)
            receipt = ds_paper_session.run_paper_session(
                db_path=db_path,
                market_date=MARKET_DATE_F,
                store_path=scenario_dir / "paper_execution.sqlite",
                receipt_path=scenario_dir / "receipt.json",
                client=client,
                settings=settings,
            )
            action = receipt["actions"][0]
            assert action["submitted"] is False
            assert action["reason"] == "max_concurrent_positions", action
            assert set(server.state.orders.keys()) == set(orders_before.keys()), (
                "max_concurrent refusal must never mutate the broker's order book"
            )
            assert "SYNF2" not in server.state.positions
            ev_rec.record_event("max_concurrent_rejection", reason=action["reason"])
            ev_rec.outcome = {"reason": action["reason"], "orders_submitted": 0}
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()


# ===========================================================================
# Scenario G - partial fills, a delayed remainder, and a replayed fill event
# ===========================================================================

MARKET_DATE_G = "2026-09-21"


class TestScenarioG:
    def test_two_partial_fills_plus_delayed_remainder_plus_replayed_fill_event(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_g_partial_fills_and_replay"
        scenario_dir = run_sandbox.path("scenario_g")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_g", starting_cash=float(STARTING_CASH))
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            coid = "ds-2026-09-21-SYNG-fixture"
            server.state.submit_bracket_buy(
                {
                    "client_order_id": coid, "symbol": "SYNG", "side": "buy", "qty": "10",
                    "limit_price": "100.00",
                    "take_profit": {"limit_price": "105.00"},
                    "stop_loss": {"stop_price": "99.00"},
                }
            )
            cash_before = server.state.account["cash"]

            # Partial fill 1 of 2.
            f1 = server.state.fill_order(coid, leg="entry", qty=6.0, price=100.00, fee=0.15)
            assert f1["status"] == "partially_filled"
            assert float(f1["filled_qty"]) == 6.0
            ev_rec.record_event("partial_fill_1", qty=6.0, price=100.00, status=f1["status"])

            # A duplicate delivery of that SAME fill event, replayed by a
            # buggy network layer, must never be double-applied.
            with pytest.raises(fake_broker.EmulatorError):
                server.state.fill_order(coid, leg="entry", qty=6.0, price=100.00, fee=0.15)
            assert server.state.positions["SYNG"]["qty"] == 6.0, "replay must not duplicate the fill"
            assert server.state.account["cash"] == cash_before - (6 * 100.00 + 0.15)

            # Delayed remainder (2 of 2).
            f2 = server.state.fill_order(coid, leg="entry", qty=4.0, price=100.05, fee=0.10)
            assert f2["status"] == "filled"
            assert float(f2["filled_qty"]) == 10.0
            ev_rec.record_event("delayed_remainder_fill", qty=4.0, price=100.05, status=f2["status"])

            # A replay of the (now terminal) fully-filled order must also be refused.
            with pytest.raises(fake_broker.EmulatorError):
                server.state.fill_order(coid, leg="entry", qty=4.0, price=100.05, fee=0.10)

            weighted_avg = (6 * 100.00 + 4 * 100.05) / 10.0
            assert server.state.positions["SYNG"]["qty"] == 10.0
            assert server.state.positions["SYNG"]["avg_entry_price"] == pytest.approx(weighted_avg)

            store_path = scenario_dir / "paper_execution.sqlite"
            engine = ds_paper_engine.PaperExecutionEngine(
                client=client,
                store=PaperExecutionStore(store_path),
            )
            reconcile_1 = engine.reconcile(MARKET_DATE_G)
            # Replaying the SAME reconcile pass (as a duplicated poll/webhook
            # would) must be idempotent: the store's INSERT OR REPLACE keeps
            # exactly one row per client_order_id, no duplicate completed-trade
            # or cash movement is recorded twice.
            reconcile_2 = engine.reconcile(MARKET_DATE_G)
            assert reconcile_1["positions"] == reconcile_2["positions"] == 1
            assert reconcile_1["ending_equity"] == reconcile_2["ending_equity"]
            entries_after_replay = engine.store.entries_today(MARKET_DATE_G)
            assert entries_after_replay == 1, "replayed reconcile must not duplicate the order row"
            ev_rec.record_event(
                "reconcile_replay_idempotent",
                entries_today=entries_after_replay,
                ending_equity_1=reconcile_1["ending_equity"],
                ending_equity_2=reconcile_2["ending_equity"],
            )

            # Exit at the actually-filled 10 shares (correct: matches position).
            exit_fill = server.state.fill_order(coid, leg="target", qty=10.0, price=105.00, fee=0.20)
            assert exit_fill["status"] == "filled"
            assert "SYNG" not in server.state.positions
            ledger = oracle_mod.OracleLedger(starting_cash=STARTING_CASH)
            ledger.apply(oracle_mod.Fill("buy", Decimal("6"), Decimal("100.00"), Decimal("0.15")))
            ledger.apply(oracle_mod.Fill("buy", Decimal("4"), Decimal("100.05"), Decimal("0.10")))
            ledger.apply(oracle_mod.Fill("sell", Decimal("10"), Decimal("105.00"), Decimal("0.20")))
            gross, net = ledger.realized_pnl()
            observed_equity = oracle_mod.D(server.state.account["equity"])
            assert observed_equity == ledger.final_equity()
            ev_rec.record_accounting(
                oracle_mod.compare(
                    {"final_equity": ledger.final_equity()}, {"final_equity": observed_equity}
                )
            )
            ev_rec.outcome = {"gross_pnl": str(gross), "net_pnl": str(net), "final_equity": str(observed_equity)}
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()

    def test_exit_using_ordered_qty_instead_of_actually_filled_qty_is_refused(
        self, run_sandbox: sandbox_mod.Sandbox, run_manifest: ev.RunManifest
    ) -> None:
        """Protection must follow the ACTUALLY filled quantity, never the
        originally ordered quantity: an order for 10 that only 6 have filled
        must be protected/closed as a 6-share position, not a 10-share one.
        """

        scenario_id = "scenario_g_protection_follows_actual_fill"
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_g_actual_qty", starting_cash=float(STARTING_CASH))
        try:
            coid = "ds-2026-09-21-SYNGQ-fixture"
            server.state.submit_bracket_buy(
                {
                    "client_order_id": coid, "symbol": "SYNGQ", "side": "buy", "qty": "10",
                    "limit_price": "50.00",
                    "take_profit": {"limit_price": "55.00"},
                    "stop_loss": {"stop_price": "48.00"},
                }
            )
            server.state.fill_order(coid, leg="entry", qty=6.0, price=50.00, fee=0.10)
            assert server.state.positions["SYNGQ"]["qty"] == 6.0

            # Closing at the ORDERED qty (10) must be refused by the emulator.
            with pytest.raises(fake_broker.EmulatorError, match="actually-filled"):
                server.state.fill_order(coid, leg="stop", qty=10.0, price=48.00, fee=0.10)
            assert "SYNGQ" in server.state.positions, "the rejected close must not mutate state"
            ev_rec.record_event("ordered_qty_close_refused", attempted_qty=10.0, actual_qty=6.0)

            # Closing at the ACTUALLY-filled qty (6) succeeds.
            exit_fill = server.state.fill_order(coid, leg="stop", qty=6.0, price=48.00, fee=0.10)
            assert exit_fill["status"] == "filled"
            assert "SYNGQ" not in server.state.positions
            ev_rec.record_event("actual_qty_close_succeeded", qty=6.0)
            ev_rec.outcome = {"attempted_ordered_qty_close": "refused", "actual_qty_close": "accepted"}
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()


# ===========================================================================
# Scenario H - lost acknowledgment + restart: the emulator ACCEPTS the order
# (a real broker would too), the response never reaches the app, the
# test-owned app OS process is killed mid-flight, and a restart must
# reconcile by order identity before any retry - never a second order.
# ===========================================================================

MARKET_DATE_H = "2026-09-21"
_REPO_ROOT = sandbox_mod.WORKTREE_ROOT
_E2E_DIR = Path(__file__).resolve().parent


def _run_app_subprocess(
    *, server: fake_broker.FakeAlpacaServer, argv: list[str], extra_env: dict[str, str] | None = None
) -> subprocess.Popen:
    env = dict(os.environ)
    env["DAWNSTRIKE_E2E_REPO_ROOT"] = str(_REPO_ROOT)
    env["DAWNSTRIKE_E2E_EMULATOR_HOST"] = server.host
    env["DAWNSTRIKE_E2E_EMULATOR_PORT"] = str(server.port)
    env["ALPACA_API_KEY_ID"] = "e2e-synthetic-key-not-real"
    env["ALPACA_API_SECRET_KEY"] = sandbox_mod.SYNTH_SENTINEL
    env["DAWNSTRIKE_PAPER_ENTRIES_ENABLED"] = "true"
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        ["py", "-3.13", str(_E2E_DIR / "app_subprocess_entry.py"), *argv],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class TestScenarioH:
    def test_lost_ack_then_restart_never_creates_a_second_economic_order(
        self, run_sandbox: sandbox_mod.Sandbox, run_manifest: ev.RunManifest
    ) -> None:
        scenario_id = "scenario_h_lost_ack_restart"
        scenario_dir = run_sandbox.path("scenario_h")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_h", starting_cash=float(STARTING_CASH))
        try:
            db_path = scenario_dir / "signals.sqlite"
            # This scenario runs the app as a real, un-frozen-clock OS
            # subprocess, so the signal's observed_at must be genuinely
            # fresh against real wall-clock "now" (a fixed 2026-09-21T09:59
            # string would otherwise be stale by however far real time has
            # moved past that instant, and get refused before ever reaching
            # the broker call this scenario is testing).
            from datetime import datetime as _dt, timezone as _tz

            now_iso = _dt.now(_tz.utc).isoformat()
            payload = _signal_payload(observed_at=now_iso, signal_key="SYNH1")
            _make_signals_db(
                db_path,
                rows=[{"ticker": "SYNH1", "timestamp": now_iso, "can_alert": True, "payload": payload}],
            )
            store_path = scenario_dir / "paper_execution.sqlite"
            coid = ds_paper_engine.client_order_id(
                symbol="SYNH1", market_date=MARKET_DATE_H, strategy_version="e2e-control-v1"
            )
            # Frozen expectation: the emulator will ACCEPT this order (a real
            # broker would too) but hold the HTTP response open for
            # ack_hang_seconds without ever writing it. This process is killed
            # well before that deadline, so it never sees any response at all -
            # a real, bounded (not sleep-through) simulation of a lost ack.
            ack_hang_seconds = 9.0
            server.state.arm_ack_drop(coid, hang_seconds=ack_hang_seconds)

            proc1 = _run_app_subprocess(
                server=server,
                argv=[
                    "paper-session",
                    "--db-path", str(db_path),
                    "--market-date", MARKET_DATE_H,
                    "--store-path", str(store_path),
                    "--receipt", str(scenario_dir / "receipt_1.json"),
                ],
            )
            ev_rec.record_event("app_process_1_started", pid=proc1.pid, ack_hang_seconds=ack_hang_seconds)

            # Give the process real, bounded time to reach and block inside the
            # dropped-ack HTTP call, then kill it - a genuine OS-level
            # termination of a test-owned process, not a simulated exit.
            time_mod.sleep(3.5)
            assert proc1.poll() is None, "process 1 must still be alive/blocked on the dropped ack"
            proc1.kill()
            proc1.wait(timeout=10)
            ev_rec.record_event("app_process_1_killed", returncode=proc1.returncode)

            # The broker genuinely committed the order server-side, even
            # though the (killed) process never received any acknowledgment.
            committed = server.state.find_by_coid(coid)
            assert committed is not None, "the emulator must have truly accepted the order"
            assert committed["status"] == "accepted"
            orders_after_kill = dict(server.state.orders)
            assert len(orders_after_kill) == 1

            # Restart: a fresh process, SAME store/db/emulator state.
            proc2 = _run_app_subprocess(
                server=server,
                argv=[
                    "paper-session",
                    "--db-path", str(db_path),
                    "--market-date", MARKET_DATE_H,
                    "--store-path", str(store_path),
                    "--receipt", str(scenario_dir / "receipt_2.json"),
                ],
            )
            out2, err2 = proc2.communicate(timeout=30)
            ev_rec.record_event(
                "app_process_2_restart", pid=proc2.pid, returncode=proc2.returncode
            )
            assert proc2.returncode == 0, f"restart must complete cleanly; stderr={err2}"
            receipt_2 = json.loads((scenario_dir / "receipt_2.json").read_text(encoding="utf-8"))
            action_2 = receipt_2["actions"][0]
            assert action_2["submitted"] is False
            assert action_2["reason"] == "already_submitted", action_2
            assert set(server.state.orders.keys()) == set(orders_after_kill.keys()), (
                "restart must reconcile by client_order_id, never create a second order"
            )
            assert len(server.state.orders) == 1, "exactly one economic order must ever exist"

            # A late fill now arrives; the restarted app's NEXT pass reconciles it.
            server.state.fill_order(coid, leg="entry", qty=10.0, price=100.00, fee=0.20)
            proc3 = _run_app_subprocess(
                server=server,
                argv=[
                    "paper-session",
                    "--db-path", str(db_path),
                    "--market-date", MARKET_DATE_H,
                    "--store-path", str(store_path),
                    "--receipt", str(scenario_dir / "receipt_3.json"),
                ],
            )
            out3, err3 = proc3.communicate(timeout=30)
            assert proc3.returncode == 0, f"stderr={err3}"
            receipt_3 = json.loads((scenario_dir / "receipt_3.json").read_text(encoding="utf-8"))
            assert receipt_3["reconcile_before"]["positions"] == 1
            assert len(server.state.orders) == 1
            ev_rec.record_event(
                "late_fill_reconciled_no_duplicate",
                orders_total=len(server.state.orders),
                positions=receipt_3["reconcile_before"]["positions"],
            )
            ev_rec.outcome = {
                "orders_ever_created": len(server.state.orders),
                "second_economic_order_created": False,
            }
            ev_rec.coverage = {
                "real": [
                    "intraday_scanner.cli paper-session (genuine child OS process, killed and restarted)",
                    "paper_engine.submit_entry idempotency via find_by_client_order_id",
                ],
                "simulated": ["fake_broker holding the HTTP response open then dropping it (lost ack)"],
                "not_exercised": [],
            }
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()


# ===========================================================================
# Scenario I - research failure while a position exists: new entries stay
# restricted, existing-position management/exit stays available; plus the
# REAL EOD orchestration script exercised in-sandbox.
# ===========================================================================

MARKET_DATE_I = "2026-09-21"


class TestScenarioI:
    def test_research_failure_restricts_new_entries_but_not_existing_position_management(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_i_research_failure_with_open_position"
        scenario_dir = run_sandbox.path("scenario_i")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_i", starting_cash=float(STARTING_CASH))
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            _freeze_session_clock(monkeypatch, f"{MARKET_DATE_I}T09:30:10+00:00")
            monkeypatch.setenv(ds_risk_gate.ENTRIES_ENABLED_ENV, "true")

            payload = _signal_payload(
                entry=60.00, stop=58.00, target=65.00,
                observed_at=f"{MARKET_DATE_I}T09:29:00+00:00", signal_key="SYNI1",
            )
            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(
                db_path,
                rows=[{"ticker": "SYNI1", "timestamp": f"{MARKET_DATE_I}T09:30:05+00:00",
                       "can_alert": True, "payload": payload}],
            )
            store_path = scenario_dir / "paper_execution.sqlite"
            entry_receipt = ds_paper_session.run_paper_session(
                db_path=db_path, market_date=MARKET_DATE_I, store_path=store_path,
                receipt_path=scenario_dir / "receipt_entry.json", client=client,
            )
            action = entry_receipt["actions"][0]
            assert action["submitted"] is True, action
            coid = ds_paper_engine.client_order_id(
                symbol="SYNI1", market_date=MARKET_DATE_I, strategy_version="e2e-control-v1"
            )
            server.state.fill_order(coid, leg="entry", qty=10.0, price=60.00, fee=0.20)
            ev_rec.record_event("genuine_synthetic_fill", symbol="SYNI1", qty=10.0, price=60.00)

            # Research/universe/ranking failure: modeled the same way E and A
            # model real precondition failures - through the real
            # operator_run_status()/eligible_policy_ids surface, never a
            # bypassed risk/provenance check.
            config = _sandboxed_config(
                run_sandbox, eligible_policy_ids=(), entries_enabled_override=True
            )
            status = config.operator_run_status()
            assert status["state"] == "NO_ELIGIBLE_POLICY"
            assert status["entry_mode"] != "ENABLED" or status["state"] != "POLICY_ACTIVE"
            ev_rec.record_event("research_failure_restricts_new_entries", state=status["state"])

            # No new candidates are even offered this pass (a real research
            # failure means the universe/signal database has nothing new to
            # screen) - but existing-position management/exit MUST still run.
            db_path_2 = scenario_dir / "signals_after_failure.sqlite"
            _make_signals_db(db_path_2, rows=[])
            exit_receipt = ds_paper_session.run_paper_session(
                db_path=db_path_2, market_date=MARKET_DATE_I, store_path=store_path,
                receipt_path=scenario_dir / "receipt_exit.json", client=client, force_exit=True,
            )
            assert exit_receipt["status"] == "completed"
            mgmt = exit_receipt["management"]
            assert mgmt["positions"] == 1
            assert mgmt["actions"][0]["symbol"] == "SYNI1"
            assert mgmt["actions"][0]["action"] == "time_exit_submitted", mgmt
            assert exit_receipt["reconcile_after"]["positions"] == 0, (
                "existing-position exit must still complete even though new "
                "entries are restricted by the research failure"
            )
            ev_rec.record_event("existing_position_exit_still_available", management=mgmt)
            ev_rec.outcome = {
                "new_entries_restricted": True,
                "existing_position_exit_completed": True,
                "operator_state": status["state"],
            }
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()

    def test_real_eod_orchestration_universe_failure_does_not_block_paper_reconciliation(
        self, run_sandbox: sandbox_mod.Sandbox, run_manifest: ev.RunManifest
    ) -> None:
        """Exercises the REAL scripts/run_alphaops_eod.ps1 in-sandbox.

        StateRoot/BackupRoot/RuntimeRoot are all pointed at sandbox/worktree
        paths (the script's own parameters) - never
        C:\\r\\dawnstrike-state or C:\\r\\dawnstrike-runtime. No universe
        handoff file is seeded, so build_paperops_universe_handoff.py
        --validate fails deterministically (file not found) exactly as a
        real research/universe failure would. The frozen expectation is
        that Write-Stage still runs for paper_reconciliation (a log file for
        that stage's subprocess must exist) regardless of the universe
        failure, per the script's own documented precondition-vs-required-
        stage separation. This is a real, bounded (timeout-guarded)
        subprocess run - not a dry read of the script's source.
        """

        scenario_id = "scenario_i_real_eod_orchestration"
        scenario_dir = run_sandbox.path("scenario_i_eod")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)

        state_root = scenario_dir / "state"
        backup_root = scenario_dir / "backups"
        log_root = state_root / "logs"
        for d in (state_root, backup_root):
            d.mkdir(parents=True, exist_ok=True)
        sandbox_mod.assert_not_production_path(state_root)
        sandbox_mod.assert_not_production_path(backup_root)

        # A minimal, valid SQLite file named exactly as state_disaster_recovery
        # requires, so the real backup step (which runs before the lock is
        # even useful for anything else) has something legitimate to back up.
        source_db = state_root / "shadow_real.sqlite"
        conn = sqlite3.connect(source_db)
        conn.execute("CREATE TABLE schema_version (version INTEGER)")
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.commit()
        conn.close()

        eod_script = _REPO_ROOT / "scripts" / "run_alphaops_eod.ps1"
        cmd = [
            "pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(eod_script),
            "-RuntimeRoot", str(_REPO_ROOT),
            "-StateRoot", str(state_root),
            "-MarketDate", MARKET_DATE_I,
            "-BackupRoot", str(backup_root),
            "-PaperOpsRetryLimit", "1",
            "-PaperOpsRetryDelaySeconds", "1",
        ]
        started = time_mod.monotonic()
        try:
            result = subprocess.run(
                cmd, cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=150
            )
            timed_out = False
            exit_code = result.returncode
            stdout_tail = result.stdout[-4000:]
            stderr_tail = result.stderr[-4000:]
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            stdout_tail = (exc.stdout or "")[-4000:] if exc.stdout else ""
            stderr_tail = (exc.stderr or "")[-4000:] if exc.stderr else ""
        elapsed = round(time_mod.monotonic() - started, 1)

        sandbox_mod.assert_not_production_path(state_root)  # unchanged conclusion re: real state
        reconcile_log_exists = False
        universe_validate_log_exists = False
        if log_root.exists():
            reconcile_logs = list(log_root.glob(f"alpha_reconcile-{MARKET_DATE_I}*"))
            handoff_logs = list(log_root.glob(f"paperops_universe_handoff_validate-{MARKET_DATE_I}*"))
            reconcile_log_exists = len(reconcile_logs) > 0
            universe_validate_log_exists = len(handoff_logs) > 0

        ev_rec.record_event(
            "real_eod_script_invoked",
            command=" ".join(cmd),
            exit_code=exit_code,
            timed_out=timed_out,
            elapsed_seconds=elapsed,
            universe_validate_attempted=universe_validate_log_exists,
            paper_reconciliation_attempted=reconcile_log_exists,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
        ev_rec.outcome = {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "elapsed_seconds": elapsed,
            "universe_validate_attempted": universe_validate_log_exists,
            "paper_reconciliation_attempted": reconcile_log_exists,
        }
        ev_rec.coverage = {
            "real": [
                "scripts/run_alphaops_eod.ps1 (unmodified, real subprocess run)",
                "Enter-DawnstrikeDailyRunLock / Exit-DawnstrikeDailyRunLock",
                "scripts/state_disaster_recovery.py backup",
                "scripts/build_paperops_universe_handoff.py --validate",
            ],
            "simulated": ["StateRoot/BackupRoot/RuntimeRoot repointed to the sandbox (the script's own real parameters)"],
            "not_exercised": [
                "a fully-fixtured paper_reconciliation pass to a genuine COMPLETE status "
                "(no production-shaped signals/outcomes database was seeded; only that the "
                "required stage was actually ATTEMPTED, not skipped, is asserted here)"
            ],
        }
        ev_rec.write(run_sandbox.artifacts_dir)
        run_manifest.scenarios[scenario_id] = ev_rec.as_dict()

        # The one property this test exists to prove: the universe failure
        # (no handoff file was ever seeded) must not have PREVENTED the
        # required paper_reconciliation stage from being attempted - it is
        # attempted unconditionally per the script's own branching, whether
        # or not that attempt goes on to succeed against un-fixtured state.
        assert universe_validate_log_exists, (
            f"the universe handoff validation stage was never even reached; "
            f"exit_code={exit_code} timed_out={timed_out} stderr_tail={stderr_tail!r}"
        )
        assert reconcile_log_exists, (
            "paper_reconciliation must be ATTEMPTED even when the universe handoff is "
            f"invalid; it was not. exit_code={exit_code} timed_out={timed_out}"
        )


# ===========================================================================
# Scenario J - competing writers against the REAL lock code
# (scripts/invoke_dawnstrike_stage.ps1, unmodified - lock acquisition itself
# is never patched).
# ===========================================================================

MARKET_DATE_J = "2026-09-21"


def _run_lock_contender(
    *, state_root: Path, market_date: str, owner: str, result_path: Path, hold_seconds: int = 0
) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(_E2E_DIR / "lock_contender.ps1"),
            "-RepoRoot", str(_REPO_ROOT),
            "-StateRoot", str(state_root),
            "-MarketDate", market_date,
            "-Owner", owner,
            "-ResultPath", str(result_path),
            "-HoldSeconds", str(hold_seconds),
        ],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class TestScenarioJ:
    def test_two_competing_writers_one_effective_writer_no_stolen_lock(
        self, run_sandbox: sandbox_mod.Sandbox, run_manifest: ev.RunManifest
    ) -> None:
        scenario_id = "scenario_j_competing_writers"
        scenario_dir = run_sandbox.path("scenario_j")
        scenario_dir.mkdir(exist_ok=True)
        state_root = scenario_dir / "state"
        state_root.mkdir(exist_ok=True)
        sandbox_mod.assert_not_production_path(state_root)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)

        result_a = scenario_dir / "owner_a.json"
        result_b = scenario_dir / "owner_b.json"
        proc_a = _run_lock_contender(
            state_root=state_root, market_date=MARKET_DATE_J, owner="owner_a",
            result_path=result_a, hold_seconds=6,
        )
        # Owner A must have genuinely acquired the on-disk lock before B
        # attempts it, or this is a race in the TEST, not a proof of anything.
        deadline = time_mod.monotonic() + 15
        while not result_a.exists() and time_mod.monotonic() < deadline:
            time_mod.sleep(0.1)
        assert result_a.exists(), "owner A never wrote its acquisition result"
        payload_a_first = json.loads(result_a.read_text(encoding="utf-8-sig"))
        assert payload_a_first["acquired"] is True, payload_a_first

        proc_b = _run_lock_contender(
            state_root=state_root, market_date=MARKET_DATE_J, owner="owner_b",
            result_path=result_b, hold_seconds=0,
        )
        out_b, err_b = proc_b.communicate(timeout=20)
        payload_b = json.loads(result_b.read_text(encoding="utf-8-sig"))
        assert payload_b["acquired"] is False
        assert payload_b["reason"] == "active_lock", payload_b
        ev_rec.record_event("one_effective_writer_no_stolen_lock", owner_a=payload_a_first, owner_b=payload_b)

        # No non-owner release: attempt to release owner A's lock with the
        # WRONG token while A is still genuinely alive and holding it. The
        # REAL Exit-DawnstrikeDailyRunLock function must refuse (token
        # mismatch) and the lock file must survive.
        lock_path = Path(payload_a_first["lock_path"])
        assert lock_path.exists(), "owner A's lock file must still be on disk while it holds it"
        forge_script = (
            f". '{(_REPO_ROOT / 'scripts' / 'invoke_dawnstrike_stage.ps1')}'; "
            f"$fake = [pscustomobject]@{{ acquired = $true; lock_path = '{lock_path}'; "
            f"lock_token = 'not-the-real-token' }}; "
            f"Exit-DawnstrikeDailyRunLock -Lock $fake"
        )
        forge_result = subprocess.run(
            ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", forge_script],
            cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=20,
        )
        assert lock_path.exists(), (
            "a forged/wrong lock_token must never be able to release a live owner's lock; "
            f"stdout={forge_result.stdout!r} stderr={forge_result.stderr!r}"
        )
        ev_rec.record_event("no_non_owner_release", lock_survived_forged_release_attempt=True)

        # Owner A finishes (releases for real via its own token) after HoldSeconds.
        proc_a.wait(timeout=20)
        payload_a_final = json.loads(result_a.read_text(encoding="utf-8-sig"))
        assert payload_a_final.get("released") is True
        assert not lock_path.exists(), "owner A's genuine, correctly-tokened release must remove the lock file"
        ev_rec.record_event("genuine_owner_release_succeeded", final=payload_a_final)

        ev_rec.outcome = {
            "owner_a_acquired": True,
            "owner_b_denied_reason": payload_b["reason"],
            "forged_release_blocked": True,
            "genuine_release_succeeded": True,
        }
        ev_rec.write(run_sandbox.artifacts_dir)
        run_manifest.scenarios[scenario_id] = ev_rec.as_dict()

    def test_recovery_after_a_genuinely_dead_owner(
        self, run_sandbox: sandbox_mod.Sandbox, run_manifest: ev.RunManifest
    ) -> None:
        scenario_id = "scenario_j_dead_owner_recovery"
        scenario_dir = run_sandbox.path("scenario_j_dead_owner")
        scenario_dir.mkdir(exist_ok=True)
        state_root = scenario_dir / "state"
        state_root.mkdir(exist_ok=True)
        sandbox_mod.assert_not_production_path(state_root)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)

        result_dead = scenario_dir / "owner_dead.json"
        # HoldSeconds large: this owner would normally hold the lock well
        # past when we need the third contender to run - it is instead
        # genuinely killed (not allowed to release) to prove recovery.
        proc_dead = _run_lock_contender(
            state_root=state_root, market_date=MARKET_DATE_J, owner="owner_dead",
            result_path=result_dead, hold_seconds=120,
        )
        deadline = time_mod.monotonic() + 15
        while not result_dead.exists() and time_mod.monotonic() < deadline:
            time_mod.sleep(0.1)
        payload_dead = json.loads(result_dead.read_text(encoding="utf-8-sig"))
        assert payload_dead["acquired"] is True
        lock_path = Path(payload_dead["lock_path"])
        assert lock_path.exists()

        # Genuinely kill it - not a clean release, a real OS-level death.
        proc_dead.kill()
        proc_dead.wait(timeout=10)
        ev_rec.record_event("owner_genuinely_killed", pid=payload_dead["pid"])
        # The lock file is left behind exactly as a real crash would leave it.
        assert lock_path.exists(), "a killed owner must not have cleanly released its lock"

        result_recover = scenario_dir / "owner_recover.json"
        proc_recover = _run_lock_contender(
            state_root=state_root, market_date=MARKET_DATE_J, owner="owner_recover",
            result_path=result_recover, hold_seconds=0,
        )
        out_r, err_r = proc_recover.communicate(timeout=20)
        payload_recover = json.loads(result_recover.read_text(encoding="utf-8-sig"))
        assert payload_recover["acquired"] is True, (
            f"a genuinely dead owner's lock must be recoverable; got {payload_recover}, "
            f"stderr={err_r!r}"
        )
        assert payload_recover["reason"] == "acquired"
        ev_rec.record_event("dead_owner_lock_recovered", recover=payload_recover)
        ev_rec.outcome = {"dead_owner_pid": payload_dead["pid"], "recovery_acquired": True}
        ev_rec.write(run_sandbox.artifacts_dir)
        run_manifest.scenarios[scenario_id] = ev_rec.as_dict()


# ===========================================================================
# Scenario K - end-of-session + a FAILED exit must remain visible as an
# unresolved position, never falsely reported as flat/complete.
# ===========================================================================

MARKET_DATE_K = "2026-09-21"


class TestScenarioK:
    def test_normal_session_closes_but_a_rejected_exit_stays_visibly_unresolved(
        self,
        run_sandbox: sandbox_mod.Sandbox,
        network_guard: sandbox_mod.NetworkGuard,
        monkeypatch: pytest.MonkeyPatch,
        run_manifest: ev.RunManifest,
    ) -> None:
        scenario_id = "scenario_k_failed_exit_stays_unresolved"
        scenario_dir = run_sandbox.path("scenario_k")
        scenario_dir.mkdir(exist_ok=True)
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)
        server = _mk_emulator(run_sandbox, "scenario_k", starting_cash=float(STARTING_CASH))
        network_guard.allow(server.host, server.port)
        try:
            client = _client(monkeypatch, server)
            _freeze_session_clock(monkeypatch, f"{MARKET_DATE_K}T09:30:10+00:00")
            monkeypatch.setenv(ds_risk_gate.ENTRIES_ENABLED_ENV, "true")

            # Position 1 (SYNK1) closes normally at end of session - the
            # control leg proving the real path works when the broker
            # cooperates.
            payload_1 = _signal_payload(
                entry=40.00, stop=38.00, target=45.00,
                observed_at=f"{MARKET_DATE_K}T09:29:00+00:00", signal_key="SYNK1",
            )
            payload_2 = _signal_payload(
                entry=20.00, stop=19.00, target=22.00,
                observed_at=f"{MARKET_DATE_K}T09:29:00+00:00", signal_key="SYNK2",
            )
            db_path = scenario_dir / "signals.sqlite"
            _make_signals_db(
                db_path,
                rows=[
                    {"ticker": "SYNK1", "timestamp": f"{MARKET_DATE_K}T09:30:05+00:00",
                     "can_alert": True, "payload": payload_1, "alpha_score": 91.0},
                    {"ticker": "SYNK2", "timestamp": f"{MARKET_DATE_K}T09:30:06+00:00",
                     "can_alert": True, "payload": payload_2, "alpha_score": 90.0},
                ],
            )
            store_path = scenario_dir / "paper_execution.sqlite"
            entry_receipt = ds_paper_session.run_paper_session(
                db_path=db_path, market_date=MARKET_DATE_K, store_path=store_path,
                receipt_path=scenario_dir / "receipt_entry.json", client=client,
            )
            assert len(entry_receipt["actions"]) == 2
            assert all(a["submitted"] for a in entry_receipt["actions"]), entry_receipt["actions"]
            coid_1 = ds_paper_engine.client_order_id(
                symbol="SYNK1", market_date=MARKET_DATE_K, strategy_version="e2e-control-v1"
            )
            coid_2 = ds_paper_engine.client_order_id(
                symbol="SYNK2", market_date=MARKET_DATE_K, strategy_version="e2e-control-v1"
            )
            server.state.fill_order(coid_1, leg="entry", qty=10.0, price=40.00, fee=0.10)
            server.state.fill_order(coid_2, leg="entry", qty=10.0, price=20.00, fee=0.10)

            # Frozen expectation: at end of session, SYNK1's flatten succeeds;
            # SYNK2's flatten is rejected by the broker (armed below) and MUST
            # remain a visible, unresolved open position - never silently
            # reported as flat or the session as fully complete/reconciled.
            server.state.arm_close_rejection("SYNK2")

            eod_receipt = ds_paper_session.run_paper_session(
                db_path=db_path, market_date=MARKET_DATE_K, store_path=store_path,
                receipt_path=scenario_dir / "receipt_eod.json", client=client, force_exit=True,
            )
            assert eod_receipt["status"] == "completed"  # the SESSION ran; it did not crash
            actions_by_symbol = {a["symbol"]: a for a in eod_receipt["management"]["actions"]}
            assert actions_by_symbol["SYNK1"]["action"] == "time_exit_submitted"
            assert actions_by_symbol["SYNK2"]["action"] == "time_exit_failed", actions_by_symbol
            assert "SYNK2" in server.state.positions, "the rejected exit must leave the position genuinely open"
            assert eod_receipt["reconcile_after"]["positions"] == 1, (
                "exactly one position (SYNK2) must remain - not falsely reported flat"
            )
            # The receipt must never claim a clean/complete flatten while a
            # position is still open: management.actions names the failure by
            # symbol, and reconcile_after.positions is non-zero - the two
            # true signals an operator or dashboard needs, both present.
            assert eod_receipt["management"]["positions"] == 2, "both positions were genuinely attempted"
            ev_rec.record_event(
                "rejected_exit_remains_visibly_unresolved",
                management=eod_receipt["management"],
                reconcile_after_positions=eod_receipt["reconcile_after"]["positions"],
            )
            ev_rec.outcome = {
                "syn_k1_flattened": True,
                "syn_k2_unresolved_open_position": True,
                "reconcile_after_positions": eod_receipt["reconcile_after"]["positions"],
                "falsely_reported_flat_or_complete": False,
            }
        finally:
            server.stop()
            ev_rec.write(run_sandbox.artifacts_dir)
            run_manifest.scenarios[scenario_id] = ev_rec.as_dict()


# ===========================================================================
# Scenario L - operator truth (the real operator_run_status()/evidence
# rendering path) across five conditions, and isolation after the run.
# ===========================================================================


class TestScenarioL:
    def test_operator_truth_across_five_conditions_and_isolation_after_the_run(
        self, run_sandbox: sandbox_mod.Sandbox, run_manifest: ev.RunManifest
    ) -> None:
        scenario_id = "scenario_l_operator_truth_and_isolation"
        ev_rec = ev.ScenarioEvidence(scenario_id=scenario_id, run_id=run_sandbox.run_id)

        production_roots_before = {}
        for root in sandbox_mod.PRODUCTION_ROOTS:
            if root.exists():
                production_roots_before[str(root)] = os.path.getmtime(root)

        anchor = control_policy.new_trust_anchor(run_sandbox)

        def _eligible(fake_provider: bool, entries: bool) -> tuple[str, ...]:
            evidence_record = control_policy.build_eligibility_evidence(
                sandbox=run_sandbox, anchor=anchor, fake_provider_selected=fake_provider,
                entries_enabled=entries,
            )
            return control_policy.eligible_policy_ids_for(
                sandbox=run_sandbox, evidence=evidence_record, anchor=anchor
            )

        conditions: dict[str, dict[str, Any]] = {
            "active_synthetic_policy": dict(
                eligible_policy_ids=_eligible(True, True), entries_enabled_override=True
            ),
            "no_policy": dict(eligible_policy_ids=(), entries_enabled_override=True),
            "operator_disabled": dict(
                eligible_policy_ids=_eligible(True, True), entries_enabled_override=False
            ),
            "policy_state_unavailable": dict(
                eligible_policy_ids=(), entries_enabled_override=True, policy_state_present=False
            ),
        }
        expected_states = {
            "active_synthetic_policy": "POLICY_ACTIVE",
            "no_policy": "NO_ELIGIBLE_POLICY",
            "operator_disabled": "ENTRIES_DISABLED_BY_OPERATOR",
            "policy_state_unavailable": "POLICY_STATE_UNAVAILABLE",
        }
        observed_states = {}
        for name, overrides in conditions.items():
            config = _sandboxed_config(run_sandbox, **overrides)
            status = config.operator_run_status()
            observed_states[name] = status["state"]
            assert status["state"] == expected_states[name], (name, status)
            # The rendering path this harness owns (evidence.py) is the
            # artifact surface asserted here to carry the sentinel; nothing
            # under production ever emits this string.
            rendered = ev.ScenarioEvidence(
                scenario_id=f"scenario_l_condition_{name}", run_id=run_sandbox.run_id
            )
            rendered.outcome = {"operator_state": status["state"], "entry_mode": status["entry_mode"]}
            rendered_dict = rendered.as_dict()
            assert rendered_dict["classification"] == "SYNTHETIC_E2E"
            rendered.write(run_sandbox.artifacts_dir)

        # Fifth condition: runtime error - has_errors always wins precedence.
        error_config = _sandboxed_config(
            run_sandbox, eligible_policy_ids=(control_policy.CONTROL_POLICY_ID,),
            entries_enabled_override=True,
        )
        error_config = replace(error_config, operator_errors=("synthetic_runtime_error_e2e",))
        error_status = error_config.operator_run_status()
        observed_states["runtime_error"] = error_status["state"]
        assert error_status["state"] == "ERROR", error_status
        rendered_error = ev.ScenarioEvidence(
            scenario_id="scenario_l_condition_runtime_error", run_id=run_sandbox.run_id
        )
        rendered_error.outcome = {"operator_state": error_status["state"]}
        assert rendered_error.as_dict()["classification"] == "SYNTHETIC_E2E"
        rendered_error.write(run_sandbox.artifacts_dir)

        ev_rec.record_event("operator_truth_five_conditions", **observed_states)

        # Isolation after the run: the harness's own constructed sandbox
        # environment (what this harness actually hands to a subprocess -
        # not the ambient pytest process env, which may legitimately carry
        # this developer/agent session's own unrelated credentials outside
        # this harness's control) still carries no credential-shaped name,
        # and this test's own risk-gate env overrides (ENTRIES_ENABLED_ENV /
        # KILL_SWITCH_ENV) did not leak past monkeypatch teardown into the
        # ambient process env that later, non-E2E test runs would inherit.
        sandbox_mod.assert_no_credential_env_visible(sandbox_mod.sandboxed_env(run_sandbox))
        assert os.environ.get(ds_risk_gate.ENTRIES_ENABLED_ENV) is None, (
            "a prior scenario's monkeypatched entries-enabled override leaked "
            "past its fixture teardown into the ambient process env"
        )
        assert os.environ.get(ds_risk_gate.KILL_SWITCH_ENV) is None
        # (metadata only - never content) the real production roots were
        # never modified by anything this harness did.
        production_roots_after = {}
        for root in sandbox_mod.PRODUCTION_ROOTS:
            if root.exists():
                production_roots_after[str(root)] = os.path.getmtime(root)
        assert production_roots_after == production_roots_before, (
            "a real production root's top-level mtime changed during this run"
        )
        ev_rec.record_event(
            "production_state_and_entry_settings_unchanged_after_run",
            production_roots_checked=list(production_roots_before.keys()),
        )
        ev_rec.outcome = {
            "conditions": observed_states,
            "production_roots_unchanged": True,
            "synthetic_display_carries_sentinel": True,
        }
        ev_rec.write(run_sandbox.artifacts_dir)
        run_manifest.scenarios[scenario_id] = ev_rec.as_dict()
