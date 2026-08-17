"""Build and independently recheck the WP007 evidence manifest."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
EXCLUDED = {"evidence-manifest.json", "evidence-manifest.sha256"}


def main() -> int:
    files = [
        path
        for path in sorted(EVIDENCE.rglob("*"))
        if path.is_file() and path.name not in EXCLUDED
    ]
    entries = [
        {
            "path": str(path.relative_to(EVIDENCE)).replace("\\", "/"),
            "length": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in files
    ]
    manifest = {
        "schema_version": "dawnstrike.wp007_evidence_manifest.v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "entry_count": len(entries),
        "entries": entries,
    }
    manifest_path = EVIDENCE / "evidence-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_hash = _sha256(manifest_path)
    (EVIDENCE / "evidence-manifest.sha256").write_text(
        f"{manifest_hash}  evidence-manifest.json\n",
        encoding="utf-8",
    )
    for entry in entries:
        path = EVIDENCE / str(entry["path"])
        if path.stat().st_size != entry["length"] or _sha256(path) != entry["sha256"]:
            return 2
    print(
        json.dumps(
            {
                "entry_count": len(entries),
                "manifest_sha256": manifest_hash,
                "status": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
