"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from cowshed.domain import IllnessThresholds


@dataclass(frozen=True)
class Settings:
    """Application settings.

    All values can be overridden with ``COWSHED_*`` environment variables so the same
    image runs unchanged on a laptop, in Docker or in a cluster.
    """

    database_url: str = "sqlite:///./data/cowshed.db"
    report_timezone: str = "UTC"
    report_dir: str = "./reports"
    sqlite_synchronous: str = "FULL"
    illness: IllnessThresholds = field(default_factory=IllnessThresholds)

    def __post_init__(self) -> None:
        ZoneInfo(self.report_timezone)  # fail fast on an unknown timezone
        if self.sqlite_synchronous.upper() not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
            raise ValueError(f"invalid COWSHED_SQLITE_SYNCHRONOUS: {self.sqlite_synchronous}")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.report_timezone)

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        return cls(
            database_url=os.getenv("COWSHED_DATABASE_URL", defaults.database_url),
            report_timezone=os.getenv("COWSHED_REPORT_TIMEZONE", defaults.report_timezone),
            report_dir=os.getenv("COWSHED_REPORT_DIR", defaults.report_dir),
            sqlite_synchronous=os.getenv("COWSHED_SQLITE_SYNCHRONOUS", defaults.sqlite_synchronous),
        )
