"""Shared data model + base class for tee-sheet providers."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, date

import requests

log = logging.getLogger("teetime_scout")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


@dataclass
class TeeTime:
    """One available tee time, normalized across providers."""
    when: datetime              # tz-aware, local course time
    open_spots: int | None = None
    price: float | None = None  # per-player green fee, dollars
    holes: int | None = None
    note: str = ""


@dataclass
class FetchResult:
    """Times found for one course on one date, or an error explaining why not."""
    course_name: str
    day: date
    times: list[TeeTime] = field(default_factory=list)
    error: str | None = None


class Provider:
    """Base class. Subclasses implement fetch_day()."""

    name = "base"

    def __init__(self, course_cfg: dict, settings: dict):
        self.cfg = course_cfg
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "application/json"})

    # -- interface ------------------------------------------------------------
    def fetch_day(self, day: date) -> FetchResult:  # pragma: no cover
        raise NotImplementedError

    # -- helpers --------------------------------------------------------------
    def _result(self, day: date, times=None, error=None) -> FetchResult:
        return FetchResult(self.cfg["name"], day, times or [], error)

    def _get_json(self, url: str, **kw):
        resp = self.session.get(url, timeout=25, **kw)
        resp.raise_for_status()
        return resp.json()
