"""Daily report job: generate a report and write it as .txt and .json files.

The ``scheduler`` container (see ``terraform/``) runs ``cowshed schedule``, which calls
this once a day for the previous day. Re-running for a date overwrites that date's
files, so the job is idempotent and safe to backfill (``cowshed report --publish``).
"""

from __future__ import annotations

import logging
import time as time_module
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from typeguard import typechecked

from cowshed.domain import IllnessThresholds
from cowshed.reporting import build_daily_report, render_json, render_text
from cowshed.repository import Repository

log = logging.getLogger(__name__)


@typechecked
def publish(content: str, destination: str, filename: str) -> str:
    """Atomically write ``content`` to ``destination/filename``.

    Writing to a temporary file and renaming means a reader never sees a half-written
    report, even if the job is killed mid-write.
    """
    path = Path(destination) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return str(path)


@typechecked
def run_daily_report(
    repo: Repository,
    report_date: date,
    tz: ZoneInfo,
    destination: str,
    thresholds: IllnessThresholds | None = None,
) -> list[str]:
    report = build_daily_report(repo, report_date, tz, thresholds)
    stem = f"date={report_date.isoformat()}/cowshed_report_{report_date.isoformat()}"
    written = [
        publish(render_text(report), destination, f"{stem}.txt"),
        publish(render_json(report), destination, f"{stem}.json"),
    ]
    log.info(
        "report %s: %d cows, %d possible illness -> %s",
        report_date,
        len(report.cows),
        len(report.possible_illness),
        written,
    )
    return written


@typechecked
def next_run(now: datetime, at: time) -> datetime:
    """Next occurrence of wall-clock time ``at`` in ``now``'s timezone, strictly after ``now``."""
    candidate = datetime.combine(now.date(), at, tzinfo=now.tzinfo)
    if candidate <= now:
        candidate = datetime.combine(now.date() + timedelta(days=1), at, tzinfo=now.tzinfo)
    return candidate


def run_forever(
    repo: Repository, tz: ZoneInfo, destination: str, at: time, thresholds: IllnessThresholds | None = None
) -> None:  # pragma: no cover - loop
    """Minimal in-process scheduler for local use: every day at ``at``, report yesterday."""
    while True:
        now = datetime.now(tz)
        target = next_run(now, at)
        log.info("next report at %s", target.isoformat())
        time_module.sleep(max(0.0, (target - now).total_seconds()))
        try:
            run_daily_report(repo, target.date() - timedelta(days=1), tz, destination, thresholds)
        except Exception:  # keep the scheduler alive; the failure is logged and alertable
            log.exception("daily report failed")
