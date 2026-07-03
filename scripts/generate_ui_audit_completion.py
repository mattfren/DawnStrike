"""Generate the Dawnstrike UI audit completion ledger."""

from __future__ import annotations

import argparse
from pathlib import Path

from intraday_scanner.dashboard.ui_audit_ledger import (
    build_ledger_rows,
    ledger_summary,
    write_ledger_json,
    write_ledger_markdown,
)

DEFAULT_MANIFEST = Path("dawnstrike_extreme_route_manifest_codex.json")
DEFAULT_MARKDOWN = Path("UI_AUDIT_COMPLETION.md")
DEFAULT_JSON = Path("data/ui_audit_completion_ledger.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build UI_AUDIT_COMPLETION.md from the latest Dawnstrike route manifest."
    )
    parser.add_argument("--repo-root", default=".", help="Repository root.")
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
        help="Visual route manifest JSON. Defaults to the latest 825-row visual audit manifest.",
    )
    parser.add_argument("--markdown", default=str(DEFAULT_MARKDOWN), help="Markdown output path.")
    parser.add_argument("--json", default=str(DEFAULT_JSON), help="JSON output path.")
    parser.add_argument(
        "--no-discover-extras",
        action="store_true",
        help="Only include manifest rows; skip current repo HTML route discovery.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    manifest_path = (repo_root / args.manifest).resolve()
    markdown_path = (repo_root / args.markdown).resolve()
    json_path = (repo_root / args.json).resolve()

    if not manifest_path.exists():
        raise SystemExit(f"Manifest not found: {manifest_path}")

    rows = build_ledger_rows(
        repo_root=repo_root,
        manifest_path=manifest_path,
        include_discovered_extras=not args.no_discover_extras,
    )
    write_ledger_json(rows, json_path)
    write_ledger_markdown(
        rows,
        markdown_path,
        manifest_path=manifest_path.relative_to(repo_root),
        json_path=json_path.relative_to(repo_root),
    )

    summary = ledger_summary(rows)
    print(
        "wrote "
        f"{markdown_path} with {summary['row_count']} rows "
        f"({summary['manifest_count']} manifest, {summary['repo_extra_count']} repo-extra)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
