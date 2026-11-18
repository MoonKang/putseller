"""Command line: python -m putseller run <snapshot.json> | report"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import paper, report, screen, store
from .config import load_rules
from .models import load_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="putseller")
    parser.add_argument("--db", default="data/paper.db", help="log database")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="mark open paper positions, then scan a snapshot")
    run.add_argument("snapshot", help="snapshot JSON file")
    run.add_argument("--rules", default="rules.toml")

    sub.add_parser("report", help="summarize the whole log")

    scr = sub.add_parser("screen", help="propose a stock universe from a saved scanner result")
    scr.add_argument("scan", help="scanner result JSON (data/screens/<date>.json)")
    scr.add_argument("--config", default="screen.toml")
    scr.add_argument("--rules", default="rules.toml", help="where the current universe is read")
    scr.add_argument("--apply", action="store_true",
                     help="write the proposed universe into rules.toml and strategies/*.toml")

    args = parser.parse_args(argv)
    if args.command == "screen":
        targets = [Path(args.rules), *sorted(Path("strategies").glob("*.toml"))] if args.apply else None
        print(screen.run(args.scan, args.config, args.rules, targets))
        return 0
    conn = store.connect(args.db)
    try:
        if args.command == "run":
            try:
                result = paper.run(conn, load_snapshot(args.snapshot),
                                   load_rules(args.rules), args.snapshot)
            except (ValueError, KeyError, OSError) as err:
                print(f"error: {err}", file=sys.stderr)
                return 1
            print(report.format_run(result))
        else:
            print(report.format_report(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
