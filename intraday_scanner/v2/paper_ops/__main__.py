"""CLI for PaperOps v1."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.paper_ops.calendar_truth import verify_calendar_truth
from intraday_scanner.v2.paper_ops.calendar_view import write_calendar_view
from intraday_scanner.v2.paper_ops.challenger_evaluation import (
    evaluate_paperops_challengers,
)
from intraday_scanner.v2.paper_ops.engine import (
    calendar,
    check,
    close,
    demo,
    enter,
    init,
    preflight,
    reconcile,
    replay,
    report,
    run_day,
    scan,
)
from intraday_scanner.v2.paper_ops.governance import apply_evidence_governance
from intraday_scanner.v2.paper_ops.ledger_rebuild import rebuild_ledger
from intraday_scanner.v2.paper_ops.models import PaperRunMode
from intraday_scanner.v2.paper_ops.observer_safety import (
    OBSERVER_COMMAND_SPECS,
    PaperOpsObserverBlocked,
    require_observer_command,
)
from intraday_scanner.v2.paper_ops.readiness import forward_readiness
from intraday_scanner.v2.paper_ops.session_gaps import record_forward_session_gap
from intraday_scanner.v2.paper_ops.shadow_runner import (
    initialize_shadow_registry,
    register_shadow_challenger,
    run_shadow_day,
)
from intraday_scanner.v2.paper_ops.source_bar_truth import verify_source_bar_truth
from intraday_scanner.v2.paper_ops.strategy_evidence import score_strategy_evidence
from intraday_scanner.v2.paper_ops.trade_blotter import (
    build_trade_blotter,
    verify_trade_blotter,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 PaperOps")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "preflight",
            "scan",
            "enter",
            "check",
            "close",
            "run-day",
            "replay",
            "calendar",
            "reconcile",
            "report",
            "demo",
            "rebuild-ledger",
            "verify-calendar",
            "evidence",
            "readiness",
            "calendar-view",
            "blotter",
            "verify-blotter",
            "verify-source-bars",
            "shadow-init",
            "shadow-register",
            "shadow-run",
            "challenger-evaluate",
            "record-forward-gap",
            "apply-evidence-governance",
        ),
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--mode", choices=("forward", "replay", "demo"), default="forward")
    parser.add_argument("--output-root", default="data/v2_paper_ops")
    parser.add_argument("--write-rebuilt", action="store_true")
    parser.add_argument("--manifest")
    parser.add_argument("--reason-code", default="historical_forward_run_absent")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    run_date = date.fromisoformat(args.date)
    mode = PaperRunMode(args.mode)
    observer_command = args.command in OBSERVER_COMMAND_SPECS and not (
        args.command == "rebuild-ledger" and args.write_rebuilt
    )
    if observer_command:
        try:
            require_observer_command(output_root, args.command)
        except PaperOpsObserverBlocked as exc:
            print(f"status: {exc.status}")
            print(f"detail: {exc.detail}")
            return 2
    if args.command == "init":
        result = init(output_root=output_root)
    elif args.command == "preflight":
        result = preflight(run_date=run_date, mode=mode, output_root=output_root)
    elif args.command == "scan":
        result = scan(run_date=run_date, mode=mode, output_root=output_root)
    elif args.command == "enter":
        result = enter(run_date=run_date, mode=mode, output_root=output_root)
    elif args.command == "check":
        result = check(run_date=run_date, mode=mode, output_root=output_root)
    elif args.command == "close":
        result = close(run_date=run_date, mode=mode, output_root=output_root)
    elif args.command == "run-day":
        result = run_day(run_date=run_date, mode=mode, output_root=output_root)
    elif args.command == "replay":
        start = date.fromisoformat(args.start or args.date)
        end = date.fromisoformat(args.end or args.date)
        result = replay(start=start, end=end, output_root=output_root)
    elif args.command == "calendar":
        result = calendar(output_root=output_root)
    elif args.command == "reconcile":
        result = reconcile(output_root=output_root)
    elif args.command == "report":
        result = report(output_root=output_root)
    elif args.command == "rebuild-ledger":
        result = rebuild_ledger(
            output_root=output_root,
            write_rebuilt=bool(args.write_rebuilt),
        ).to_dict()
    elif args.command == "verify-calendar":
        result = verify_calendar_truth(output_root=output_root).to_dict()
    elif args.command == "evidence":
        result = score_strategy_evidence(output_root=output_root).to_dict()
    elif args.command == "readiness":
        result = forward_readiness(output_root=output_root).to_dict()
    elif args.command == "calendar-view":
        result = write_calendar_view(output_root=output_root)
    elif args.command == "blotter":
        result = build_trade_blotter(
            output_root=output_root,
            mode=mode.value,
            run_date=run_date.isoformat(),
        )
    elif args.command == "verify-blotter":
        result = verify_trade_blotter(output_root=output_root, mode=mode.value)
    elif args.command == "verify-source-bars":
        result = verify_source_bar_truth(output_root=output_root, mode=mode).to_dict()
    elif args.command == "shadow-init":
        result = initialize_shadow_registry(output_root=output_root)
    elif args.command == "shadow-register":
        if not args.manifest:
            parser.error("shadow-register requires --manifest")
        result = register_shadow_challenger(
            manifest_path=Path(args.manifest),
            output_root=output_root,
        )
    elif args.command == "shadow-run":
        result = run_shadow_day(
            run_date=run_date,
            mode=mode,
            output_root=output_root,
        )
    elif args.command == "challenger-evaluate":
        result = evaluate_paperops_challengers(output_root=output_root)
    elif args.command == "record-forward-gap":
        result = record_forward_session_gap(
            output_root=output_root,
            market_date=run_date.isoformat(),
            reason_code=args.reason_code,
        )
    elif args.command == "apply-evidence-governance":
        result = apply_evidence_governance(output_root=output_root)
    else:
        result = demo(output_root=output_root)
    for key, value in result.items():
        print(f"{key}: {value}")
    return _result_exit_code(args.command, result)


def _result_exit_code(command: str, result: dict[str, object]) -> int:
    """Make scheduler-visible evidence failures return a nonzero process code."""

    if command == "run-day":
        reconciliation = result.get("reconcile")
        if isinstance(reconciliation, dict):
            status = str(reconciliation.get("status") or "").lower()
            if status and status != "passed":
                return 2
    status = str(result.get("status") or "").lower()
    if status in {
        "blocked",
        "failed",
        "invalid",
        "mismatch",
        "not_ready",
        "not ready",
    }:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
