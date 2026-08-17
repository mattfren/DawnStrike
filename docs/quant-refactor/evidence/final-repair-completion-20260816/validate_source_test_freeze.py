"""Rehash every frozen source/test file without refreshing the freeze."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent


def main() -> int:
    frozen = json.loads((EVIDENCE / "source-test-freeze.json").read_text(encoding="utf-8"))
    aggregate = hashlib.sha256()
    mismatches = []
    for expected in frozen["files"]:
        relative = expected["path"]
        path = ROOT / relative
        if not path.is_file():
            mismatches.append({"path": relative, "reason": "missing"})
            continue
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")
        if len(content) != expected["length"] or digest != expected["sha256"]:
            mismatches.append(
                {
                    "path": relative,
                    "reason": "identity_mismatch",
                    "expected_length": expected["length"],
                    "actual_length": len(content),
                    "expected_sha256": expected["sha256"],
                    "actual_sha256": digest,
                }
            )
    actual_aggregate = aggregate.hexdigest()
    valid = (
        len(frozen["files"]) == frozen["file_count"] == 572
        and actual_aggregate == frozen["aggregate_sha256"]
        and not mismatches
    )
    payload = {
        "schema_version": "dawnstrike.source_test_freeze_validation.v1",
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "file_count": len(frozen["files"]),
        "expected_aggregate_sha256": frozen["aggregate_sha256"],
        "actual_aggregate_sha256": actual_aggregate,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "valid": valid,
    }
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
