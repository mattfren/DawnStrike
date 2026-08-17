"""Collect current nodes and compare them to the frozen canonical inventory."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from scripts.run_pytest_shard import collect_nodes


def main() -> int:
    canonical = json.loads(
        (EVIDENCE / "canonical-pytest-inventory.json").read_text(encoding="utf-8")
    )
    nodes = collect_nodes()
    digest = hashlib.sha256(("\n".join(nodes) + "\n").encode("utf-8")).hexdigest()
    canonical_nodes = tuple(canonical["nodes"])
    valid = (
        len(nodes) == canonical["node_count"] == 3156
        and digest == canonical["inventory_sha256"]
        and nodes == canonical_nodes
    )
    payload = {
        "schema_version": "dawnstrike.pytest_inventory_validation.v1",
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "expected_node_count": canonical["node_count"],
        "actual_node_count": len(nodes),
        "expected_inventory_sha256": canonical["inventory_sha256"],
        "actual_inventory_sha256": digest,
        "nodes_exactly_equal": nodes == canonical_nodes,
        "valid": valid,
    }
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
