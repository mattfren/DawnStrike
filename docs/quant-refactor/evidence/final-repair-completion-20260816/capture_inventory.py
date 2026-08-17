"""Persist the frozen canonical pytest node inventory and identity."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.run_pytest_shard import collect_nodes


def main() -> int:
    nodes = collect_nodes()
    encoded = ("\n".join(nodes) + "\n").encode("utf-8")
    payload = {
        "schema_version": "dawnstrike.pytest_inventory.v1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "node_count": len(nodes),
        "inventory_sha256": hashlib.sha256(encoded).hexdigest(),
        "nodes": nodes,
    }
    output = Path(__file__).resolve().parent / "canonical-pytest-inventory.json"
    output.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("node_count", "inventory_sha256")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
