"""Harness-level lookahead assertion for the ID08 replay (AMENDMENT 2, correction 3).

``tests/test_id08_noise_band.py::test_decision_unchanged_when_bars_at_and_after_fill_bar_are_removed``
only proves ``decide_signal``'s SIGNATURE has no parameter through which a
future bar could arrive. It says nothing about whether
``scripts/id08_run_replay.py``'s harness -- which builds the
``PriorSessionSnapshot``/``current_session_open``/``decision_bar_close``
arguments from the cached bar CSV -- actually respects that boundary when
wiring real data together.

This test exercises the HARNESS itself, on the real cached bars reused from
``C:\\r\\dsos-00-v3\\id08_run\\spy_1min_bars_cache.csv`` (no re-fetch, per the
ID08 packet instructions): it runs the full replay once untouched, then
again with one session's bar series truncated at a chosen fill bar (as if
that bar and everything chronologically after it inside the session had
never been fetched), and asserts every decision produced up to and
including that fill bar's own decision mark is byte-for-byte identical
between the two runs.

If the harness ever leaked a later bar into an earlier decision (e.g. by
indexing the wrong minute, or by using a same-session future close instead
of the decision bar's own close), deleting that later bar would starve the
leak of the data it depends on and the decision would change (or vanish).
This test is therefore capable of catching a real harness-level leak, not
just a signature-level one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.id08_run_replay import (
    CACHE_PATH,
    EVAL_END,
    LOOKBACK_SESSIONS,
    WARMUP_START,
    SessionBars,
    load_bars_by_session,
    load_calendar_sessions,
    run_replay,
)

pytestmark = pytest.mark.skipif(
    not Path(CACHE_PATH).exists(),
    reason=f"real cached bars not present at {CACHE_PATH}; this test reuses "
    "that cache and does not fetch data itself",
)


def _load_full_fixture():
    calendar_sessions = load_calendar_sessions(WARMUP_START, EVAL_END)
    calendar_close_times = dict(calendar_sessions)
    session_order = [s for s, _ in calendar_sessions]
    eval_sessions = session_order[LOOKBACK_SESSIONS:]
    bars_by_session = load_bars_by_session(calendar_close_times)
    return session_order, eval_sessions, bars_by_session


def _truncate_session_at_mark(bucket: SessionBars, cutoff_hhmm: str) -> SessionBars:
    """Return a COPY of ``bucket`` with every minute >= ``cutoff_hhmm`` removed.

    09:30's own open is preserved (it is never a decision mark; it only
    supplies the band anchor and always precedes any real fill bar).
    """

    truncated_closes = {
        hhmm: px for hhmm, px in bucket.minute_closes.items() if hhmm < cutoff_hhmm
    }
    truncated_opens = {
        hhmm: px for hhmm, px in bucket.minute_opens.items() if hhmm < cutoff_hhmm
    }
    return SessionBars(
        session_date=bucket.session_date,
        close_time_et=bucket.close_time_et,
        open_0930=bucket.open_0930,
        minute_closes=truncated_closes,
        minute_opens=truncated_opens,
    )


def test_harness_decisions_unchanged_when_bars_at_and_after_fill_bar_are_removed():
    session_order, eval_sessions, bars_by_session = _load_full_fixture()

    decisions_full, trades_full = run_replay(session_order, eval_sessions, bars_by_session)
    assert trades_full, "fixture must contain at least one real trade to be a meaningful test"

    # Pick a real trade's entry: entry_fill_mark is the exact bar the
    # harness filled on. That bar (and everything after it in the session)
    # is what we delete.
    target_trade = trades_full[0]
    target_session = target_trade["entry_session"]
    entry_decision_mark = target_trade["entry_decision_mark"]
    entry_fill_mark = target_trade["entry_fill_mark"]

    truncated_bars_by_session = dict(bars_by_session)  # shallow copy of the session map
    truncated_bars_by_session[target_session] = _truncate_session_at_mark(
        bars_by_session[target_session], entry_fill_mark
    )

    decisions_truncated, _ = run_replay(session_order, eval_sessions, truncated_bars_by_session)

    assert len(decisions_truncated) == len(decisions_full), (
        "truncation must not change how many decision rows are produced -- "
        "only their content for the affected session"
    )

    decisions_up_to_fill_bar = [
        row
        for row in decisions_full
        if row["session"] == target_session and row["mark"] and row["mark"] <= entry_decision_mark
    ]
    assert decisions_up_to_fill_bar, "expected at least the entry decision mark itself"

    decisions_truncated_by_key = {
        (row["session"], row["mark"]): row for row in decisions_truncated
    }

    for full_row in decisions_up_to_fill_bar:
        key = (full_row["session"], full_row["mark"])
        truncated_row = decisions_truncated_by_key[key]
        assert truncated_row == full_row, (
            f"decision at {key} changed after deleting bars at/after the fill bar "
            f"{entry_fill_mark} -- this is exactly the signature of a lookahead leak"
        )

    # Sanity check that the truncation actually did something -- otherwise
    # this test would pass trivially even with a real leak. Sessions in the
    # 14-session lookback window immediately after target_session legitimately
    # lose data too (target_session is one of THEIR 14 prior sessions, and it
    # is now missing its own post-cutoff marks), so those later sessions are
    # expected to pick up new NO_DATA rows -- that is a real, honest data-gap
    # effect of deleting real bars, not a leak. What must hold is that the
    # overall run was NOT a no-op: at least one row changed somewhere.
    assert decisions_truncated != decisions_full, (
        "truncating the fill bar had no effect at all -- this test would not "
        "be capable of catching a real leak"
    )
