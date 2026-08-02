# Dependency and security update procedure

`pyproject.toml` remains Dawnstrike's package declaration. `requirements.in`
lists the direct runtime, test, research, and security inputs used by CI; `requirements.lock`
is the hash-locked resolved set for the supported Python version.

1. Change a direct lower bound in `pyproject.toml` and its matching line in
   `requirements.in` deliberately. Runtime dependencies, including parser
   safety controls such as `defusedxml`, belong in both places.
2. Create a clean virtual environment with the supported Python version.
3. Regenerate the lock exactly:

   ```powershell
   py -m pip install pip-tools
   py -m piptools compile --generate-hashes --output-file requirements.lock requirements.in
   ```

4. Install with `py -m pip install --require-hashes -r requirements.lock`, then
   run `py -m pip check`, the full test suite, `py -m pip_audit -r requirements.lock`,
   and the CI security commands.
5. Review every transitive change, SBOM delta, license result, and vulnerability
   advisory. Do not suppress high/critical findings without a dated owner and
   compensating control.

`config/security/bandit-baseline.json` records legacy medium findings that were
reviewed when the gate was introduced. CI rejects any new medium or high
finding. Shrink the baseline only by fixing the specific finding and preserving
its regression test; never refresh it blindly.

`.secrets.baseline` is a reviewed detect-secrets snapshot of tracked files.
The Windows CI lane rejects a new candidate rather than treating a scan report
as a passing result. Test fixtures that intentionally resemble credentials must
be marked with a precise `pragma: allowlist secret` comment.

The lock is regenerated only through this process. Never hand-edit it or use
unhashed package installation in CI.
