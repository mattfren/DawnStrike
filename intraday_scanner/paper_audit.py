"""Console entry point for paper-audit runs."""

from __future__ import annotations

import argparse

from intraday_scanner.config import load_config
from intraday_scanner.logging_config import configure_logging
from intraday_scanner.services.audit_service import run_paper_audit


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper-audit ranked intraday candidates")
    parser.add_argument("--ranked", required=True, help="ranked_candidates.csv from a scan run")
    parser.add_argument("--minute-bars", required=True, help="minute bars CSV")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--top-n", type=int, default=3)
    parser.add_argument("--slippage-bps", type=float, default=None)
    parser.add_argument("--entry-mode", choices=["open", "breakout"], default="open")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    configure_logging(args.log_level)
    config = load_config(slippage_bps=args.slippage_bps, entry_mode=args.entry_mode)
    paths = run_paper_audit(
        args.ranked,
        args.minute_bars,
        args.out_dir,
        config,
        top_n=args.top_n,
        fixture_only=_is_fixture_path(args.minute_bars),
    )
    print(f"Wrote paper audit trades to {paths['trades']}")
    print(f"Wrote paper audit summary to {paths['summary']}")
    return 0


def _is_fixture_path(value: str) -> bool:
    return "sample_data" in value.replace("/", "\\").lower()


if __name__ == "__main__":
    raise SystemExit(main())
