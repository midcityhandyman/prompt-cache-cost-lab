"""Command line entry point: `costlab report` and `costlab replay`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pricing import PriceConfigError, load_prices
from .replay import ReplayError, load_env_file, load_prompts, pick_transport, replay_report, run_replay, save_usage
from .report import full_report
from .scenarios import load_requests, run_scenarios

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRICES = ROOT / "config" / "prices.toml"
DEFAULT_USAGE = ROOT / "fixtures" / "usage_recorded.jsonl"
DEFAULT_PROMPTS = ROOT / "fixtures" / "replay_prompts.json"
DEFAULT_BASELINE = "claude-opus-5-5"
DEFAULT_CHEAPER = "claude-sonnet-5-5"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="costlab", description=__doc__)
    parser.add_argument("--prices", default=str(DEFAULT_PRICES), help="path to the price file")
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="cost recorded usage under every strategy")
    report.add_argument("--usage", default=str(DEFAULT_USAGE), help="JSONL usage fixture")
    report.add_argument("--baseline", default=DEFAULT_BASELINE, help="model the workload runs on today")
    report.add_argument("--cheaper", default=DEFAULT_CHEAPER, help="cheaper tier to compare against")

    replay = sub.add_parser("replay", help="replay prompts with caching off then on")
    replay.add_argument("--prompts", default=str(DEFAULT_PROMPTS), help="JSON prompt set")
    replay.add_argument("--model", default=DEFAULT_BASELINE)
    replay.add_argument("--live", action="store_true", help="use the real API (needs ANTHROPIC_API_KEY)")
    replay.add_argument("--save", help="write measured usage to this JSONL file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        book = load_prices(args.prices)
        if args.command == "report":
            requests = load_requests(args.usage)
            results = run_scenarios(requests, book, args.baseline, args.cheaper)
            print(full_report(results, book, Path(args.usage).name))
        else:
            load_env_file(ROOT / ".env")
            transport, note = pick_transport(args.live, book)
            result = run_replay(load_prompts(args.prompts), transport, book, args.model)
            print(replay_report(result, book, note))
            if args.save:
                save_usage(result, args.save)
                print(f"Usage written to {args.save}")
    except (PriceConfigError, ReplayError, ValueError, OSError) as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
