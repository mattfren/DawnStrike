"""E2E_CONTROL_POLICY_V1 - a test-only fixture policy for the rehearsal.

This policy id must never become selectable by normal deployed
configuration. It is gated on BOTH:

  1. sandbox-path validation (the run is operating inside a validated
     ``tests.e2e.sandbox.Sandbox``), and
  2. fake-provider selection (the broker/provider selection is explicitly
     the loopback fake, never a real host).

It is exposed to the real strategy interface the same way any other
eligibility entry would be: as one of ``ScannerConfig.eligible_policy_ids``.
Nothing here patches ``evaluate_operator_run_state`` or
``evaluate_entry`` - it only produces the *input* those real functions
consume.

A synthetic upstream evidence record is built (as a real eligibility
registry entry would be) and signed with a per-run HMAC key that is
generated fresh for the sandbox and is never the production key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any

from sandbox import Sandbox, SandboxViolation

CONTROL_POLICY_ID = "E2E_CONTROL_POLICY_V1"


@dataclass(frozen=True)
class TrustAnchor:
    """An independent, per-run HMAC key. Never the production signing key."""

    key_id: str
    key: bytes

    def sign(self, payload: bytes) -> str:
        return hmac.new(self.key, payload, hashlib.sha256).hexdigest()


def new_trust_anchor(sandbox: Sandbox) -> TrustAnchor:
    return TrustAnchor(key_id=f"e2e-trust-{sandbox.run_id}", key=secrets.token_bytes(32))


def build_eligibility_evidence(
    *,
    sandbox: Sandbox,
    anchor: TrustAnchor,
    fake_provider_selected: bool,
    entries_enabled: bool,
) -> dict[str, Any]:
    """Build the upstream eligibility-registry record for the control policy.

    Uses the same shape a real registry entry would have - a policy id, a
    signed content hash, and the conditions under which it was produced -
    so ``ScannerConfig.eligible_policy_ids`` receives a realistic input
    rather than a bare hardcoded id.
    """

    record = {
        "policy_id": CONTROL_POLICY_ID,
        "run_id": sandbox.run_id,
        "classification": "SYNTHETIC_E2E",
        "fake_provider_selected": fake_provider_selected,
    }
    body = json.dumps(record, sort_keys=True).encode("utf-8")
    record["signature"] = anchor.sign(body)
    record["signed_by"] = anchor.key_id
    return record


def eligible_policy_ids_for(
    *, sandbox: Sandbox, evidence: dict[str, Any], anchor: TrustAnchor
) -> tuple[str, ...]:
    """Gate selectability: sandbox path AND fake-provider selection, both real checks.

    Fails closed (returns no eligible policies) unless both gates hold and
    the signature actually verifies against the supplied evidence - this is
    what stops the control policy from ever being selectable by a normal,
    un-sandboxed run: there is no code path that can construct a verifying
    evidence record outside a real Sandbox with the fake provider selected.
    """

    from sandbox import assert_inside_sandbox

    try:
        assert_inside_sandbox(sandbox.root, sandbox.root)
    except SandboxViolation:
        return ()

    if not evidence.get("fake_provider_selected"):
        return ()
    if evidence.get("policy_id") != CONTROL_POLICY_ID:
        return ()

    signature = evidence.get("signature")
    signed_by = evidence.get("signed_by")
    if signature is None or signed_by != anchor.key_id:
        return ()
    unsigned = {k: v for k, v in evidence.items() if k not in ("signature", "signed_by")}
    body = json.dumps(unsigned, sort_keys=True).encode("utf-8")
    if not hmac.compare_digest(anchor.sign(body), str(signature)):
        return ()

    return (CONTROL_POLICY_ID,)
