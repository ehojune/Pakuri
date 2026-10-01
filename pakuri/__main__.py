"""CLI publishes schema v1 JSON without treating external prose as instructions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .collector import collect, load_config
from .storage import WriterBusy


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Star-seeker: learn from public GitHub development")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("collect", help="collect bounded activity into atomic schema v1 JSON")
    command.add_argument("--config", default="targets.json")
    command.add_argument("--output", default="data/latest.json")
    command.add_argument("--state", help="SQLite checkpoint database (default: output directory/state.sqlite3)")
    command.add_argument("--hours", type=int, default=24)
    command.add_argument("--max-requests", type=int)
    report = subparsers.add_parser("report", help="render a concise report without consuming delivery state")
    report.add_argument("--input", default="data/latest.json")
    report.add_argument("--limit", type=int, default=8)
    args = parser.parse_args(argv)
    if args.command == "report":
        try:
            from .reporting import render
            with open(args.input, encoding="utf-8-sig") as stream:
                payload = json.load(stream)
            print(render(payload, limit=args.limit))
            return 0
        except (ValueError, OSError, TypeError):
            print("pakuri: invalid_report_input", file=sys.stderr)
            return 2
    try:
        state = args.state or str(Path(args.output).parent / "state.sqlite3")
        result = collect(load_config(args.config), state, hours=args.hours, max_requests=args.max_requests, output_path=args.output)
    except (ValueError, OSError, json.JSONDecodeError, WriterBusy) as exc:
        # Config paths may be private; do not include exception details or tokens.
        code = "writer_busy" if isinstance(exc, WriterBusy) else "configuration_or_output_error"
        print("pakuri: " + code, file=sys.stderr)
        return 2
    print("pakuri: " + result["status"] + " · " + str(len(result["items"])) + " items · " + str(result["coverage"].get("requests", 0)) + " requests")
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
