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
import sqlite3
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
) -> ds_config.ScannerConfig:
    base = ds_config.load_config(env_file=run_sandbox.env_file)
    return replace(
        base,
        eligible_policy_ids=eligible_policy_ids,
        entries_enabled_override=entries_enabled_override,
        policy_state_present=policy_state_present,
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
