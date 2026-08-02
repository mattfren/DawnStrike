# AlphaOps V6 shadow learning architecture

## Product boundary

V6 is a deterministic research and paper-audit challenger. It never sends a V6 recommendation, places an order, enables a broker, or automatically replaces V5. V5 remains the immutable champion while V6 accumulates forward evidence.

## The closed loop

1. The morning cycle saves every candidate's V6 decision-time record: source-signal identity, exact feature record, source-summary hash, decision timestamp, regime/setup keys, cost model, safety vetoes, and a shadow signal alias.
2. A vetoed candidate is recorded as `SHADOW_REJECT_VETO`; it cannot be made trackable by a model score. A safety-clear candidate is `SHADOW_TRACK`, which is a paper observation only.
3. EOD captures V6 aliases independently from V5 so one V5 source outcome cannot overwrite a challenger outcome. Source bars must be complete, timestamp-valid, and hash-addressable.
4. The V6 label deducts its frozen conservative round-trip cost from a sourced gross return, then compares that result to the sourced benchmark. Missing prices, benchmark values, or source hashes remain null and ineligible.
5. The learner estimates activation separately from conditional net excess return. Its utility uses a lower confidence bound and an adverse-tail penalty. It validates only with expanding, date-ordered windows; training dates are always earlier than evaluation dates.
6. Attribution breaks results down by setup and regime and writes proposed experiments only. A proposal never changes selection policy automatically.

## Immutable ledgers

Migration 14 adds the following additive tables:

- `alpha_v6_decisions`: point-in-time inputs and safety decision.
- `alpha_v6_outcomes`: one immutable sourced outcome receipt per V6 decision.
- `alpha_v6_model_runs`: frozen training receipts.
- `alpha_v6_evaluations`: strict walk-forward evidence receipts.
- `alpha_v6_experiments`: proposed, holdout-only experiments.

V4 and V5 tables are neither rewritten nor treated as V6 labels. A label with absent truth is not a return observation.

## Promotion gate

V6 always reports `NOT_ELIGIBLE_FOR_PROMOTION` unless a human independently reviews the forward evidence. Minimum evidence is 60 sessions and 100 valid, closed, after-cost paper labels, positive mean net excess return, acceptable tail loss, complete source hashes, and point-in-time validation. The system never auto-promotes even after those thresholds.

Until then the only acceptable performance state is:

```text
PERFORMANCE_STATUS=WAITING_FOR_FORWARD_EVIDENCE
```

## Daily command path

The daily EOD task calls, in order, source outcome capture, V5
reconciliation/learning, `alpha-v6-daily-monitor`, and
`alpha-v6-attribution`. The daily monitor only scores with a previously frozen
artifact and records calibration, interval, drift, and coverage evidence; it
cannot refit a model, select a family, or promote a policy. A distinct weekly
task calls `alpha-v6-train-weekly`, which freezes its cutoff/dataset hash and
may emit a research-only competition receipt. Every Python process uses
`dawnstrike_process_runner.ps1`, which persists separate stdout/stderr logs,
SHA-256 hashes, a timing receipt, and the native exit code.

The public Research surface receives a bounded `data/v6-learning.json` projection. It displays the evidence count and promotion threshold but never exposes database paths, runtime roots, raw input paths, or secrets.
