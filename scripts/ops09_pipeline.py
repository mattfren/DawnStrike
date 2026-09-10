"""Run one OPS09 capture and its existing public consumers under one guard."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from intraday_scanner.observation.ops06_bars_adapter import adapt_ops05_to_r3  # noqa: E402
from intraday_scanner.observation.ops09 import (  # noqa: E402
    _request_contract,
    _run_consumers,
    validate_ops09_scope,
)
from scripts.prepare_r3_observational_registration import (  # noqa: E402
    prepare_actual_observational_registration,
)


class _BoundedWriter:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def _tree_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def __call__(self, path: Path, data: bytes) -> None:
        path = path.resolve()
        if not path.is_relative_to(self.root):
            raise RuntimeError(f"OPS09 downstream publication escaped account root: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        projected = self._tree_bytes() + len(data)
        if projected > self.max_bytes:
            raise RuntimeError(
                f"OPS09 downstream byte budget rejects {path.name}: "
                f"projected={projected}, cap={self.max_bytes}"
            )
        temporary.write_bytes(data)
        temporary.replace(path)


def _compact_consumer_value(value, *, depth: int = 0):
    """Keep consumer truth/identity without copying the adapter dataset."""
    if depth > 5:
        return {"sha256": hashlib.sha256(repr(value).encode()).hexdigest()}
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            if isinstance(item, (dict, list)):
                result[name] = _compact_consumer_value(item, depth=depth + 1)
            elif isinstance(item, (str, int, float, bool)) or item is None:
                if (
                    lowered.endswith(
                        ("status", "reason", "count", "counts", "id", "sha256", "version")
                    )
                    or lowered in {
                        "eligibility", "identity", "model", "decision", "coverage",
                        "source", "research_only", "broker_execution_enabled",
                    }
                ):
                    text = item if not isinstance(item, str) else item[:2048]
                    result[name] = text
        return result
    if isinstance(value, list):
        if len(value) > 64:
            raw = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
            return {
                "count": len(value),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "sample": [
                    _compact_consumer_value(row, depth=depth + 1)
                    for row in value[:3]
                ],
            }
        return [_compact_consumer_value(row, depth=depth + 1) for row in value]
    if isinstance(value, str):
        return value[:2048]
    return value


def _ops05_main():
    module_path = REPO_ROOT / "scripts" / "ops05_historical_bars.py"
    spec = importlib.util.spec_from_file_location("dawnstrike_ops05_cli", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("OPS09 could not load the source-pinned OPS05 CLI")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--census", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-config-hash", required=True)
    parser.add_argument("--source-config", type=Path)
    parser.add_argument("--capture-receipt-hash")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--decision-artifact", type=Path)
    parser.add_argument("--registration-context", type=Path)
    parser.add_argument(
        "--retained-capture-root", type=Path,
        help="consume an already authenticated archive without invoking OPS05/provider",
    )
    parser.add_argument("--adapter-output-root", type=Path)
    parser.add_argument("--database-path", type=Path)
    parser.add_argument("--repo-sha", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--downstream-max-bytes", type=int, default=7 * 1024 * 1024)
    parser.add_argument("--in-memory-consumer", action="store_true")
    parser.add_argument("--producer-mode", choices=("fixture", "actual"), default="fixture")
    parser.add_argument("--actual-source-root", type=Path)
    parser.add_argument("--actual-entitlement", type=Path)
    parser.add_argument("--actual-census", type=Path)
    parser.add_argument("--registration-output-root", type=Path)
    parser.add_argument("--request-contract-output", type=Path)
    parser.add_argument("--cohort-plan", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    actual_registration = None
    actual_scope = None
    request_contract_path = (
        args.request_contract_output.resolve()
        if args.request_contract_output else None
    )
    if args.producer_mode == "actual":
        if args.actual_source_root is None or args.registration_output_root is None:
            raise RuntimeError("actual producer requires source and registration roots")
        actual_registration = prepare_actual_observational_registration(
            source_root=args.actual_source_root.resolve(),
            output_root=args.registration_output_root.resolve(),
            market_date=args.market_date,
            entitlement=args.actual_entitlement.resolve() if args.actual_entitlement else None,
            source_config=args.source_config.resolve() if args.source_config else None,
            typed_census=args.actual_census.resolve() if args.actual_census else None,
        )
        actual_scope = Path(actual_registration["scope_path"])
        if request_contract_path is None:
            request_contract_path = actual_scope.parent / "request-contract.json"
        if args.census is None:
            generated_census = actual_scope.parent / "capture-census.json"
            scope_value = json.loads(actual_scope.read_text(encoding="utf-8"))
            movers = scope_value.get("scopes", {}).get("original_small_cap_gap", [])
            generated_census.write_text(
                json.dumps(movers, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            args.census = generated_census
        if args.decision_artifact is None:
            args.decision_artifact = actual_scope.parent / "alpha_v6_decisions.actual.json"
        if args.registration_context is None:
            args.registration_context = actual_scope
        if args.capture_receipt_hash is None and args.cohort_plan is not None:
            plan = json.loads(args.cohort_plan.read_text(encoding="utf-8"))
            session = next(
                row for row in plan["sessions"]
                if row["market_date"] == args.market_date
            )
            scope = validate_ops09_scope(actual_scope, expected_date=args.market_date)
            request = _request_contract(
                plan=plan, session=session, scope=scope, scope_path=actual_scope
            )
            request_contract_path.parent.mkdir(parents=True, exist_ok=True)
            request_contract_path.write_text(
                json.dumps(request, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            args.capture_receipt_hash = request["request_contract_sha256"]
        if args.capture_receipt_hash is None:
            request = {
                "schema_version": "dawnstrike.ops09.request_contract.v1",
                "status": "REQUESTED",
                "market_date": args.market_date,
                "producer_mode": "actual",
                "scope_path": str(actual_scope),
                "scope_sha256": __import__("hashlib").sha256(actual_scope.read_bytes()).hexdigest(),
                "decision_artifact_path": str(args.decision_artifact),
                "registration_context_path": str(args.registration_context),
                "source_config_sha256": args.source_config_hash,
                "capture_receipt_hash_alias": {
                    "source_field": "request_contract_sha256",
                    "target_cli_argument": "--capture-receipt-hash",
                    "authenticated": True,
                },
            }
            request_contract_path.parent.mkdir(parents=True, exist_ok=True)
            request_contract_path.write_text(
                json.dumps(request, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            args.capture_receipt_hash = hashlib.sha256(
                request_contract_path.read_bytes()
            ).hexdigest()
    if args.census is None:
        raise RuntimeError("OPS09 pipeline requires a census or actual producer")
    retained_root = args.retained_capture_root.resolve() if args.retained_capture_root else None
    if retained_root is not None:
        if not (retained_root / "receipt.json").is_file():
            raise RuntimeError("retained capture receipt is missing")
        capture_root = retained_root
        exit_code = 0
    else:
        capture_root = args.output_root.resolve()
        ops05_args = [
        "ops05_historical_bars.py", "--market-date", args.market_date,
        "--census", str(args.census), "--output-root", str(args.output_root),
        "--source-config-hash", args.source_config_hash,
        "--capture-receipt-hash", args.capture_receipt_hash,
        "--max-bytes", str(args.max_bytes),
        ]
        if args.fixture is not None:
            ops05_args += ["--fixture", str(args.fixture)]
        elif args.execute:
            ops05_args += ["--execute", "--env-file", str(args.env_file or ".env")]
        else:
            print(json.dumps({"status": "READY", "capture_root": str(args.output_root.resolve())}))
            return 0
        old_argv = sys.argv
        try:
            sys.argv = ops05_args
            exit_code = int(_ops05_main()() or 0)
        finally:
            sys.argv = old_argv
    if exit_code != 0:
        return exit_code
    payload = {"status": "CAPTURED", "capture_root": str(capture_root)}
    if actual_registration is not None:
        payload["actual_registration"] = {
            "scope_path": str(actual_scope),
            "scope_sha256": __import__("hashlib").sha256(actual_scope.read_bytes()).hexdigest(),
            "decision_artifact_path": str(args.decision_artifact),
            "request_contract_path": str(request_contract_path),
            "request_contract_sha256": hashlib.sha256(
                request_contract_path.read_bytes()
            ).hexdigest(),
        }
    if args.decision_artifact is None or not args.decision_artifact.is_file():
        payload["decision_status"] = "MISSING_INPUT"
        print(json.dumps(payload, sort_keys=True))
        return 0
    adapter_root = (
        args.adapter_output_root or (args.output_root.resolve().parent / "ops06")
    ).resolve()
    bounded_writer = _BoundedWriter(adapter_root, args.downstream_max_bytes)
    adapted = adapt_ops05_to_r3(
        observation_root=capture_root,
        decision_artifact=args.decision_artifact.resolve(),
        output_root=adapter_root,
        as_of=args.as_of,
        write_bytes=bounded_writer,
        registration_context=args.registration_context,
    )
    if args.database_path is None:
        raise RuntimeError("OPS09 native pipeline requires an isolated database path")
    consumers = _run_consumers(
        adapted=adapted,
        database_path=args.database_path.resolve(),
        session={"market_date": args.market_date},
        repo_sha=args.repo_sha,
        in_memory=args.in_memory_consumer,
    )
    compact_consumers = {
        "database_mode": consumers["database_mode"],
        "daily": _compact_consumer_value(consumers["daily"]),
        "weekly": _compact_consumer_value(consumers["weekly"]),
    }
    consumer_result_bytes = json.dumps(
        {
            "schema_version": "dawnstrike.ops09.guarded_consumer_results.v1",
            "market_date": args.market_date,
            "database_mode": compact_consumers["database_mode"],
            "daily": compact_consumers["daily"],
            "weekly": compact_consumers["weekly"],
            "receipt_semantics": (
                "compact_authenticated_consumer_summary_references_"
                "persisted_adapter_dataset"
            ),
            "adapter_dataset_path": str((adapter_root / "observation-dataset.json").resolve()),
            "adapter_dataset_sha256": hashlib.sha256(
                (adapter_root / "observation-dataset.json").read_bytes()
            ).hexdigest(),
            "adapter_raw_events_path": str((adapter_root / "raw-events.jsonl").resolve()),
            "adapter_raw_events_sha256": hashlib.sha256(
                (adapter_root / "raw-events.jsonl").read_bytes()
            ).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8") + b"\n"
    consumer_result_path = adapter_root / "consumer-results.json"
    bounded_writer(consumer_result_path, consumer_result_bytes)
    payload.update({
        "decision_status": "BOUND",
        "adapter_output_root": str(adapter_root),
        "adapter_status": adapted["adapter_packet"].get("status"),
        "label_count": len(adapted["adapter_packet"].get("labels", [])),
        "consumer_results_path": str(consumer_result_path),
        "consumer_results_sha256": __import__("hashlib").sha256(consumer_result_bytes).hexdigest(),
        "downstream_account_root": str(adapter_root),
        "downstream_existing_bytes": bounded_writer._tree_bytes(),
        "downstream_max_bytes": args.downstream_max_bytes,
        "consumers": {
            "daily_status": consumers["daily"].get("status"),
            "weekly_status": consumers["weekly"].get("status"),
            "database_path": consumers["database_path"],
            "database_mode": consumers["database_mode"],
            "consumer_results_path": str(consumer_result_path),
        },
    })
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
