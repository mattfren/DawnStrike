# Phase Two — clean-environment build and reproducibility

- Commit: `14ca714f6df37a5769119547d01d77527baeb5c5`
- Clean venv: `C:\r\dawnstrike-cleanenv-20260904` (created fresh; the developer environment was not modified)
- Interpreter: Python 3.13.15
- Raw log: `evidence/clean_install.txt`

## Procedure followed

Exactly the procedure documented in `README.md`:

```powershell
py -m pip install -e ".[dev]"
```

`INSTALL_EXIT=0` — the install itself succeeds. Dependency resolution completes, the editable
wheel builds, and `intraday_scanner` imports.

## Result: the documented install cannot produce a suite-passing environment

The documented command resolves dependencies from PyPI, **not** from the repository's own
`requirements.lock`. Nothing in the documented path consults the lock file.

| Package | `requirements.lock` pin | Resolved by documented install |
|---|---|---|
| `anyio` | `==4.14.2` (`requirements.lock:15`) | **4.15.0** |
| `openai` | (lock pin) | 3.8.0 |
| `ruff` | (lock pin) | 0.16.6 |

Running the repository's own dependency-contract test inside that clean environment reproduces
the identical failure seen in the full suite:

```
tests\test_dawnstrike_python_bootstrap.py:55: AssertionError
E   AssertionError: assert '4.15.0' == '4.14.2'
FAILED tests/test_dawnstrike_python_bootstrap.py::test_bootstrap_uses_captured_exact_commit_requirements_after_admission
FAILED tests/test_dawnstrike_python_bootstrap.py::test_bootstrap_handle_locks_every_tracked_non_python_file_through_dispatch
```

This is decisive: the 35 lock-drift failures in the full suite are **not** an artifact of the
developer's machine. Any operator who follows the README from a clean checkout today gets an
environment that fails the repository's own dependency-identity contract.

## Root cause

The repository maintains a strict, hash-bearing `requirements.lock` (151,180 bytes, generated
with `pip-compile --generate-hashes`) and asserts exact installed versions against it in tests,
but the documented and scripted install path is `pip install -e ".[dev]"`, which honours only
the loose floors in `pyproject.toml` (`streamlit>=1.32`, `pytest>=8.0`, `mypy>=1.8`, `ruff>=0.4`).
The lock is therefore an assertion target that no install step ever enforces.

`requirements.in` documents the regeneration command
(`py -m piptools compile --generate-hashes --output-file requirements.lock requirements.in`)
but no document instructs an operator to *install* from the lock.

## Smallest durable correction

Make the documented install path install from the lock, e.g.

```powershell
py -m pip install --require-hashes -r requirements.lock
py -m pip install -e . --no-deps
```

and update `README.md`, `docs/WINDOWS_SETUP.md` and `docs/OPERATIONS.md` accordingly. Regenerate
the lock when a pin is intentionally advanced. Do not weaken the contract test — the test is
correct; the install procedure is what diverges from it.

## Acceptance test

A clean venv created by the documented procedure passes
`tests/test_dawnstrike_python_bootstrap.py` with zero version-mismatch assertions.

## What this does and does not prove

- It **does** prove that installation, dependency resolution, editable build and import all work
  from a clean state, and that the failure is a pinning-procedure gap rather than broken code.
- It does **not** indicate any defect in scanning, scoring, monitoring, outcome or learning logic.
- It **does** disqualify a "reproducible green suite" certification gate until corrected.
