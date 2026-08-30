"""Validate a capture plan without contacting a provider or writing evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capture_intraday_operations import _build_plan, _parser

from intraday_scanner.services.capture_operations import CapturePlanError, plan_as_dict


def main() -> int:
    args = _parser().parse_args()
    try:
        result = plan_as_dict(_build_plan(args))
    except CapturePlanError as exc:
        print(
            json.dumps(
                {
                    "schema_version": "dawnstrike.capture_operation_doctor.v1",
                    "status": "BLOCKED",
                    "reason": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    result["schema_version"] = "dawnstrike.capture_operation_doctor.v1"
    result["provider_network_performed"] = False
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
