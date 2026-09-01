"""Run a Dawnstrike module or script from one explicit release root.

Scheduled Python is always invoked with ``-I -B -S``.  The ``-S`` switch is
intentional: global ``.pth`` files and editable installs are not release
authority.  This tiny stdlib-only bootstrap then inserts only the materialized
release root and proves that ``intraday_scanner`` resolves from that root
before dispatching the requested module/script.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import importlib.util
import os
import re
import runpy
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path, PurePosixPath
from typing import NoReturn

_APPROVED_GIT = Path(r"C:\Program Files\Git\cmd\git.exe")
_APPROVED_GIT_SHA256 = (
    "37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9"  # pragma: allowlist secret
)
_APPROVED_DISTRIBUTION_RECORD_SET_SHA256 = (
    "447a0d12feffcfd6c353d9acb4cfd1e5cc1b35e3548cd7e9ad58666516b4b3af"  # pragma: allowlist secret
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


def _normalized_git_blob_sha1(path: Path) -> str:
    body = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha1(
        b"blob " + str(len(body)).encode("ascii") + b"\0" + body,
        usedforsecurity=False,
    ).hexdigest()


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
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ATTR_NOSYSTEM": "1",
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
                "core.autocrlf=true",
                "-c",
                "core.hooksPath=NUL",
                "-c",
                "core.attributesFile=NUL",
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


def _validated_git_metadata(root: Path) -> dict[Path, bytes]:
    """Read the effective local Git config without executing Git-controlled code."""

    dot_git = root / ".git"
    metadata_files: list[Path] = []
    if _is_reparse(dot_git):
        _fail("release Git metadata is a reparse point")
    if dot_git.is_file():
        try:
            pointer_bytes = dot_git.read_bytes()
            pointer_text = pointer_bytes.decode("utf-8", "strict")
        except (OSError, UnicodeError) as exc:
            _fail(f"release Git worktree pointer is unreadable: {exc}")
        match = re.fullmatch(r"\s*gitdir:\s*([^\r\n]+?)\s*", pointer_text)
        if match is None:
            _fail("release Git worktree pointer is invalid")
        raw_git_dir = Path(match.group(1).strip())
        git_dir = (
            raw_git_dir if raw_git_dir.is_absolute() else root / raw_git_dir
        ).resolve(strict=True)
        metadata_files.append(dot_git)
    elif dot_git.is_dir():
        git_dir = dot_git.resolve(strict=True)
    else:
        _fail("release root has no valid Git metadata")
    if not git_dir.is_dir() or _is_reparse(git_dir):
        _fail("release Git directory is invalid")

    common_dir = git_dir
    common_pointer = git_dir / "commondir"
    if common_pointer.is_file():
        if _is_reparse(common_pointer):
            _fail("release Git common-dir pointer is a reparse point")
        try:
            common_bytes = common_pointer.read_bytes()
            common_text = common_bytes.decode("utf-8", "strict")
        except (OSError, UnicodeError) as exc:
            _fail(f"release Git common-dir pointer is unreadable: {exc}")
        match = re.fullmatch(r"\s*([^\r\n]+?)\s*", common_text)
        if match is None:
            _fail("release Git common-dir pointer is invalid")
        raw_common = Path(match.group(1).strip())
        common_dir = (
            raw_common if raw_common.is_absolute() else git_dir / raw_common
        ).resolve(strict=True)
        metadata_files.append(common_pointer)
    if not common_dir.is_dir() or _is_reparse(common_dir):
        _fail("release Git common directory is invalid")
    for attributes_path in {git_dir / "info" / "attributes", common_dir / "info" / "attributes"}:
        if attributes_path.exists():
            _fail("release checkout contains an ungoverned Git attributes file")

    config_paths = [common_dir / "config"]
    for candidate in (git_dir / "config.worktree", common_dir / "config.worktree"):
        if candidate not in config_paths and candidate.is_file():
            config_paths.append(candidate)
    config_texts: list[str] = []
    for config_path in config_paths:
        if not config_path.is_file() or _is_reparse(config_path):
            _fail("release checkout Git config is unavailable")
        try:
            config_texts.append(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            _fail(f"release checkout Git config is unavailable: {exc}")
        metadata_files.append(config_path)
    local_config = "\n".join(config_texts)
    if re.search(
        r"(?im)^\s*\[\s*(?:filter|url|protocol|include|credential|http)(?:\s|\])"
        r"|^\s*(?:attributesfile|hookspath|path|sshcommand|proxy|helper|command)\s*=",
        local_config,
    ):
        _fail("release checkout contains a Git execution or transport configuration")
    try:
        return {path: path.read_bytes() for path in metadata_files}
    except OSError as exc:
        _fail(f"release Git metadata is unreadable: {exc}")


def _assert_exact_source(root: Path, expected_sha: str) -> None:
    if len(expected_sha) != 40 or any(char not in "0123456789abcdef" for char in expected_sha):
        _fail("expected release SHA is invalid")
    metadata_snapshots = _validated_git_metadata(root)
    attributes_path = root / ".gitattributes"
    if not attributes_path.is_file() or _is_reparse(attributes_path):
        _fail("release checkout has no regular governed .gitattributes")
    top = _git(root, "rev-parse", "--show-toplevel").strip()
    if Path(top).resolve(strict=True) != root:
        _fail("Git checkout root does not match the release root")
    head = _git(root, "rev-parse", "HEAD").strip().lower()
    if head != expected_sha:
        _fail("release HEAD does not match expected candidate SHA")
    attributes_blob = _git(root, "rev-parse", "HEAD:.gitattributes").strip().lower()
    if attributes_blob != _normalized_git_blob_sha1(attributes_path):
        _fail("release .gitattributes differs from exact HEAD")
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
    diff = subprocess.run(
        [
            str(_APPROVED_GIT),
            "-c",
            "core.autocrlf=true",
            "-c",
            "core.hooksPath=NUL",
            "-c",
            "core.attributesFile=NUL",
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
    try:
        if any(path.read_bytes() != expected for path, expected in metadata_snapshots.items()):
            _fail("release Git metadata changed during source verification")
    except OSError as exc:
        _fail(f"release Git metadata changed during source verification: {exc}")


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


def _append_governed_dependencies() -> tuple[Path, ...]:
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
                raise RuntimeError(
                    "interpreter dependency directory contains an unsafe startup link"
                )
        text = str(dependency)
        if text not in sys.path:
            sys.path.append(text)
    return tuple(sorted(dependency_paths, key=str))


def _locked_requirements(root: Path) -> dict[str, str]:
    """Read exact package pins from the repository's hash-locked manifest."""

    lockfile = root / "requirements.lock"
    if not lockfile.is_file():
        _fail("release checkout has no hash-locked requirements manifest")
    requirements: dict[str, str] = {}
    hashes: set[str] = set()
    current: str | None = None
    for line in lockfile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        match = re.match(
            r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s\\]+)",
            stripped,
        )
        if match is not None:
            name, version = match.groups()
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            if normalized in requirements and requirements[normalized] != version:
                _fail(f"requirements.lock contains conflicting pins for {name}")
            requirements[normalized] = version
            current = normalized
            continue
        if current is not None and re.search(r"--hash=sha256:[0-9a-f]{64}", stripped):
            hashes.add(current)
    if not requirements:
        _fail("requirements.lock contains no exact package pins")
    if hashes != set(requirements):
        _fail("requirements.lock is missing an exact sha256 hash for a package")
    return requirements


