"""Required-key/schema validation and capability-status resolution.

The scanner's runtime configuration has always let an unset environment
variable fall back to a hard-coded default with no record that the default
was used.  For plain tuning knobs that is fine.  For a *capability* flag -
a key whose only job is to gate a subsystem on or off - it is not fine: an
absent key and an operator explicitly setting the key to ``false`` collapse
onto the exact same ``False`` value, and nothing downstream can tell them
apart.  ``DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED`` did exactly that: the key
was never written into the runtime's source file, so it silently evaluated
to ``false`` and the strategy-decision-receipt producer never ran, across
537 sessions, with no error and no visible signal.

This module gives every capability key an explicit, three-way status
(:class:`CapabilityStatus`) and gives every *required* key a loud, named
failure instead of a silent default.  Nothing here changes what any
existing key defaults to; it only makes the source of that value
observable and makes a genuinely required key fail fast.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

from intraday_scanner.errors import ConfigError

_BOOL_TRUE_TOKENS = {"true", "1", "yes", "y", "on"}
_BOOL_FALSE_TOKENS = {"false", "0", "no", "n", "off"}


class CapabilityStatus(str, Enum):
    """Tri-state status for a key that gates an optional subsystem.

    ``DISABLED_MISSING_CONFIG`` and ``DISABLED_BY_OPERATOR`` are
    deliberately distinct values.  Conflating them is exactly the bug this
    module exists to close: a missing key is a configuration gap, an
    explicit ``false`` is a decision.
    """

    ENABLED = "ENABLED"
    DISABLED_BY_OPERATOR = "DISABLED_BY_OPERATOR"
    DISABLED_MISSING_CONFIG = "DISABLED_MISSING_CONFIG"


class OperatorRunState(str, Enum):
    """Why the strategy engine will or will not place a new entry today.

    ``NO_ELIGIBLE_POLICY`` is deliberately distinct from
    ``ENTRIES_DISABLED_BY_OPERATOR`` and from ``ERROR``: a strategy search
    that rejected every candidate is a healthy system with nothing
    qualified to trade, not an operator toggle and not a fault.  Collapsing
    these into one value is exactly the gap this closes - an operator
    looking at "no trades today" could otherwise not tell which of the
    three it was.
    """

    POLICY_ACTIVE = "POLICY_ACTIVE"
    NO_ELIGIBLE_POLICY = "NO_ELIGIBLE_POLICY"
    ENTRIES_DISABLED_BY_OPERATOR = "ENTRIES_DISABLED_BY_OPERATOR"
    POLICY_STATE_UNAVAILABLE = "POLICY_STATE_UNAVAILABLE"
    ERROR = "ERROR"


# Freshness rule for the policy-eligibility state (mirrors the precedent set
# by ``DISABLED_MISSING_CONFIG`` vs ``DISABLED_BY_OPERATOR`` above): the
# strategy-eligibility registry that produces ``eligible_policy_ids`` must
# have been refreshed within this many seconds of "now", or its contents are
# no longer trustworthy enough to compute NO_ELIGIBLE_POLICY vs
# POLICY_ACTIVE from. A named constant so the threshold is never a magic
# number buried in a branch; see ``operator_run_status`` in config.py, the
# single call site that measures age against it.
POLICY_STATE_MAX_AGE_SECONDS = 900.0  # 15 minutes


def evaluate_operator_run_state(
    *,
    eligible_policy_count: int,
    entries_enabled: bool,
    has_errors: bool,
    policy_state_unavailable: bool = False,
) -> OperatorRunState:
    """Compute the run state from real inputs - never a hardcoded value.

    Precedence: a reported error always wins (it means the other signals may
    not be trustworthy - this is ``has_errors``, a genuine runtime/data
    failure such as a provider exception or a reconciliation mismatch).

    Next, ``policy_state_unavailable`` - the policy-eligibility registry
    itself is missing, unreadable/corrupt, or stale (older than
    ``POLICY_STATE_MAX_AGE_SECONDS``, see above) - produces
    ``POLICY_STATE_UNAVAILABLE``. This is deliberately checked *before*
    ``eligible_policy_count``: if we cannot read the registry we do not
    know whether zero is the real count or an artifact of the read failure,
    so it must never be reported as the healthy "we looked and nothing
    qualified" state (``NO_ELIGIBLE_POLICY``), and it is not a fault in the
    run itself, so it must never be reported as ``ERROR`` either. An
    operator needs to be able to tell "I don't know" from "it broke".

    Otherwise, zero eligible policies means ``NO_ELIGIBLE_POLICY``
    regardless of the operator's entry toggle - even a re-armed toggle
    cannot trade with nothing qualified. Only once a policy is eligible
    does the operator's own entries switch decide between
    ``POLICY_ACTIVE`` and ``ENTRIES_DISABLED_BY_OPERATOR``.
    """

    if has_errors:
        return OperatorRunState.ERROR
    if policy_state_unavailable:
        return OperatorRunState.POLICY_STATE_UNAVAILABLE
    if eligible_policy_count <= 0:
        return OperatorRunState.NO_ELIGIBLE_POLICY
    if not entries_enabled:
        return OperatorRunState.ENTRIES_DISABLED_BY_OPERATOR
    return OperatorRunState.POLICY_ACTIVE


@dataclass(frozen=True)
class ConfigKeySpec:
    """Declares one configuration key's contract.

    ``required=True`` means the key MUST be present (in the process
    environment or the parsed .env file) or ``load_config`` fails loudly,
    naming this key and ``subsystem``.  ``required=False`` means the key
    may be absent and ``default`` is used - but for a ``kind="bool"``
    capability key (``subsystem`` set), that absence is still reported via
    :class:`CapabilityStatus`, never silently swallowed.
    """

    name: str
    kind: str  # "bool" | "int" | "float" | "str"
    required: bool = False
    subsystem: str | None = None
    default: str = "false"


@dataclass(frozen=True)
class ResolvedKey:
    """The outcome of resolving one :class:`ConfigKeySpec` against env data."""

    name: str
    value: Any
    source: str  # "explicit" | "default" | "absent"
    defaulted: bool
    status: CapabilityStatus | None


def key_present(name: str, env_values: dict[str, str]) -> tuple[bool, str | None]:
    """Return whether ``name`` was set at all, and its raw string if so.

    Checked in the same precedence order as ``config._env``: process
    environment first, then the parsed .env file.  A key set to the empty
    string counts as present (an operator wrote something), not absent.
    """

    if name in os.environ:
        return True, os.environ[name]
    if name in env_values:
        return True, env_values[name]
    return False, None


def _parse_bool_token(raw: str, *, key_name: str, subsystem: str | None) -> bool:
    normalized = raw.strip().lower()
    if normalized in _BOOL_TRUE_TOKENS:
        return True
    if normalized in _BOOL_FALSE_TOKENS:
        return False
    subsystem_note = f" (disables {subsystem})" if subsystem else ""
    raise ConfigError(
        f"{key_name} must be a boolean (true/false), got {raw!r}{subsystem_note}"
    )


def require_present(spec: ConfigKeySpec, env_values: dict[str, str]) -> None:
    """Raise loudly, naming the key and subsystem, if a required key is absent."""

    if not spec.required:
        return
    present, _ = key_present(spec.name, env_values)
    if not present:
        subsystem_note = f" required by {spec.subsystem}" if spec.subsystem else ""
        raise ConfigError(
            f"Missing required configuration key {spec.name}{subsystem_note}: "
            "no silent default is allowed for this key"
        )


def resolve_capability_bool(
    spec: ConfigKeySpec, env_values: dict[str, str]
) -> ResolvedKey:
    """Resolve a boolean capability key, distinguishing absent vs explicit-false.

    - Required and absent -> raises :class:`ConfigError` naming the key.
    - Present but not a recognized boolean token (e.g. ``"maybe"``) ->
      raises :class:`ConfigError` naming the key. Never silently coerced.
    - Absent and optional -> resolves to the declared default and reports
      ``DISABLED_MISSING_CONFIG`` when that default is falsy (or
      ``ENABLED`` on the rare capability whose safe default is on).
    - Present and explicitly false -> ``DISABLED_BY_OPERATOR``.
    - Present and explicitly true -> ``ENABLED``.
    """

    require_present(spec, env_values)
    present, raw = key_present(spec.name, env_values)

    if not present:
        value = _parse_bool_token(spec.default, key_name=spec.name, subsystem=spec.subsystem)
        status = CapabilityStatus.ENABLED if value else CapabilityStatus.DISABLED_MISSING_CONFIG
        return ResolvedKey(
            name=spec.name, value=value, source="absent", defaulted=True, status=status
        )

    value = _parse_bool_token(raw, key_name=spec.name, subsystem=spec.subsystem)
    status = CapabilityStatus.ENABLED if value else CapabilityStatus.DISABLED_BY_OPERATOR
    return ResolvedKey(
        name=spec.name, value=value, source="explicit", defaulted=False, status=status
    )


def resolve_optional_scalar(
    spec: ConfigKeySpec,
    env_values: dict[str, str],
    parser: Any,
) -> ResolvedKey:
    """Resolve a non-capability optional key, recording whether it defaulted.

    ``parser`` is a callable ``(raw: str) -> Any`` that may raise
    :class:`ConfigError` for a malformed value; that error is not
    swallowed - a malformed optional value must fail loudly too, it is only
    the *value itself* that is allowed to default, never a malformed one.
    """

    require_present(spec, env_values)
    present, raw = key_present(spec.name, env_values)
    if not present:
        return ResolvedKey(
            name=spec.name,
            value=parser(spec.default),
            source="absent",
            defaulted=True,
            status=None,
        )
    return ResolvedKey(
        name=spec.name, value=parser(raw), source="explicit", defaulted=False, status=None
    )
