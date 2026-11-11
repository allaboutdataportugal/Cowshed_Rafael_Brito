"""Command line entry point: ``cowshed serve | simulate | report | schedule``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import httpx

from cowshed.config import Settings
from cowshed.db import create_db_engine, init_db
from cowshed.jobs import run_daily_report, run_forever
from cowshed.reporting import build_daily_report, render_json, render_text
from cowshed.repository import Repository
from cowshed.simulator import run_simulation


def _repo(settings: Settings) -> Repository:
    engine = create_db_engine(settings.database_url, settings.sqlite_synchronous)
    init_db(engine)
    return Repository(engine)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cowshed", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--access-log", action="store_true", help="log every request")

    sim = sub.add_parser("simulate", help="replay the parquet datasets against the API")
    sim.add_argument("--data-dir", type=Path, required=True)
    sim.add_argument("--api-url", default="http://127.0.0.1:8000")
    sim.add_argument("--concurrency", type=int, default=8)
    sim.add_argument("--limit", type=int, default=None, help="only send the first N measurements")
    sim.add_argument(
        "--since", type=date.fromisoformat, default=None, help="only send measurements from YYYY-MM-DD (UTC)"
    )

    report = sub.add_parser("report", help="generate the daily report for one date")
    report.add_argument(
        "--date", type=date.fromisoformat, default=None, help="YYYY-MM-DD (default: yesterday)"
    )
    report.add_argument("--format", choices=["text", "json"], default="text")
    report.add_argument(
        "--publish",
        metavar="DEST",
        default=None,
        help="write .txt and .json files to this folder instead of stdout",
    )

    schedule = sub.add_parser("schedule", help="generate yesterday's report every day at a fixed time")
    schedule.add_argument("--at", type=time.fromisoformat, default=time(1, 0), help="HH:MM (default 01:00)")
    schedule.add_argument("--publish", metavar="DEST", default=None, help="default: COWSHED_REPORT_DIR")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise
    settings = Settings.from_env()

    if args.command == "serve":
        import uvicorn

        from cowshed.api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=args.access_log)
    elif args.command == "simulate":

        async def _go() -> int:
            def make_client() -> httpx.AsyncClient:
                limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
                return httpx.AsyncClient(base_url=args.api_url, timeout=30, limits=limits)

            result = await run_simulation(
                make_client, args.data_dir, args.concurrency, args.limit, args.since
            )
            failed = result.cows.failed + result.sensors.failed + result.measurements.failed
            for sample in result.measurements.rejected_samples:
                logging.warning("rejected: %s", sample)
            return 1 if failed else 0

        return asyncio.run(_go())
    elif args.command == "report":
        tz = settings.tz
        day = args.date or (datetime.now(tz).date() - timedelta(days=1))
        repo = _repo(settings)
        if args.publish:
            for path in run_daily_report(repo, day, tz, args.publish, settings.illness):
                print(path)
        else:
            report = build_daily_report(repo, day, tz, settings.illness)
            sys.stdout.write(render_text(report) if args.format == "text" else render_json(report))
    elif args.command == "schedule":
        run_forever(
            _repo(settings), settings.tz, args.publish or settings.report_dir, args.at, settings.illness
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
