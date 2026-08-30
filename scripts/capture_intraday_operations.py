"""Run a validated, read-only forward or retrospective SIP capture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.errors import StorageError
from intraday_scanner.services.capture_operations import CapturePlan, CapturePlanError, plan_as_dict
from intraday_scanner.storage.intraday_evidence_store import IntradayEvidenceStore


def main() -> int:
    args = _parser().parse_args()
    plan = _build_plan(args)
    try:
        prepared = plan_as_dict(plan)
    except CapturePlanError as exc:
        _print_failure(str(exc))
        return 2
    if not args.execute:
        print(json.dumps(prepared, sort_keys=True))
        return 0

    # The capture plan has already authenticated the checked-in calendar,
    # exact candidate SHA, and write-once session file.  Populate only the
    # expected-session denominator here; account/reporting ledgers remain
    # untouched until a separate authenticated outcome boundary exists.
    try:
        expected = json.loads(plan.expected_session.read_text(encoding="utf-8"))
        IntradayEvidenceStore(prepared["db_path"]).persist_expected_market_session(
            {
                "session_id": prepared["exchange_session_id"],
                "market_date": prepared["market_date"],
                "exchange": expected["exchange"],
                "session_open_utc": expected["start_utc"],
                "session_close_utc": expected["end_utc"],
                "status": "EXPECTED",
                "calendar_source": expected.get("calendar_id") or "checked_market_calendar",
                "calendar_source_hash_sha256": prepared["expected_session_sha256"],
                "created_at": datetime.now(UTC).isoformat(),
                "research_only": True,
                "broker_execution_enabled": False,
            }
        )
    except (OSError, json.JSONDecodeError, KeyError, CapturePlanError, StorageError) as exc:
        _print_failure(f"expected session denominator persistence failed: {exc}")
        return 2

    mode_run = Path(prepared["mode_run_root"])
    mode_output = Path(prepared["mode_output_root"])
    mode_run.mkdir(parents=True, exist_ok=True)
    mode_output.mkdir(parents=True, exist_ok=True)
    symbols_file = mode_run / "symbols-frozen.txt"
    symbols_file.write_text("\n".join(prepared["symbols"]) + "\n", encoding="utf-8")
    metadata_path = mode_run / "operator-entitlement-sanitized.json"
    metadata_path.write_text(
        json.dumps(
            plan.sanitized_entitlement_metadata(
                receipt_hash=prepared["entitlement_receipt_sha256"]
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(Path(__file__).with_name("capture_intraday_evidence.py")),
        "--provider",
        prepared["provider"],
        "--feed",
        prepared["feed"],
        "--evidence-mode",
        prepared["mode"],
        "--symbols-file",
        str(symbols_file),
        "--market-date",
        prepared["market_date"],
        "--exchange-session-id",
        prepared["exchange_session_id"],
        "--utc-start",
        prepared["request_start"],
        "--utc-end",
        prepared["request_end"],
        "--db-path",
        prepared["db_path"],
        "--evidence-root",
        prepared["mode_evidence_root"],
        "--run-root",
        prepared["mode_run_root"],
        "--code-sha",
        prepared["candidate_sha"],
        "--source-config-hash",
        prepared["source_config_sha256"],
        "--operator-entitlement-metadata",
        str(metadata_path),
        "--env-file",
        str(plan.env_file),
        "--include-trades",
        "--include-quotes",
        "--include-corporate-actions",
    ]
    env = os.environ.copy()
    env["ALPACA_DATA_FEED"] = "sip"
    env["DAWNSTRIKE_INTRADAY_MAX_PAGES"] = str(plan.max_pages)
    env["INTRADAY_REQUEST_RETRIES"] = str(plan.retries)
    result = subprocess.run(command, cwd=plan.repo_root, env=env, capture_output=True, text=True)
    receipt = _safe_capture_receipt(result, prepared)
    receipt["receipt_identity_sha256"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    output_path = mode_output / (
        f"capture-{prepared['market_date']}-{receipt['receipt_identity_sha256'][:16]}.json"
    )
    try:
        _write_once_json(output_path, receipt)
    except RuntimeError as exc:
        _print_failure(str(exc))
        return 3
    print(json.dumps(receipt, sort_keys=True))
    return 0 if result.returncode == 0 else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("forward_observed", "retrospective_research"),
        required=True,
    )
    parser.add_argument("--provider", default="alpaca")
    parser.add_argument("--feed", default="sip")
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--symbols-manifest", type=Path, required=True)
    parser.add_argument("--symbols-manifest-sha256", required=True)
    parser.add_argument("--expected-session", type=Path, required=True)
    parser.add_argument("--entitlement-receipt", type=Path, required=True)
    parser.add_argument("--entitlement-receipt-sha256", required=True)
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--source-config-sha256", required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--execute", action="store_true", help="perform the provider read-only capture"
    )
    return parser


def _build_plan(args: argparse.Namespace) -> CapturePlan:
    return CapturePlan(
        mode=args.mode,
        provider=args.provider,
        feed=args.feed,
        candidate_sha=args.candidate_sha,
        repo_root=args.repo_root,
        db_path=args.db_path,
        evidence_root=args.evidence_root,
        run_root=args.run_root,
        output_root=args.output_root,
        symbols_manifest=args.symbols_manifest,
        symbols_manifest_sha256=args.symbols_manifest_sha256,
        expected_session=args.expected_session,
        entitlement_receipt=args.entitlement_receipt,
        entitlement_receipt_sha256=args.entitlement_receipt_sha256,
        source_config=args.source_config,
        source_config_sha256=args.source_config_sha256,
        env_file=args.env_file,
        max_pages=args.max_pages,
        retries=args.retries,
    )


def _safe_capture_receipt(
    process: subprocess.CompletedProcess[str], plan: dict[str, Any]
) -> dict[str, Any]:
    inner: dict[str, Any] | None = None
    for line in reversed(process.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            inner = candidate
            break
    inner_status = str(inner.get("status") or "") if inner else ""
    payload: dict[str, Any] = {
        "schema_version": "dawnstrike.capture_operation_result.v1",
        "status": (
            "CAPTURED"
            if process.returncode == 0
            else "CAPTURE_INCOMPLETE"
            if inner_status in {"PARTIAL", "NO_DATA"}
            else "CAPTURE_PROCESS_FAILED"
        ),
        "exit_code": process.returncode,
        "mode": plan["mode"],
        "provider": plan["provider"],
        "feed": plan["feed"],
        "candidate_sha": plan["candidate_sha"],
        "candidate_tree_sha": plan["candidate_tree_sha"],
        "candidate_worktree_clean": plan["candidate_worktree_clean"],
        "plan_identity_sha256": plan["plan_identity_sha256"],
        "market_date": plan["market_date"],
        "source_config_sha256": plan["source_config_sha256"],
        "entitlement_receipt_sha256": plan["entitlement_receipt_sha256"],
        "required_endpoints": plan["required_endpoints"],
        "broker_execution": "disabled",
    }
    if inner is not None:
        payload["capture_status"] = inner_status
        payload.update(
            {
                key: inner[key]
                for key in (
                    "run_id",
                    "session_id",
                    "coverage",
                    "state_path",
                    "started_at",
                    "completed_at",
                    "created_at",
                    "source_identity",
                    "artifact_identity",
                    "raw_artifact_hash_sha256",
                    "normalized_artifact_hash_sha256",
                    "receipt_hash_sha256",
                )
                if key in inner
            }
        )
    return payload


def _write_once_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
        return
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
            raise RuntimeError(
                "capture result receipt identity conflicts with retained evidence"
            ) from None


def _print_failure(reason: str) -> None:
    print(
        json.dumps(
            {
                "schema_version": "dawnstrike.capture_operation_result.v1",
                "status": "BLOCKED",
                "reason": reason,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
