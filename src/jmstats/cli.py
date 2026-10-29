from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .archive import Archive, acquire_lock
from .collect import (
    analyze_fees,
    collect_chain,
    collect_market,
    import_analyzer,
    import_bond_history,
    import_fee_estimates,
    import_orderbook_history,
    infer_historical_fees,
    initialize,
)
from .http import JsonClient
from .render import render
from .solver import DEFAULT_SECONDS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect and render Bitcoin mainnet JoinMarket stats"
    )
    parser.add_argument(
        "--data", type=Path, default=Path("data"), help="Persistent archive directory"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Explicitly initialize a new chain archive")
    init.add_argument("--start-height", type=int, required=True)
    importer = commands.add_parser(
        "import-analyzer", help="Import the local 2025+ analyzer SQLite corpus"
    )
    importer.add_argument("database", type=Path)
    importer.add_argument("--start-height", type=int)
    for name, help_text in (
        ("import-orderbook-history", "Import reduced historical snapshot JSONL"),
        ("import-bond-history", "Import reduced historical active bond weights"),
        ("import-fees", "Reduce local conditional solver results to monthly fee estimates"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("source", type=Path)
    for name in ("sync", "update"):
        command = commands.add_parser(
            name, help="Collect chain" if name == "sync" else "Collect both sources and render"
        )
        command.add_argument("--api", default="https://mempool.sgn.space/api")
        command.add_argument("--interval", type=float, default=2.0)
        command.add_argument("--request-budget", type=int, default=600)
        command.add_argument("--max-blocks", type=int, default=200)
        command.add_argument("--solver-seconds", type=float, default=DEFAULT_SECONDS)
        command.add_argument(
            "--no-solver", action="store_true", help="Collect only, for a separate solver writer"
        )
        command.add_argument(
            "--backfill",
            action="store_true",
            help="Explicitly fill archive gaps, local bootstrap only",
        )
        command.add_argument(
            "--from-height", type=int, help="Explicit backfill floor, requires --backfill"
        )
        if name == "update":
            command.add_argument(
                "--orderbook", default="https://joinmarket-ng.sgn.space/orderbook.json"
            )
            command.add_argument("--output", type=Path, default=Path("site"))
    market = commands.add_parser("orderbook", help="Collect one aggregate watcher snapshot")
    market.add_argument("--url", default="https://joinmarket-ng.sgn.space/orderbook.json")
    build = commands.add_parser("render", help="Render a fully self-contained static site")
    build.add_argument("--output", type=Path, default=Path("site"))
    rollback = commands.add_parser(
        "rollback", help="Explicitly remove blocks above a height after a reorg"
    )
    rollback.add_argument("--to", type=int, required=True)
    infer = commands.add_parser(
        "infer-cj-fees", help="Explicitly infer fee ceilings from known historical CJs"
    )
    infer.add_argument("--days", type=int, default=365)
    infer.add_argument(
        "--cache", type=Path, help="Optional local analyzer SQLite raw-transaction cache"
    )
    infer.add_argument("--cache-only", action="store_true")
    infer.add_argument("--api", default="https://mempool.sgn.space/api")
    infer.add_argument("--interval", type=float, default=2.0)
    infer.add_argument("--request-budget", type=int, default=600)
    infer.add_argument("--max-transactions", type=int, default=20_000)
    analyze = commands.add_parser(
        "analyze-fees", help="Explicitly solve archived CJs, independently of the chain writer"
    )
    analyze.add_argument("--cache", type=Path, help="Read-only analyzer raw-transaction cache")
    analyze.add_argument("--cache-only", action="store_true")
    analyze.add_argument("--from-height", type=int, default=0)
    analyze.add_argument("--to-height", type=int)
    analyze.add_argument("--max-transactions", type=int, default=200)
    analyze.add_argument("--max-seconds", type=float, default=900)
    analyze.add_argument("--solver-seconds", type=float, default=DEFAULT_SECONDS)
    analyze.add_argument(
        "--retry", action="store_true", help="Retry interrupted, timed-out, or errored attempts"
    )
    analyze.add_argument("--api", default="https://mempool.sgn.space/api")
    analyze.add_argument("--interval", type=float, default=2.0)
    analyze.add_argument("--request-budget", type=int, default=600)
    args = parser.parse_args(argv)
    locks = []
    try:
        if args.command == "analyze-fees":
            locks.append(acquire_lock(args.data / "fee-solutions"))
        elif args.command != "render":
            locks.append(acquire_lock(args.data))
            if args.command in ("sync", "update") and not args.no_solver:
                locks.append(acquire_lock(args.data / "fee-solutions"))
        if args.command == "init":
            initialize(args.data, args.start_height)
            result = {"status": "initialized"}
        elif args.command == "import-analyzer":
            result = import_analyzer(args.data, args.database, args.start_height)
        elif args.command == "orderbook":
            result = collect_market(args.data, args.url)
        elif args.command == "import-orderbook-history":
            result = import_orderbook_history(args.data, args.source)
        elif args.command == "import-bond-history":
            result = import_bond_history(args.data, args.source)
        elif args.command == "import-fees":
            result = import_fee_estimates(args.data, args.source)
        elif args.command == "infer-cj-fees":
            result = infer_historical_fees(
                Archive(args.data),
                JsonClient(args.api, interval=args.interval, budget=args.request_budget),
                days=args.days,
                cache=args.cache,
                cache_only=args.cache_only,
                max_transactions=args.max_transactions,
            )
        elif args.command == "analyze-fees":
            result = analyze_fees(
                args.data,
                JsonClient(args.api, interval=args.interval, budget=args.request_budget),
                cache=args.cache,
                cache_only=args.cache_only,
                from_height=args.from_height,
                to_height=args.to_height,
                max_transactions=args.max_transactions,
                max_seconds=args.max_seconds,
                solver_seconds=args.solver_seconds,
                retry=args.retry,
            )
        elif args.command == "render":
            result = render(args.data, args.output)
        elif args.command == "rollback":
            Archive(args.data).rollback(args.to)
            result = {"status": "rolled back", "to": args.to}
        else:
            result = {}
            failed = False
            if args.max_blocks < 1:
                raise ValueError("max-blocks must be positive")
            if args.command == "update":
                try:
                    result["market"] = collect_market(args.data, args.orderbook)
                except Exception as exc:
                    failed = True
                    result["market"] = {"status": "error", "reason": str(exc)}
            try:
                result["chain"] = collect_chain(
                    Archive(args.data),
                    JsonClient(args.api, interval=args.interval, budget=args.request_budget),
                    backfill=args.backfill,
                    max_blocks=args.max_blocks,
                    from_height=args.from_height,
                    solve_fees=not args.no_solver,
                    solver_seconds=args.solver_seconds,
                )
                if result["chain"].get("solver_outcomes", {}).get("error", 0):
                    failed = True
            except Exception as exc:
                failed = True
                result["chain"] = {"status": "error", "reason": str(exc)}
            if args.command == "update":
                result["render"] = render(args.data, args.output)
            print(json.dumps(result, indent=2))
            return 1 if failed else 0
        print(json.dumps(result, indent=2))
        return 1 if result.get("outcomes", {}).get("error", 0) else 0
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        for lock in reversed(locks):
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
