"""Fail closed when a generated public artifact contains sensitive internals.

This is deliberately a content gate, not a sanitization tool: an unsafe artifact
must be rebuilt from a safe, explicit public DTO rather than redacted at deploy
time. It never prints a matched value.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

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
            r"(?i)(?:(?<![a-z0-9])[a-z]:\\(?!/)|/(?:home|users|var|private|tmp)/|"
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

_HTML_MARKUP = re.compile(r"<\s*/?\s*[a-z][^>]*>", re.IGNORECASE)
_LINK_ATTRIBUTES = frozenset({"action", "formaction", "href", "src"})


class _PublicLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rules: set[str] = set()

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del tag
        for name, value in attrs:
            if name.lower() not in _LINK_ATTRIBUTES or not value:
                continue
            self.rules.update(_external_url_rules(value, prefix="public_link"))

    handle_startendtag = handle_starttag


def _external_url_rules(value: str, *, prefix: str) -> set[str]:
    candidate = value.strip()
    if not candidate:
        return set()
    if candidate.startswith("//"):
        return {f"{prefix}_not_https"}
    if candidate.startswith(("#", "/", "./", "../", "?")):
        return set()
    if len(candidate) > 2048 or re.search(r"[\x00-\x1f\x7f]", candidate):
        return {f"{prefix}_malformed"}
    if not re.match(r"(?i)^https://", candidate):
        return {f"{prefix}_not_https"}
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
    except ValueError:
        return {f"{prefix}_malformed"}
    rules: set[str] = set()
    if parsed.scheme.lower() != "https":
        rules.add(f"{prefix}_not_https")
    if not hostname:
        rules.add(f"{prefix}_host_missing")
    if parsed.username is not None or parsed.password is not None:
        rules.add(f"{prefix}_credentials")
    return rules


def _walk_json(value: object) -> Sequence[tuple[str | None, object]]:
    rows: list[tuple[str | None, object]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            rows.append((str(key), child))
            rows.extend(_walk_json(child))
    elif isinstance(value, list):
        for child in value:
            rows.append((None, child))
            rows.extend(_walk_json(child))
    return rows


def _structured_content_rules(path: Path, content: str) -> set[str]:
    rules: set[str] = set()
    if path.suffix.lower() == ".html":
        parser = _PublicLinkParser()
        parser.feed(content)
        rules.update(parser.rules)
    if path.suffix.lower() != ".json":
        return rules
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return rules
    scenario_payload = path.name.lower() == "scenarios.json" or (
        isinstance(payload, dict)
        and str(payload.get("schema_version") or "").startswith("dawnstrike-scenarios")
    )
    for key, value in _walk_json(payload):
        if key and key.lower().endswith("source_url") and isinstance(value, str) and value:
            rules.update(_external_url_rules(value, prefix="scenario_source_url"))
        if scenario_payload and isinstance(value, str) and _HTML_MARKUP.search(value):
            rules.add("scenario_raw_html_markup")
    return rules


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
        for rule in sorted(_structured_content_rules(path, content)):
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
