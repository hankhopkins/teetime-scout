"""CPS legacy v3 ("Prophet Services") provider — Brookview.

Server-rendered ASP.NET app with the session token embedded in the URL path:
  https://secure.east.prophetservices.com/BrookviewGCv3/(S(...))/Home/nIndex
      ?CourseId=1,2&Date=2026-6-12&Time=AnyTime&Player=99&Hole=18

We request without the (S(...)) segment and let the redirect mint a session,
then scrape tee times out of the HTML.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .base import Provider, TeeTime

ATTR_RE = re.compile(r"teetime=['\"](\d{1,2}):(\d{2})\s*([AP]M)['\"]", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AP]M)\b", re.I)
PRICE_RE = re.compile(r"\$\s*(\d+(?:\.\d{2})?)")


class ProphetV3Provider(Provider):
    name = "prophet_v3"

    def __init__(self, course_cfg, settings):
        super().__init__(course_cfg, settings)
        p = course_cfg["prophet_v3"]
        self.base_url = p["base_url"].rstrip("/")     # .../BrookviewGCv3
        self.course_id = str(p.get("course_id", "1"))
        self.tz = ZoneInfo(settings["timezone"])
        self.session.headers["Accept"] = "text/html,application/xhtml+xml"

    def fetch_day(self, day: date):
        params = {
            "CourseId": self.course_id,
            "Date": f"{day.year}-{day.month}-{day.day}",   # no zero padding
            "Time": "AnyTime",
            "Player": "99",
            "Hole": str(self.settings.get("holes", 18)),
        }
        try:
            # first request mints the (S(...)) session, but the redirect drops
            # our query params — so request again against the session URL
            first = self.session.get(f"{self.base_url}/Home/nIndex",
                                     params=params, timeout=35,
                                     allow_redirects=True)
            first.raise_for_status()
            session_url = first.url.split("?")[0]
            resp = self.session.get(session_url, params=params, timeout=35)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:  # noqa: BLE001
            return self._result(day, error=f"Prophet v3 request failed: {e}")

        times, seen = [], set()
        # tee times live in teetime='6:30 AM' attributes on bookable slots
        matches = list(ATTR_RE.finditer(html))
        if not matches:   # fallback to plain clock times if markup changes
            matches = list(TIME_RE.finditer(html))
        for m in matches:
            hh, mm, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
            if ampm == "pm" and hh != 12:
                hh += 12
            if ampm == "am" and hh == 12:
                hh = 0
            key = f"{hh:02d}:{mm:02d}"
            if key in seen:
                continue
            seen.add(key)
            # look for a price near this match
            window = html[m.start():m.start() + 400]
            mp = PRICE_RE.search(window)
            times.append(TeeTime(
                when=datetime(day.year, day.month, day.day, hh, mm,
                              tzinfo=self.tz),
                price=float(mp.group(1)) if mp else None,
                holes=self.settings.get("holes", 18),
            ))
        times.sort(key=lambda t: t.when)
        return self._result(day, times=times)
