"""Capture immutable source/test identities from the disposable candidate index."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
PREFIXES = (".github/workflows/", "api/", "intraday_scanner/", "scripts/", "tests/", "web/")
ROOT_FILES = {
    "app.py",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "vercel.json",
}


def main() -> int:
    paths = subprocess.check_output(
        ["git", "ls-files"], cwd=ROOT, text=True
    ).splitlines()
    selected = tuple(
        sorted(
            path
            for path in paths
            if path in ROOT_FILES or path.startswith(PREFIXES)
        )
    )
    entries = []
    aggregate = hashlib.sha256()
    for relative in selected:
        content = (ROOT / relative).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")
        entries.append({"path": relative, "length": len(content), "sha256": digest})
    payload = {
        "schema_version": "dawnstrike.source_test_freeze.v1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "file_count": len(entries),
        "aggregate_sha256": aggregate.hexdigest(),
        "files": entries,
    }
    output = Path(__file__).resolve().parent / "source-test-freeze.json"
    output.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("file_count", "aggregate_sha256")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
