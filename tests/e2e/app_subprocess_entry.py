"""Test-owned OS-process entrypoint for scenarios H (lost ack + restart) and
J (competing writers against the paper-session CLI).

This is not production code and nothing under ``intraday_scanner/`` imports
it. It exists because those scenarios require killing and restarting a real
child process, not an in-process pytest call - so the real
``intraday_scanner.cli paper-session`` command has to run as its own OS
process with its own PID.

``paper_broker.PAPER_BASE`` is a module-level constant with no environment
override in production, and this harness must never edit production files
for testability. So the redirect to the loopback fake-Alpaca emulator is
done here, in a test-owned bootstrap, using the exact same monkeypatch shape
``test_synthetic_rehearsal.py::_patch_broker_to_loopback`` uses in-process -
only the loopback host:port may ever be reached; anything else raises.

Usage: py -3.13 app_subprocess_entry.py <normal paper-session CLI argv...>
Requires env vars DAWNSTRIKE_E2E_EMULATOR_HOST / _PORT, plus
ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY (synthetic, never real).
"""

from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request

_REPO_ROOT = os.environ["DAWNSTRIKE_E2E_REPO_ROOT"]
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import intraday_scanner.execution.paper_broker as ds_paper_broker  # noqa: E402

_HOST = os.environ["DAWNSTRIKE_E2E_EMULATOR_HOST"]
_PORT = int(os.environ["DAWNSTRIKE_E2E_EMULATOR_PORT"])

ds_paper_broker.PAPER_BASE = f"http://{_HOST}:{_PORT}"
ds_paper_broker.PAPER_HOST = _HOST


def _loopback_open(target, *, timeout, allowed_hosts, allow_http=False):  # noqa: ANN001
    url = target.full_url if isinstance(target, urllib.request.Request) else target
    parsed = urllib.parse.urlsplit(url)
    if (parsed.hostname, parsed.port) != (_HOST, _PORT):
        raise RuntimeError(
            f"subprocess loopback transport refuses non-emulator target {url!r}"
        )
    opener = urllib.request.build_opener()
    return opener.open(target, timeout=timeout)


ds_paper_broker.open_allowlisted_url = _loopback_open

from intraday_scanner import cli as ds_cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(ds_cli.main(sys.argv[1:]))