def _read_distribution_record(
    dist: importlib.metadata.Distribution, prefix: Path
) -> tuple[
    set[str], dict[str, tuple[bytes, int | None]], set[str], str
]:
    """Parse a locked distribution's anchored RECORD without loading its code."""

    files = dist.files
    if files is None:
        _fail(f"installed dependency {dist.metadata['Name']} has no RECORD")
    record = next((item for item in files if str(item).endswith(".dist-info/RECORD")), None)
    if record is None:
        _fail(f"installed dependency {dist.metadata['Name']} has no RECORD entry")
    record_path = Path(dist.locate_file(record)).resolve(strict=True)
    if prefix not in record_path.parents:
        _fail("installed dependency RECORD escapes the approved interpreter prefix")
    try:
        record_sha256 = hashlib.sha256(record_path.read_bytes()).hexdigest()
        owned_paths: set[str] = set()
        owned_hashes: dict[str, tuple[bytes, int | None]] = {}
        top_level_names: set[str] = set()
        with record_path.open(encoding="utf-8", newline="") as stream:
            rows = csv.reader(stream)
            for row in rows:
                if len(row) != 3:
                    _fail(f"installed dependency {dist.metadata['Name']} has malformed RECORD data")
                relative, hash_spec, size_text = row
                target = Path(os.path.abspath(dist.locate_file(PurePosixPath(relative))))
                if os.path.commonpath((str(target), str(prefix))) != str(prefix):
                    _fail("installed dependency RECORD contains a path outside the approved prefix")
                target_key = os.path.normcase(str(target))
                owned_paths.add(target_key)
                parts = PurePosixPath(relative).parts
                if parts and parts[0] not in {".", ".."}:
                    top = parts[0]
                    if top.endswith((".py", ".pyd")):
                        top = Path(top).stem
                    if top.isidentifier():
                        top_level_names.add(top)
                unhashed_allowed = relative.endswith(".dist-info/RECORD") or relative.endswith(
                    ".pyc"
                )
                if not hash_spec and not unhashed_allowed:
                    _fail(f"installed dependency {dist.metadata['Name']} contains an unhashed file")
                if hash_spec:
                    algorithm, separator, encoded = hash_spec.partition("=")
                    if separator != "=" or algorithm != "sha256" or not encoded:
                        _fail("installed dependency RECORD uses an unapproved digest")
                    try:
                        expected = base64.urlsafe_b64decode(encoded + "===")
                    except (ValueError, binascii.Error):
                        _fail("installed dependency RECORD digest is invalid")
                    try:
                        expected_size = int(size_text) if size_text else None
                    except ValueError:
                        _fail("installed dependency RECORD size is invalid")
                    owned_hashes[target_key] = (expected, expected_size)
        declared_top = dist.read_text("top_level.txt")
        if declared_top:
            top_level_names.update(
                line.strip()
                for line in declared_top.splitlines()
                if line.strip().isidentifier()
            )
        return owned_paths, owned_hashes, top_level_names, record_sha256
    except OSError as exc:
        _fail(f"installed dependency RECORD is unreadable: {exc}")


