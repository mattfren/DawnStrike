"""Run a Dawnstrike module or script from one explicit release root.

Scheduled Python is always invoked with ``-I -B -S``.  The ``-S`` switch is
intentional: global ``.pth`` files and editable installs are not release
authority.  This tiny stdlib-only bootstrap then inserts only the materialized
release root and proves that ``intraday_scanner`` resolves from that root
before dispatching the requested module/script.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import runpy
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import NoReturn

_APPROVED_GIT = Path(r"C:\Program Files\Git\cmd\git.exe")
_APPROVED_GIT_SHA256 = (
    "37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9"  # pragma: allowlist secret
)
_FORBIDDEN_IGNORED_SUFFIXES = {
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".ps1",
    ".psm1",
    ".pth",
    ".py",
    ".pyc",
    ".pyd",
    ".sh",
}
_FORBIDDEN_IGNORED_NAMES = {"sitecustomize.py", "usercustomize.py"}


def _is_reparse(path: Path) -> bool:
    """Reject Windows junctions/reparse points as well as POSIX symlinks."""

    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _fail(message: str) -> NoReturn:
    raise RuntimeError(message)


def _isolated_git_env() -> dict[str, str]:
    blocked = {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"}
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in blocked and not key.upper().startswith("GIT_")
    }
    # Do not allow machine/global Git configuration to add filters, hooks, or
    # other behavior to the release identity check.
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    )
    return env


def _git(root: Path, *args: str) -> str:
    try:
        digest = hashlib.sha256(_APPROVED_GIT.read_bytes()).hexdigest()
    except OSError as exc:
        _fail(f"approved Git is unavailable: {exc}")
    if digest != _APPROVED_GIT_SHA256:
        _fail("approved Git hash changed")
    try:
        result = subprocess.run(
            [
                str(_APPROVED_GIT),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(root),
                *args,
            ],
            cwd=str(root),
            env=_isolated_git_env(),
            capture_output=True,
            text=False,
            check=False,
        )
    except OSError as exc:
        _fail(f"approved Git execution failed: {exc}")
    if result.returncode != 0:
        _fail("exact release Git identity check failed")
    return result.stdout.decode("utf-8", "strict")


def _assert_exact_source(root: Path, expected_sha: str) -> None:
    if len(expected_sha) != 40 or any(char not in "0123456789abcdef" for char in expected_sha):
        _fail("expected release SHA is invalid")
    git_dir = root / ".git"
    if not git_dir.is_dir() or _is_reparse(git_dir):
        _fail("release root is not a self-contained Git checkout")
    top = _git(root, "rev-parse", "--show-toplevel").strip()
    if Path(top).resolve(strict=True) != root:
        _fail("Git checkout root does not match the release root")
    head = _git(root, "rev-parse", "HEAD").strip().lower()
    if head != expected_sha:
        _fail("release HEAD does not match expected candidate SHA")
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        markers = sorted({line[:2] for line in status.splitlines() if line})
        _fail(f"release checkout is not clean (porcelain markers: {','.join(markers)})")
    ignored = _git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    for relative in ignored.split("\0"):
        if not relative:
            continue
        path = Path(relative)
        if (
            path.suffix.lower() in _FORBIDDEN_IGNORED_SUFFIXES
            or path.name.lower() in _FORBIDDEN_IGNORED_NAMES
        ):
            _fail("release checkout contains an ignored executable or startup artifact")
    flags = _git(root, "ls-files", "-v", "-z")
    entries = flags.split("\0")
    if any(entry and entry[0] in "hSs" for entry in entries):
        _fail("release checkout contains hidden Git index entries")
    if _git(root, "replace", "-l").strip():
        _fail("release checkout contains Git replace refs")
    config_path = root / ".git" / "config"
    try:
        local_config = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"release checkout Git config is unavailable: {exc}")
    if re.search(
        r"(?im)^\s*\[\s*filter(?:\s|\])|^\s*(?:attributesfile|hookspath|path)\s*=",
        local_config,
    ):
        _fail("release checkout contains a Git execution/filter configuration")
    diff = subprocess.run(
        [
            str(_APPROVED_GIT),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(root),
            "diff-index",
            "--quiet",
            "HEAD",
            "--",
        ],
        cwd=str(root),
        env=_isolated_git_env(),
        capture_output=True,
        check=False,
    )
    if diff.returncode != 0:
        _fail("release checkout differs from exact HEAD")


def _release_root(raw: str) -> Path:
    root = Path(raw).resolve(strict=True)
    expected = Path(__file__).resolve().parents[1]
    if root != expected or not root.is_dir():
        raise RuntimeError("release bootstrap root is not the materialized bootstrap parent")
    package = root / "intraday_scanner"
    if (
        _is_reparse(package)
        or _is_reparse(package / "__init__.py")
        or _is_reparse(root / "pyproject.toml")
        or not (package / "__init__.py").is_file()
        or not (root / "pyproject.toml").is_file()
    ):
        raise RuntimeError("release bootstrap root is incomplete")
    return root


def _assert_package_from(root: Path) -> None:
    spec = importlib.util.find_spec("intraday_scanner")
    expected = (root / "intraday_scanner" / "__init__.py").resolve(strict=True)
    if spec is None or spec.origin is None or Path(spec.origin).resolve(strict=True) != expected:
        raise RuntimeError("intraday_scanner did not resolve from the exact release root")


def _append_governed_dependencies() -> None:
    """Expose only the pinned interpreter's dependency directories.

    The -S switch intentionally prevents Python from running site.py. That
    means the normal site-packages directories are absent from sys.path;
    append the interpreter's own purelib/platlib directories explicitly so
    installed dependencies remain usable without executing any global .pth
    file or editable-install finder. The release root is inserted first by
    main and therefore remains the package authority.
    """

    paths = sysconfig.get_paths()
    dependency_paths = set()
    for name in ("purelib", "platlib"):
        raw_dependency = Path(paths[name]) if paths.get(name) else None
        if raw_dependency is None:
            continue
        if _is_reparse(raw_dependency) or any(
            _is_reparse(parent) for parent in raw_dependency.parents
        ):
            raise RuntimeError("interpreter dependency path contains a reparse point")
        dependency_paths.add(raw_dependency.resolve(strict=True))
    prefix = Path(sysconfig.get_config_var("prefix") or sys.prefix).resolve(strict=True)
    for dependency in sorted(dependency_paths, key=str):
        if (
            not dependency.is_dir()
            or _is_reparse(dependency)
            or prefix not in dependency.parents
            or any(_is_reparse(parent) for parent in dependency.parents)
        ):
            raise RuntimeError("interpreter dependency path is outside the approved prefix")
        # -S suppresses .pth execution, but a reparse point or startup file in
        # the approved dependency directory would still let imports escape the
        # pinned interpreter boundary.
        for child in dependency.iterdir():
            if _is_reparse(child) or child.name.lower() in {"sitecustomize.py", "usercustomize.py"}:
                raise RuntimeError("interpreter dependency directory contains an unsafe startup link")
        text = str(dependency)
        if text not in sys.path:
            sys.path.append(text)


def _parse_bootstrap_args(argv: list[str] | None) -> tuple[argparse.Namespace, list[str]]:
    """Parse bootstrap flags without consuming the target's own options."""

    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--release-root", required=True)
    parser.add_argument("--expected-sha", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--module")
    target.add_argument("--script")
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw:
        separator = raw.index("--")
        bootstrap_args = raw[:separator]
        target_args = raw[separator + 1 :]
        return parser.parse_args(bootstrap_args), target_args
    # Backwards-compatible direct invocation. add_help=False ensures a target
    # module's --help is returned as remainder rather than causing the
    # bootstrap parser to exit before dispatch.
    return parser.parse_known_args(raw)


def main(argv: list[str] | None = None) -> int:
    args, remainder = _parse_bootstrap_args(argv)
    root = _release_root(args.release_root)
    _assert_exact_source(root, args.expected_sha)
    sys.path.insert(0, str(root))
    _append_governed_dependencies()
    _assert_package_from(root)
    if args.module:
        sys.argv = [args.module, *remainder]
        runpy.run_module(args.module, run_name="__main__")
    else:
        script = Path(args.script).resolve(strict=True)
        if root not in script.parents:
            raise RuntimeError("release bootstrap script is outside the exact release root")
        sys.argv = [str(script), *remainder]
        runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
