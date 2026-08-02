"""Fail closed when a generated public artifact contains sensitive internals.

This is deliberately a content gate, not a sanitization tool: an unsafe artifact
must be rebuilt from a safe, explicit public DTO rather than redacted at deploy
time. It never prints a matched value.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

MAX_TEXT_BYTES = 5_000_000
TEXT_SUFFIXES = {
    ".css",
    ".csv",
    ".html",
    ".js",
    ".json",
    ".map",
    ".md",
    ".txt",
    ".xml",
}


class Violation(NamedTuple):
    path: Path
    rule: str


_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "local_or_runtime_path",
        re.compile(
            r"(?i)(?:[a-z]:\\|/(?:home|users|var|private|tmp)/|"
            r"dawnstrike-(?:state|runtime|terra-v6))"
        ),
    ),
    (
        "raw_holdout_identifier",
        re.compile(
            r"(?i)\b(?:holdout_(?:evaluation|experiment)_id|evidence_hash_sha256)"
            r"\s*(?:[\"']\s*)?[:=]\s*[\"']?(?!REDACTED\b)[^\s\",'}]{8,}"
        ),
    ),
    (
        "credential_value",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|secret|password|"
            r"telegram[_-]?(?:bot[_-]?)?token)\b\s*(?:[\"']\s*)?[:=]\s*"
            r"[\"']?[a-z0-9_\-]{12,}"
        ),
    ),
    (
        "telegram_bot_url",
        re.compile(r"(?i)api\.telegram\.org/bot\d{6,}:[a-z0-9_-]{20,}"),
    ),
)


def scan_public_artifact(root: Path) -> list[Violation]:
    """Return policy violations without returning the sensitive matched text."""

    if not root.exists() or not root.is_dir():
        raise ValueError(f"Public artifact root does not exist or is not a directory: {root}")

    violations: list[Violation] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > MAX_TEXT_BYTES:
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for rule, pattern in _RULES:
            if pattern.search(content):
                violations.append(Violation(path=path, rule=rule))
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Generated public artifact directory.")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        violations = scan_public_artifact(root)
    except ValueError as exc:
        parser.error(str(exc))
    if violations:
        for violation in violations:
            print(f"PUBLIC_ARTIFACT_SECURITY_VIOLATION rule={violation.rule} file={violation.path}")
        return 1
    print(f"PUBLIC_ARTIFACT_SECURITY_OK root={root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