def _assert_locked_dependencies(
    root: Path, dependency_paths: tuple[Path, ...]
) -> tuple[frozenset[str], frozenset[str], dict[str, tuple[bytes, int | None]]]:
    """Require the actual interpreter environment to match requirements.lock."""

    requirements = _locked_requirements(root)
    prefix = Path(sysconfig.get_config_var("prefix") or sys.prefix).resolve(strict=True)
    installed: dict[str, list[importlib.metadata.Distribution]] = {}
    owned_paths: set[str] = set()
    owned_hashes: dict[str, tuple[bytes, int | None]] = {}
    allowed_top_level: set[str] = set()
    record_contract_rows: list[str] = []
    for dependency in dependency_paths:
        for dist in importlib.metadata.distributions(path=[str(dependency)]):
            name = dist.metadata.get("Name")
            if not name:
                _fail("installed dependency metadata has no package name")
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            installed.setdefault(normalized, []).append(dist)
    for name, version in requirements.items():
        matches = installed.get(name, [])
        if len(matches) != 1 or matches[0].version != version:
            _fail(f"installed dependency does not exactly match requirements.lock: {name}")
        distribution_paths, distribution_hashes, distribution_top_level, record_sha256 = (
            _read_distribution_record(matches[0], prefix)
        )
        owned_paths.update(distribution_paths)
        for path, contract in distribution_hashes.items():
            if path in owned_hashes and owned_hashes[path] != contract:
                _fail("installed dependency RECORD ownership is ambiguous")
            owned_hashes[path] = contract
        allowed_top_level.update(distribution_top_level)
        record_contract_rows.append(f"{name}\0{version}\0{record_sha256}\n")
    record_contract = hashlib.sha256("".join(record_contract_rows).encode()).hexdigest()
    if record_contract != _APPROVED_DISTRIBUTION_RECORD_SET_SHA256:
        _fail("installed dependency RECORD set is not the source-approved runtime contract")
    return frozenset(allowed_top_level), frozenset(owned_paths), owned_hashes


