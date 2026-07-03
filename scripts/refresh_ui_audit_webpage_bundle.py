"""Refresh a UI audit webpage bundle from the current source HTML files."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

DEFAULT_BASE_BUNDLE = Path("data/ui_ux_audit_webpages_20260703T094955")
DEFAULT_MANIFEST = Path("dawnstrike_extreme_route_manifest_codex.json")
DEFAULT_OUT = Path("data/ui_ux_audit_webpages_hardened_20260703T1150")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy the existing UI audit bundle and refresh its HTML from current source files."
        )
    )
    parser.add_argument("--repo-root", default=".", help="Repository root.")
    parser.add_argument("--base-bundle", default=str(DEFAULT_BASE_BUNDLE))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete the output bundle before copying.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    base_bundle = (repo_root / args.base_bundle).resolve()
    manifest_path = (repo_root / args.manifest).resolve()
    out = (repo_root / args.out).resolve()
    _assert_within_repo(repo_root, out)
    if not base_bundle.exists():
        raise SystemExit(f"Base bundle not found: {base_bundle}")
    if not manifest_path.exists():
        raise SystemExit(f"Manifest not found: {manifest_path}")
    if out.exists() and args.replace:
        shutil.rmtree(out)
    shutil.copytree(base_bundle, out, dirs_exist_ok=True)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = []
    for row in manifest["rows"]:
        source = str(row["source"]).replace("\\", "/")
        audit_path = str(row["audit_path"]).replace("\\", "/")
        source_path = repo_root / source
        target_path = out / audit_path
        if not source_path.exists():
            raise SystemExit(f"Source route missing: {source}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        text = source_path.read_text(encoding="utf-8")
        target_path.write_text(text, encoding="utf-8")
        refreshed = dict(row)
        refreshed["title"] = _extract_tag(text, "title")
        refreshed["h1"] = _extract_tag(text, "h1")
        refreshed["size_bytes"] = target_path.stat().st_size
        rows.append(refreshed)

    asset_count = sum(
        1
        for path in (out / "site").rglob("*")
        if path.is_file() and path.suffix != ".html"
    )
    payload = {
        "html_count": len(rows),
        "asset_count": asset_count,
        "source_root": str(repo_root),
        "bundle_root": str(out),
        "base_bundle": str(base_bundle),
        "rows": rows,
    }
    (out / "manifest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"refreshed {len(rows)} html routes into {out}")
    return 0


def _extract_tag(text: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", text, flags=re.I | re.S)
    if not match:
        return ""
    value = re.sub(r"<[^>]+>", " ", match.group(1))
    return re.sub(r"\s+", " ", value).strip()


def _assert_within_repo(repo_root: Path, target: Path) -> None:
    target.relative_to(repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
