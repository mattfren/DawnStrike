"""Fail-closed executable identities for Python-hosted production operations."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

APPROVED_GIT_PATH = Path(r"C:\Program Files\Git\cmd\git.exe")
APPROVED_GIT_SHA256 = "37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9"
SAFE_GIT_CONFIGURATION = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "submodule.recurse=false",
)
_FIXED_LOCAL_GIT_CONFIG = {
    "core.repositoryformatversion": "0",
    # Git records the platform-native executable-bit model at repository
    # creation. Keep that value exact for each supported host instead of
    # hard-coding Windows metadata into the Linux CI checkout that
    # independently verifies this production boundary.
    "core.filemode": "false" if os.name == "nt" else "true",
    "core.bare": "false",
    "core.logallrefupdates": "true",
    "core.symlinks": "false",
    "core.ignorecase": "true",
    # actions/checkout disables automatic repository maintenance in its
    # ephemeral checkout. This has no command/path execution surface.
    "gc.auto": "0",
    "remote.origin.fetch": "+refs/heads/*:refs/remotes/origin/*",
    "lfs.repositoryformatversion": "0",
}
_GOVERNED_ORIGIN_URLS = frozenset(
    {
        "https://github.com/mattfren/DawnStrike",
        "https://github.com/mattfren/DawnStrike.git",
    }
)
_NON_EXECUTING_LOCAL_GIT_CONFIG = frozenset({"user.email", "user.name"})
_EXACT_GIT_CONTRACT_ATTRIBUTE = "_dawnstrike_exact_git_contract_v1"
_EXACT_GIT_DESCENDANT_SENTINEL = "DAWNSTRIKE_EXACT_GIT_ADMISSION_REQUIRED"
_EXACT_RELEASE_AUTHORITY_PATHS = frozenset(
    {
        "requirements.lock",
        "config/state_preparation_contract.json",
    }
)


class ApprovedToolError(RuntimeError):
    """A required production executable is missing, redirected, or changed."""


def _is_hex_digest(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )


def admitted_git_contract(root: str | Path) -> Mapping[str, Any] | None:
    """Return the bootstrap's immutable exact-commit snapshot, if installed.

    Presence is authoritative: a malformed or cross-root contract fails closed
    instead of falling back to mutable repository metadata.
    """

    raw = getattr(sys, _EXACT_GIT_CONTRACT_ATTRIBUTE, None)
    if raw is None:
        if os.environ.get(_EXACT_GIT_DESCENDANT_SENTINEL) == "1":
            raise ApprovedToolError(
                "mutable Git is forbidden in an admitted descendant without its own snapshot"
            )
        return None
    if os.environ.get(_EXACT_GIT_DESCENDANT_SENTINEL) != "1":
        raise ApprovedToolError("admitted Git contract sentinel is unavailable")
    if not isinstance(raw, Mapping):
        raise ApprovedToolError("admitted Git contract is malformed")
    expected_root = os.path.normcase(os.path.abspath(root))
    if (
        raw.get("schema_version") != "dawnstrike.exact_git_contract.v1"
        or raw.get("root") != expected_root
    ):
        raise ApprovedToolError("admitted Git contract does not bind this repository")
    if not _is_hex_digest(raw.get("candidate_sha"), 40) or not _is_hex_digest(
        raw.get("candidate_tree"), 40
    ):
        raise ApprovedToolError("admitted Git identity is malformed")
    if raw.get("clean") is not True or not _is_hex_digest(raw.get("git_executable_sha256"), 64):
        raise ApprovedToolError("admitted Git cleanliness contract is malformed")
    origin_url = raw.get("origin_url")
    if origin_url is not None and origin_url not in _GOVERNED_ORIGIN_URLS:
        raise ApprovedToolError("admitted Git origin is not governed")
    origin_main = raw.get("origin_main_sha")
    if origin_main is not None and not _is_hex_digest(origin_main, 40):
        raise ApprovedToolError("admitted origin/main identity is malformed")

    tracked = raw.get("tracked_inventory")
    if not isinstance(tracked, tuple) or not 1 <= len(tracked) <= 4096:
        raise ApprovedToolError("admitted tracked-file inventory is malformed")
    tracked_objects: dict[str, str] = {}
    for entry in tracked:
        if (
            not isinstance(entry, tuple)
            or len(entry) != 3
            or entry[0] not in {"100644", "100755"}
            or not _is_hex_digest(entry[1], 40)
            or not isinstance(entry[2], str)
        ):
            raise ApprovedToolError("admitted tracked-file inventory is malformed")
        relative = PurePosixPath(entry[2])
        if relative.is_absolute() or ".." in relative.parts or entry[2] in tracked_objects:
            raise ApprovedToolError("admitted tracked-file path is unsafe or ambiguous")
        tracked_objects[entry[2]] = entry[1]
    authority_blobs = raw.get("release_authority_blobs")
    if not isinstance(authority_blobs, Mapping):
        raise ApprovedToolError("admitted release authority is malformed")
    if not set(authority_blobs).issubset(_EXACT_RELEASE_AUTHORITY_PATHS):
        raise ApprovedToolError("admitted release authority path is not approved")
    if "requirements.lock" not in authority_blobs:
        raise ApprovedToolError("admitted requirements authority is unavailable")
    for relative, payload in authority_blobs.items():
        if (
            relative not in tracked_objects
            or not isinstance(payload, bytes)
            or len(payload) > 1024 * 1024
        ):
            raise ApprovedToolError("admitted release authority payload is invalid")

    inventory = raw.get("public_web_inventory")
    blobs = raw.get("public_web_blobs")
    if not isinstance(inventory, tuple) or not isinstance(blobs, Mapping):
        raise ApprovedToolError("admitted public web snapshot is malformed")
    names: set[str] = set()
    total_bytes = 0
    if len(inventory) > 128:
        raise ApprovedToolError("admitted public web inventory exceeds its ceiling")
    for entry in inventory:
        if (
            not isinstance(entry, tuple)
            or len(entry) != 3
            or entry[0] not in {"100644", "100755"}
            or not _is_hex_digest(entry[1], 40)
            or not isinstance(entry[2], str)
        ):
            raise ApprovedToolError("admitted public web inventory is malformed")
        relative = PurePosixPath(entry[2])
        if relative.is_absolute() or ".." in relative.parts or entry[2] in names:
            raise ApprovedToolError("admitted public web path is unsafe or ambiguous")
        if entry[2] not in {"web/index.html", "web/favicon.svg"} and not entry[2].startswith(
            "web/assets/"
        ):
            raise ApprovedToolError("admitted public web path is outside its boundary")
        if tracked_objects.get(entry[2]) != entry[1]:
            raise ApprovedToolError("admitted public web path is not tracked exactly")
        payload = blobs.get(entry[2])
        if not isinstance(payload, bytes):
            raise ApprovedToolError("admitted public web blob is unavailable")
        if len(payload) > 16 * 1024 * 1024:
            raise ApprovedToolError("admitted public web blob exceeds its file ceiling")
        total_bytes += len(payload)
        names.add(entry[2])
    if set(blobs) != names or total_bytes > 32 * 1024 * 1024:
        raise ApprovedToolError("admitted public web blob set is invalid")
    return raw


def read_admitted_release_bytes(root: str | Path, relative: str) -> bytes | None:
    """Return one allowlisted exact-commit authority blob without a path read."""

    contract = admitted_git_contract(root)
    if contract is None:
        return None
    if relative not in _EXACT_RELEASE_AUTHORITY_PATHS:
        raise ApprovedToolError("release authority path is not allowlisted")
    payload = contract["release_authority_blobs"].get(relative)
    if not isinstance(payload, bytes):
        raise ApprovedToolError("release authority is absent from the admitted commit")
    return payload


def _captured_git_text(
    contract: Mapping[str, Any], arguments: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    sha = str(contract["candidate_sha"])
    tree = str(contract["candidate_tree"])
    outputs = {
        ("rev-parse", "HEAD"): f"{sha}\n",
        ("rev-parse", "HEAD^{tree}"): f"{tree}\n",
        ("rev-parse", f"{sha}^{{tree}}"): f"{tree}\n",
        ("status", "--porcelain", "--untracked-files=all"): "",
        (
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ): "",
    }
    if contract.get("origin_main_sha") is not None:
        outputs[("rev-parse", "refs/remotes/origin/main")] = f"{contract['origin_main_sha']}\n"
    if contract.get("origin_url") is not None:
        outputs[("remote", "get-url", "origin")] = f"{contract['origin_url']}\n"
    if arguments not in outputs:
        raise ApprovedToolError("Git command is not covered by the admitted snapshot")
    return subprocess.CompletedProcess(list(arguments), 0, outputs[arguments], "")


def _captured_git_bytes(contract: Mapping[str, Any], arguments: tuple[str, ...]) -> bytes:
    sha = str(contract["candidate_sha"])
    inventory = contract["public_web_inventory"]
    blobs = contract["public_web_blobs"]
    if arguments == (
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        sha,
        "--",
        "web/assets",
    ):
        return b"".join(
            f"{entry[2]}\0".encode() for entry in inventory if entry[2].startswith("web/assets/")
        )
    if len(arguments) == 2 and arguments[0] == "show":
        revision, separator, relative = arguments[1].partition(":")
        if separator == ":" and revision == sha and relative in blobs:
            return blobs[relative]
    raise ApprovedToolError("Git byte command is not covered by the admitted snapshot")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_no_reparse(path: Path) -> None:
    cursor = path
    while True:
        details = cursor.lstat()
        if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
            raise ApprovedToolError(f"approved tool path contains a reparse point: {path}")
        if cursor.parent == cursor:
            return
        cursor = cursor.parent


def approved_git_path() -> Path:
    if os.name != "nt":
        discovered = shutil.which("git")
        if not discovered:
            raise ApprovedToolError("Git is unavailable")
        return Path(discovered)
    configured = os.environ.get("DAWNSTRIKE_APPROVED_GIT_PATH", "").strip()
    path = Path(configured) if configured else APPROVED_GIT_PATH
    if str(path).casefold() != str(APPROVED_GIT_PATH).casefold():
        raise ApprovedToolError("approved Git path override is not exact")
    try:
        _assert_no_reparse(path)
        if not path.is_file() or _sha256(path) != APPROVED_GIT_SHA256:
            raise ApprovedToolError("approved Git executable identity changed")
    except OSError as exc:
        raise ApprovedToolError("approved Git executable is unavailable") from exc
    return path


def sanitized_git_environment(root: str | Path) -> dict[str, str]:
    resolved_root = Path(root).resolve(strict=True)
    git_dir = resolved_root / ".git"
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ATTR_NOSYSTEM": "1",
        }
    )
    if git_dir.is_dir():
        environment.update(
            {
                "GIT_DIR": str(git_dir),
                "GIT_COMMON_DIR": str(git_dir),
                "GIT_WORK_TREE": str(resolved_root),
            }
        )
    return environment


def _assert_local_git_config_safe(root: Path) -> None:
    try:
        completed = subprocess.run(  # noqa: S603 - exact executable is verified
            [
                str(approved_git_path()),
                *SAFE_GIT_CONFIGURATION,
                "-C",
                str(root),
                "config",
                "--local",
                "--no-includes",
                "--null",
                "--list",
            ],
            cwd=root,
            env=sanitized_git_environment(root),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApprovedToolError("local Git configuration is unavailable") from exc
    seen: set[str] = set()
    for record in completed.stdout.split("\0"):
        if not record:
            continue
        try:
            key, value = record.split("\n", 1)
        except ValueError as exc:
            raise ApprovedToolError("local Git configuration is malformed") from exc
        key = key.casefold()
        if key in seen:
            raise ApprovedToolError("local Git configuration contains a duplicate key")
        seen.add(key)
        if key == "remote.origin.url":
            if value not in _GOVERNED_ORIGIN_URLS:
                raise ApprovedToolError(
                    "local Git configuration is not governed: remote.origin.url"
                )
            continue
        if key in _FIXED_LOCAL_GIT_CONFIG:
            if value != _FIXED_LOCAL_GIT_CONFIG[key]:
                raise ApprovedToolError(f"local Git configuration is not governed: {key}")
            continue
        if key in _NON_EXECUTING_LOCAL_GIT_CONFIG and 1 <= len(value) <= 512:
            continue
        if key.startswith("branch.") and key.endswith((".remote", ".merge")):
            branch, field = key[len("branch.") :].rsplit(".", 1)
            expected = "origin" if field == "remote" else f"refs/heads/{branch}"
            if branch and value == expected:
                continue
        raise ApprovedToolError(f"local Git configuration key is not governed: {key}")


def run_git(
    root: str | Path,
    *arguments: str,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    contract = admitted_git_contract(root)
    if contract is not None:
        return _captured_git_text(contract, tuple(arguments))
    resolved_root = Path(root).resolve()
    _assert_local_git_config_safe(resolved_root)
    completed = subprocess.run(  # noqa: S603 - executable is exact and hash-verified on Windows
        [
            str(approved_git_path()),
            *SAFE_GIT_CONFIGURATION,
            "-C",
            str(resolved_root),
            *arguments,
        ],
        cwd=resolved_root,
        env=sanitized_git_environment(resolved_root),
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    _assert_local_git_config_safe(resolved_root)
    return completed


def read_git_bytes(
    root: str | Path,
    *arguments: str,
    timeout: int = 30,
    max_bytes: int = 16 * 1024 * 1024,
) -> bytes:
    """Run exact Git and return bounded raw stdout without text transcoding."""

    contract = admitted_git_contract(root)
    if contract is not None:
        output = _captured_git_bytes(contract, tuple(arguments))
        if len(output) > max_bytes:
            raise ApprovedToolError("Git raw output exceeds the governed byte ceiling")
        return output
    resolved_root = Path(root).resolve()
    _assert_local_git_config_safe(resolved_root)
    completed = subprocess.run(  # noqa: S603 - exact executable is verified on Windows
        [
            str(approved_git_path()),
            *SAFE_GIT_CONFIGURATION,
            "-C",
            str(resolved_root),
            *arguments,
        ],
        cwd=resolved_root,
        env=sanitized_git_environment(resolved_root),
        check=True,
        capture_output=True,
        text=False,
        timeout=timeout,
    )
    if len(completed.stdout) > max_bytes:
        raise ApprovedToolError("Git raw output exceeds the governed byte ceiling")
    _assert_local_git_config_safe(resolved_root)
    return completed.stdout