class _LockedDependencyGuard(importlib.abc.MetaPathFinder):
    def __init__(
        self,
        dependency_paths: tuple[Path, ...],
        allowed: frozenset[str],
        owned_paths: frozenset[str],
        owned_hashes: dict[str, tuple[bytes, int | None]],
    ) -> None:
        self._dependency_paths = [str(path) for path in dependency_paths]
        self._allowed = allowed
        self._owned_paths = owned_paths
        self._owned_hashes = owned_hashes

    def find_spec(self, fullname: str, path=None, target=None):  # type: ignore[no-untyped-def]
        if path is None:
            trusted_paths = [entry for entry in sys.path if entry not in self._dependency_paths]
            if importlib.machinery.PathFinder.find_spec(fullname, trusted_paths) is not None:
                return None
            search_paths = self._dependency_paths
        else:
            search_paths = [
                str(entry)
                for entry in path
                if any(
                    os.path.commonpath((str(entry), dependency)) == dependency
                    for dependency in self._dependency_paths
                )
            ]
            if not search_paths:
                return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, search_paths)
        if spec is None:
            return None
        if fullname.split(".", 1)[0] not in self._allowed:
            raise ModuleNotFoundError(f"dependency import is not locked: {fullname}")
        if spec.origin not in {None, "namespace"}:
            origin_path = Path(spec.origin).resolve(strict=True)
            origin = os.path.normcase(str(origin_path))
            if origin not in self._owned_paths:
                raise ModuleNotFoundError(f"dependency import path is not RECORD-owned: {fullname}")
            if origin_path.suffix.lower() in {".pyd", ".dll", ".so"}:
                _read_verified_dependency_bytes(origin_path, self._owned_hashes)
        return None


def _read_verified_dependency_bytes(
    path: Path, owned_hashes: dict[str, tuple[bytes, int | None]]
) -> bytes:
    resolved = path.resolve(strict=True)
    if _is_reparse(resolved) or any(_is_reparse(parent) for parent in resolved.parents):
        _fail("dependency import path contains a reparse point")
    key = os.path.normcase(str(resolved))
    contract = owned_hashes.get(key)
    if contract is None:
        _fail("dependency import has no anchored RECORD digest")
    contents = resolved.read_bytes()
    expected, expected_size = contract
    if expected_size is not None and len(contents) != expected_size:
        _fail("dependency import file size changed")
    if hashlib.sha256(contents).digest() != expected:
        _fail("dependency import file hash changed")
    return contents


class _VerifiedSourceLoader(importlib.machinery.SourceFileLoader):
    """Compile governed dependency source directly; never consume unsealed pyc."""

    owned_hashes: dict[str, tuple[bytes, int | None]] = {}

    def get_code(self, fullname: str):  # type: ignore[no-untyped-def]
        source_path = self.get_filename(fullname)
        source = _read_verified_dependency_bytes(Path(source_path), self.owned_hashes)
        return self.source_to_code(source, source_path)


def _install_verified_dependency_importers(
    dependency_paths: tuple[Path, ...],
    allowed: frozenset[str],
    owned_paths: frozenset[str],
    owned_hashes: dict[str, tuple[bytes, int | None]],
) -> None:
    _VerifiedSourceLoader.owned_hashes = owned_hashes
    governed_file_finder = importlib.machinery.FileFinder.path_hook(
        (_VerifiedSourceLoader, importlib.machinery.SOURCE_SUFFIXES),
        (importlib.machinery.ExtensionFileLoader, importlib.machinery.EXTENSION_SUFFIXES),
    )
    dependency_roots = [os.path.normcase(str(path)) for path in dependency_paths]

    def governed_dependency_path_hook(path: str):  # type: ignore[no-untyped-def]
        normalized = os.path.normcase(os.path.abspath(path))
        if not any(
            os.path.commonpath((normalized, dependency)) == dependency
            for dependency in dependency_roots
        ):
            raise ImportError
        return governed_file_finder(path)

    sys.path_hooks.insert(0, governed_dependency_path_hook)
    path_finder_index = next(
        (
            index
            for index, finder in enumerate(sys.meta_path)
            if finder is importlib.machinery.PathFinder
        ),
        len(sys.meta_path),
    )
    sys.meta_path.insert(
        path_finder_index,
        _LockedDependencyGuard(dependency_paths, allowed, owned_paths, owned_hashes),
    )
    for dependency in dependency_paths:
        sys.path_importer_cache.pop(str(dependency), None)


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
    dependency_paths = _append_governed_dependencies()
    allowed_dependencies, owned_dependency_paths, owned_dependency_hashes = (
        _assert_locked_dependencies(root, dependency_paths)
    )
    _install_verified_dependency_importers(
        dependency_paths,
        allowed_dependencies,
        owned_dependency_paths,
        owned_dependency_hashes,
    )
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
