"""Thin CLI dispatcher for polymarket-companion.

    python main.py scan     - run one discovery scan cycle
    python main.py analyze  - run the Stage 2 feature/probability/edge/risk/score pipeline
    python main.py resolve  - check previously-analyzed markets for resolution outcomes
    python main.py paper    - open/settle simulated paper trades from the signal journal
    python main.py initdb   - create the SQLite schema
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.analysis.pipeline import run_analysis_once
from app.config.loader import load_config
from app.notify.telegram import notify_paper_trading_events
from app.paper.paper_trading import run_paper_trading_once
from app.polymarket.resolutions import check_pending_resolutions
from app.polymarket.scanner import run_scan_once
from app.storage.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(prog="polymarket-companion")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan", help="Run one discovery scan cycle")
    subparsers.add_parser("analyze", help="Run the feature/probability/edge/risk/score pipeline")
    subparsers.add_parser("resolve", help="Check previously-analyzed markets for resolution outcomes")
    subparsers.add_parser("paper", help="Open/settle simulated paper trades from the signal journal")
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

    if args.command == "scan":
        summary = asyncio.run(run_scan_once(config, db))
        print(summary)
        return

    if args.command == "analyze":
        summary = asyncio.run(run_analysis_once(config, db))
        print(summary)
        return

    if args.command == "resolve":
        summary = asyncio.run(check_pending_resolutions(config, db))
        print(summary)
        return

    if args.command == "paper":
        summary = run_paper_trading_once(config, db)
        notify_summary = notify_paper_trading_events(
            config, db, summary.creation.created_trades, summary.settlement.settled_trades
        )
        print(summary)
        print(notify_summary)
        return


if __name__ == "__main__":
    main()
