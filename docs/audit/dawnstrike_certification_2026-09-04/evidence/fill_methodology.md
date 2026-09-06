# Fill methodology and path ambiguity — audit evidence

Commit `14ca714f`. This dimension resolves the mission's historical concern #10
("paper audit may use the first eligible open rather than the strategy's actual breakout-fill
condition") and the concern that same-bar stop/target ordering is unresolvable.

## There are three distinct simulation paths, with different fidelity

| Path | File | Entry rule | Path model | On production path? |
|---|---|---|---|---|
| Offline paper audit | `services/audit_service.py` | `open` (default) or `breakout` | intraday bars | CLI only (`paper-audit`, `audit-latest`) |
| Research position engine | `v2/paper_ops/position_management.py` | policy-driven | full bar OHLC path | **No** |
| Production trade-watch | `services/trade_watcher_service.py` | breakout confirmation | **polled spot price** | **Yes** |

## 1. Offline paper audit — historical concern #10 is CONFIRMED, with nuance

`intraday_scanner/services/audit_service.py:151-164`:

```python
entry_mode = config.entry_mode
if entry_mode == "breakout":
    entry_index = _breakout_entry_index(eligible, trigger)
    if entry_index is None:
        return _no_entry_trade(candidate, "Breakout trigger was not touched after signal.")
    entry_bar = eligible[entry_index]
    raw_entry_price = max(trigger, float(entry_bar["open"]))
    after_entry = eligible[entry_index:]
else:
    entry_bar = eligible[0]
    raw_entry_price = float(entry_bar["open"])
    after_entry = eligible
```

- The **default is `open`**: `config.py:70` (`entry_mode: str = "open"`), `config.py:302`
  (`INTRADAY_ENTRY_MODE` default `"open"`), `paper_audit.py:19` (`default="open"`),
  `.env.example` (`INTRADAY_ENTRY_MODE=open`).
- Under the default, the simulation enters at the first eligible bar's open **regardless of
  whether the strategy's own breakout trigger was ever touched**. It therefore fills trades the
  strategy would never have taken and removes the confirmation selection effect entirely.
- The product's own stated entry condition is a confirmation rule. Live rows in
  `historical_signals.entry_condition` read: `"Watch only if price confirms above 1.809."`
- **Verdict: the historical concern is TRUE at this commit** — but as a misaligned *default*,
  not as broken machinery.

`breakout` mode, when selected, is **correct and conservative**:

- `_breakout_entry_index` (`audit_service.py:331-337`) returns the first bar whose **high**
  reaches the trigger — correct intrabar touch detection.
- Fill is `max(trigger, bar.open)` — you can never fill better than the trigger, and a bar that
  gaps above it fills at the worse open. This is exactly right.
- If the trigger is never touched, `_no_entry_trade(...)` records no fill rather than forcing one.

One residual defect: `_breakout_entry_index` returns `0` when `trigger <= 0`
(`audit_service.py:332-333`), so a missing or zero trigger **silently degrades breakout mode into
open mode** at the first bar instead of refusing. That is a silent fallback in a path whose whole
purpose is strictness.

**No-lookahead on eligibility is correct.** `_eligible_bars` (`audit_service.py:248-259`) keeps
only bars at or after both `config.signal_time` and the candidate's recommendation timestamp, so
entry cannot precede the signal.

## 2. Research position engine — same-bar ambiguity IS handled correctly

`intraday_scanner/v2/paper_ops/position_management.py:140-205` is the strongest quantitative code
in the repository:

- Explicit lookahead refusal:
  `if bar.timestamp < opened_at: raise ValueError("position policy cannot inspect a bar before the open")`
- **Stop gap-through first**: `bar.open <= stop` (long) exits at `bar.open`, i.e. the realistic
  worse price, not the stop level.
- **Stop before target within the same bar** — `same_bar_policy = "stop_first_conservative"`
  (`position_management.py:40`). This is the correct conservative resolution when one-minute OHLC
  cannot order two touches.
- Target gap-through exits at `bar.open`; target touch exits at the target level.
- Trailing stop evaluated after stop, before target.

So the historical claim that same-bar stop/target ordering is unresolved is **FALSE at this
commit** for this engine. It is resolved, and resolved the right way.

## 3. Production trade-watch — a polled spot-price model, not a path model

`services/trade_watcher_service.py` is what the live 5-minute monitor invokes.

Entry (`~1749-1790`) is genuinely conservative and **does** require confirmation:

- refuses while `price < trigger` (`STATE_WATCHING`)
- stands down `already_invalidated` if `price <= stop`
- stands down `already_extended` if `price >= target` (refuses late entry)
- enforces `reward_risk >= settings.min_reward_risk`

Exit (`_exit_decision`, `1838-1877`) checks stop before target, then an EOD flatten rule —
stop-first conservatism preserved.

**The material limitation** is the input, not the logic: both decisions consume a single scalar
`price` from a 5-minute observation bundle, not a bar path. Consequently the production paper
model:

- cannot see a stop touched between polls that recovered before the next poll (understates stops);
- cannot see a target touched between polls (understates targets);
- fills at the observed poll price, which may be arbitrarily far through the level.

For sub-$25 premarket gappers — the entire target universe — intrabar excursion between
five-minute polls is large. **Results from this path must not be presented as path-accurate
backtest results.**

## Cross-cutting conclusion

The repository contains a correct, conservative, lookahead-guarded, path-accurate position engine
(`position_management.py`) and does not use it in production. Production instead runs a
lower-fidelity polled model in `trade_watcher_service.py`. `evaluate_position_management` has zero
non-test callers outside `v2/paper_ops`; `trade_watcher_service.py` does not reference it.

This has never been exercised in the field: `paper_positions`, `paper_trade_fills` and
`trade_intents` all contain **0 rows** in the live database, so the production exit path has never
actually run on a real position.

## Required remediation

1. Change the paper-audit default `entry_mode` to `breakout`, or refuse to emit results for a
   confirmation strategy under `open` mode without labeling them as unconditional-entry
   simulations. (P1, research validity)
2. Make `_breakout_entry_index` refuse a non-positive trigger instead of returning index 0. (P2)
3. Either route production exits through `evaluate_position_management` against retained
   one-minute bars, or explicitly label production paper results as poll-resolution estimates
   with an accounted intrabar-excursion blind spot. (P1, research validity)
