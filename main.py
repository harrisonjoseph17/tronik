"""Thin CLI dispatcher for polymarket-companion.

    python main.py scan     - run one discovery scan cycle
    python main.py initdb   - create the SQLite schema
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.config.loader import load_config
from app.polymarket.scanner import run_scan_once
from app.storage.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(prog="polymarket-companion")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan", help="Run one discovery scan cycle")
    subparsers.add_parser("initdb", help="Create the SQLite schema")
    args = parser.parse_args()

    config = load_config()
    logging.basicConfig(level=config.log_level)
    db = Database(config.db_path)

    if args.command == "initdb":
        db.init_schema()
        print(f"Initialized schema at {config.db_path}")
        return

    db.init_schema()
    summary = asyncio.run(run_scan_once(config, db))
    print(summary)


if __name__ == "__main__":
    main()
