"""Sandbox isolation for the synthetic E2E rehearsal.

Everything the rehearsal touches - config, fixtures, DBs, locks, emulator
state, artifacts - lives under a unique run directory beneath
``C:\\r\\dsos-00-v3\\e2e\\<run-id>``. This module proves, with assertions
that fail loudly, that:

- the sandbox root itself is not (and cannot resolve to, via symlink or
  junction) a production path;
- any path handed to the harness that resolves inside a production root is
  rejected;
- outbound network access is blocked for everything except one explicitly
  owned loopback emulator socket;
- the modules the rehearsal imports really come from this worktree, not cwd
  or a stale checkout.

Nothing here is imported by production code.
"""

from __future__ import annotations

import os
import secrets
import socket
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

SYNTH_SENTINEL = "SYNTH_SENTINEL_DO_NOT_LEAK_7f3a"

SANDBOX_ROOT = Path(r"C:\r\dsos-00-v3\e2e")

# Real production roots. A canonical (resolved) path landing inside any of
# these - directly, or via a symlink/junction that escapes the sandbox - must
# be rejected before it can be used for output, config, or state.
PRODUCTION_ROOTS: tuple[Path, ...] = (
    Path(r"C:\r\dawnstrike-runtime"),
    Path(r"C:\r\dawnstrike-state"),
    Path(r"C:\r\dawnstrike-audit-20260907"),
)

WORKTREE_ROOT = Path(r"C:\r\ds-e2e-20260921")


class SandboxViolation(RuntimeError):
    """Raised when a path, config value, or network target escapes the sandbox."""


def _run_id() -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"run-{stamp}-{suffix}"


def canonical(path: str | Path) -> Path:
    """Fully resolve a path (following symlinks/junctions) for comparison."""

    return Path(os.path.realpath(str(path)))


def assert_not_production_path(path: str | Path) -> None:
    """Fail closed if ``path`` resolves inside any known production root."""

    resolved = canonical(path)
    for root in PRODUCTION_ROOTS:
        resolved_root = canonical(root) if root.exists() else root
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            continue
        raise SandboxViolation(
            f"path {path!r} resolves to {resolved} which is inside the "
            f"production root {root} - refused"
        )


def assert_inside_sandbox(path: str | Path, sandbox_root: Path) -> None:
    """Fail closed unless ``path`` canonically resolves inside the sandbox root.

    This is what actually catches a junction/symlink planted inside the
    sandbox that points back out at a production directory: ``relative_to``
    is evaluated against the *resolved* target, not the literal path text.
    """

    resolved = canonical(path)
    resolved_root = canonical(sandbox_root)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise SandboxViolation(
            f"path {path!r} resolves to {resolved}, which escapes the sandbox "
            f"root {resolved_root}"
        ) from exc
    assert_not_production_path(path)


