"""Stamp the static UI audit route inventory with the shared Apex Pro shell."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from intraday_scanner.dashboard.route_hardening import harden_routes

DEFAULT_MANIFEST = Path("dawnstrike_extreme_route_manifest_codex.json")
DEFAULT_REPORT = Path("data/ui_audit_route_hardening_report.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harden every static route in the Dawnstrike UI audit inventory."
    )
    parser.add_argument("--repo-root", default=".", help="Repository root.")
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
        help="Route manifest JSON to harden.",
    )
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="JSON report output path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    manifest_path = (repo_root / args.manifest).resolve()
    report_path = (repo_root / args.report).resolve()
    if not manifest_path.exists():
        raise SystemExit(f"Manifest not found: {manifest_path}")

    results = harden_routes(repo_root=repo_root, manifest_path=manifest_path)
    changed = sum(1 for item in results if item.changed)
    payload = {
        "manifest": str(manifest_path.relative_to(repo_root)),
        "route_count": len(results),
        "changed_count": changed,
        "unchanged_count": len(results) - changed,
        "boundaries": _count_by(results, "boundary"),
        "families": _count_by(results, "family"),
        "rows": [item.__dict__ for item in results],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        "hardened "
        f"{payload['route_count']} routes "
        f"({payload['changed_count']} changed, {payload['unchanged_count']} unchanged)"
    )
    return 0


def _count_by(items: list[object], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(getattr(item, attribute))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())
