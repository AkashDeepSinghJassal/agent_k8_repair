"""CLI: `python -m repair_faults list|inject <ID>|revert <ID>`."""

from __future__ import annotations

import argparse
import json
import sys

from repair_faults.injector import FaultError, inject, list_faults, revert


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repair_faults", description="Inject or revert demo faults.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list the fault catalog")
    for name in ("inject", "revert"):
        cmd = sub.add_parser(name, help=f"{name} a fault in the demo repo or cluster")
        cmd.add_argument("fault_id", metavar="ID", help="fault id, e.g. F1")
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            for entry in list_faults():
                print(f"{entry['id']}\t{entry['kind']}\t{entry['expected_class']}\t{entry['title']}")
            return 0
        action = inject if args.command == "inject" else revert
        print(json.dumps(action(args.fault_id), indent=2, default=str))
        return 0
    except FaultError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