@dataclass
class Sandbox:
    run_id: str
    root: Path
    config_dir: Path
    fixtures_dir: Path
    db_dir: Path
    locks_dir: Path
    emulator_dir: Path
    artifacts_dir: Path
    logs_dir: Path
    env_file: Path

    def path(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        assert_inside_sandbox(p, self.root)
        return p


def create_sandbox() -> Sandbox:
    run_id = _run_id()
    root = SANDBOX_ROOT / run_id
    assert_not_production_path(root)
    root.mkdir(parents=True, exist_ok=False)
    assert_inside_sandbox(root, root)

    dirs = {
        "config_dir": root / "config",
        "fixtures_dir": root / "fixtures",
        "db_dir": root / "db",
        "locks_dir": root / "locks",
        "emulator_dir": root / "emulator",
        "artifacts_dir": root / "artifacts",
        "logs_dir": root / "logs",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
        assert_inside_sandbox(d, root)

    env_file = dirs["config_dir"] / "sandbox.env"
    env_file.write_text("# synthetic sandbox env file - intentionally empty\n", encoding="utf-8")

    sandbox = Sandbox(run_id=run_id, root=root, env_file=env_file, **dirs)
    assert_inside_sandbox(sandbox.db_dir, sandbox.root)
    return sandbox


# --------------------------------------------------------------------------
# Environment for app subprocesses: preserve what Windows/PowerShell needs,
# positively prove no credential-bearing name is visible.
# --------------------------------------------------------------------------

_PRESERVE_PREFIXES = (
    "PATH",
    "PATHEXT",
    "PSMODULEPATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "USERNAME",
    "COMPUTERNAME",
    "HOMEDRIVE",
    "HOMEPATH",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PROCESSOR_ARCHITECTURE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PY_PYTHON",
    "PYTHONIOENCODING",
    "VIRTUAL_ENV",
)

# Substrings that flag a name as potentially credential-bearing. Checked
# against every surviving env var name, not just a fixed five-name list.
_CREDENTIAL_MARKERS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "APCA",
    "WEBHOOK",
    "BOT_TOKEN",
)

# Names that contain a credential marker substring but are not credentials,
# so the positive scan does not false-positive on them.
_MARKER_ALLOWLIST = {
    "PUBLIC",  # never set, placeholder for future safe keys
}


def sandboxed_env(sandbox: Sandbox, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the subprocess environment: preserve OS plumbing, drop secrets.

    Positively asserts (not just a denylist) that nothing which *looks* like
    a credential survives, by scanning every surviving name for a marker
    substring rather than checking a fixed list of five known names.
    """

    built: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in _PRESERVE_PREFIXES:
            built[name] = value

    built["DAWNSTRIKE_E2E_SANDBOX_RUN_ID"] = sandbox.run_id
    built["DAWNSTRIKE_E2E_SANDBOX_ROOT"] = str(sandbox.root)
    built["DAWNSTRIKE_TEST_ACTIVE_PATH_GUARD"] = "1"
    if extra:
        built.update(extra)

    offenders = [
        name
        for name in built
        if name.upper() not in _MARKER_ALLOWLIST
        and any(marker in name.upper() for marker in _CREDENTIAL_MARKERS)
    ]
    if offenders:
        raise SandboxViolation(
            f"credential-shaped env names leaked into sandbox env: {offenders}"
        )
    return built


def assert_no_credential_env_visible(env: dict[str, str]) -> None:
    """Positive proof (not a denylist check) that no credential name survives."""

    offenders = [
        name
        for name in env
        if name.upper() not in _MARKER_ALLOWLIST
        and any(marker in name.upper() for marker in _CREDENTIAL_MARKERS)
    ]
    if offenders:
        raise SandboxViolation(f"credential-shaped env names present: {offenders}")


# --------------------------------------------------------------------------
# Network guard: block every outbound socket except the one loopback emulator
# this run explicitly owns.
# --------------------------------------------------------------------------


class NetworkGuard:
    """Monkeypatch-installed guard that blocks all but one loopback target."""

    def __init__(self) -> None:
        self._allowed: set[tuple[str, int]] = set()
        self._original_connect = socket.socket.connect
        self._original_create_connection = socket.create_connection
        self._blocked_attempts: list[tuple[str, int]] = []
        self._installed = False

    def allow(self, host: str, port: int) -> None:
        self._allowed.add((host, port))

    @property
    def blocked_attempts(self) -> list[tuple[str, int]]:
        return list(self._blocked_attempts)

    def install(self) -> None:
        if self._installed:
            return
        guard = self

        def guarded_connect(sock_self, address):  # type: ignore[no-untyped-def]
            target = _address_host_port(address)
            if target is None or target not in guard._allowed:
                guard._blocked_attempts.append(target or ("<unknown>", 0))
                raise SandboxViolation(
                    f"blocked outbound connection to {target!r}; only explicitly "
                    f"allowed loopback targets may be reached during the E2E rehearsal"
                )
            return guard._original_connect(sock_self, address)

        def guarded_create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
            target = _address_host_port(address)
            if target is None or target not in guard._allowed:
                guard._blocked_attempts.append(target or ("<unknown>", 0))
                raise SandboxViolation(
                    f"blocked outbound connection to {target!r}; only explicitly "
                    f"allowed loopback targets may be reached during the E2E rehearsal"
                )
            return guard._original_create_connection(address, *args, **kwargs)

        socket.socket.connect = guarded_connect  # type: ignore[assignment]
        socket.create_connection = guarded_create_connection  # type: ignore[assignment]
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        socket.socket.connect = self._original_connect  # type: ignore[assignment]
        socket.create_connection = self._original_create_connection  # type: ignore[assignment]
        self._installed = False


def _address_host_port(address) -> tuple[str, int] | None:  # type: ignore[no-untyped-def]
    if isinstance(address, tuple) and len(address) >= 2:
        return (str(address[0]), int(address[1]))
    return None


def assert_modules_from_worktree(modules: Iterable[object], worktree: Path = WORKTREE_ROOT) -> None:
    """Fail closed unless every module's ``__file__`` is under this worktree."""

    resolved_worktree = canonical(worktree)
    offenders = []
    for module in modules:
        file_attr = getattr(module, "__file__", None)
        if not file_attr:
            offenders.append((getattr(module, "__name__", repr(module)), None))
            continue
        resolved = canonical(file_attr)
        try:
            resolved.relative_to(resolved_worktree)
        except ValueError:
            offenders.append((module.__name__, str(resolved)))
    if offenders:
        raise SandboxViolation(
            f"modules not loaded from worktree {resolved_worktree}: {offenders}"
        )
