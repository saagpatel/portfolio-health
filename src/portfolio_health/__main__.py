"""Entry point: python -m portfolio_health."""

from __future__ import annotations

import argparse
from pathlib import Path

from portfolio_health.health import collect_health, dumps_health_json, format_health_text
from portfolio_health.indexer import DEFAULT_INDEX_PATH, DEFAULT_MEMORY_DIR
from portfolio_health.queries import DEFAULT_BRIDGE_PATH
from portfolio_health.server import mcp


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portfolio-health")
    subparsers = parser.add_subparsers(dest="command")

    health = subparsers.add_parser("health", help="Report cache/source alignment")
    health.add_argument("--index-path", type=Path, default=DEFAULT_INDEX_PATH)
    health.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY_DIR)
    health.add_argument("--bridge-path", type=Path, default=DEFAULT_BRIDGE_PATH)
    health.add_argument(
        "--refresh",
        action="store_true",
        help="Incrementally refresh the selected index before reporting",
    )
    health.add_argument(
        "--full-rebuild",
        action="store_true",
        help="Rebuild the selected index from all memory files before reporting",
    )
    health.add_argument("--json", action="store_true", help="Emit JSON")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "health":
        report = collect_health(
            index_path=args.index_path,
            memory_dir=args.memory_dir,
            bridge_path=args.bridge_path,
            refresh=args.refresh,
            full_rebuild=args.full_rebuild,
        )
        print(dumps_health_json(report) if args.json else format_health_text(report))
        return

    mcp.run()


if __name__ == "__main__":
    main()
