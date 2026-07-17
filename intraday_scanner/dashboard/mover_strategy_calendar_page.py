"""Truth-gated Streamlit mount for the Mover Pattern Lab calendar."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from intraday_scanner.v2.mover_pattern_lab.calendar_report import (
    render_strategy_calendar_report,
)

DEFAULT_MOVER_LAB_ROOT = Path("data/v2_mover_pattern_lab")


def render_mover_strategy_calendar(
    output_root: str | Path | None = None,
) -> bool:
    """Render retained mover strategy days; return false when none are available."""

    root = Path(output_root) if output_root is not None else DEFAULT_MOVER_LAB_ROOT
    reports_directory = (root / "reports").resolve()
    latest_path = reports_directory / "mover_pattern_analysis_latest.json"
    if not latest_path.is_file():
        return False
    try:
        latest = _read_object(latest_path)
        report_path = Path(str(latest.get("report_path") or "")).resolve()
        report = _read_object(report_path)
        fingerprint = str(latest.get("analysis_fingerprint") or "")
        if (
            latest.get("schema_version")
            != "v2.mover_pattern_lab.v1.analysis_latest"
            or len(fingerprint) != 64
            or _fingerprint(report) != fingerprint
            or report_path.parent != reports_directory
            or report_path.name
            != f"mover_pattern_analysis_{fingerprint[:16]}.json"
            or str(latest.get("report_sha256") or "") != _sha256_file(report_path)
            or report.get("research_only") is not True
            or report.get("broker_execution_enabled") is not False
            or not isinstance(report.get("strategy_daily_calendar"), list)
        ):
            raise ValueError("mover calendar evidence manifest failed integrity checks")
        html = render_strategy_calendar_report(report, title="Mover Pattern Lab")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        st.error(f"Mover strategy calendar blocked: {exc}")
        return False

    st.markdown("### Mover Pattern Lab")
    st.caption(
        "Frozen paper strategies. Forward observations and historical replay are "
        "separate; missing outcomes remain blank."
    )
    components.html(html, height=920, scrolling=True)
    return True


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["render_mover_strategy_calendar"]
