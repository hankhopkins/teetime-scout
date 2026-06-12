"""Club Caddie provider — Fox Hollow GC (St. Michael).

Club Caddie exposes a server-rendered public tee sheet (no auth):
  GET https://apimanager-cc37.clubcaddie.com/webapi/view/{view_id}/
        -> sets a session; the page carries an "Interaction" token
  GET .../webapi/view/{view_id}/slots?date=MM/DD/YYYY&player=any&ratetype=any
        [&Interaction={token}]
        -> HTML page whose cards each contain, in order:
           course tab name ("Front"), time "06:09 PM",
           price range "$30.73 - $80.00", "9 or 18 Holes", "Golfers: 1 - 2"

We parse the HTML with regexes anchored on the time string; "Golfers: 1 - N"
gives the number of open spots that can still be booked.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .base import Provider, TeeTime, log

TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*([AP]M)", re.I)
PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
GOLFERS_RE = re.compile(r"Golfers?\s*:?\s*(?:1\s*[-–]\s*)?(\d)", re.I)
HOLES_RE = re.compile(r"(?:(\d+)\s*or\s*)?(\d+)\s*Holes", re.I)
INTERACTION_RE = re.compile(r"Interaction[\"'=:\s]+([A-Za-z0-9]{16,})")


class ClubCaddieProvider(Provider):
    name = "clubcaddie"
    impersonate = True  # plain requests sometimes get the 503 session dance

    def __init__(self, course_cfg, settings):
        super().__init__(course_cfg, settings)
        c = course_cfg["clubcaddie"]
        self.base = c.get("base", "https://apimanager-cc37.clubcaddie.com").rstrip("/")
        self.view_id = c["view_id"]                # e.g. gdfdabab
        self.tz = ZoneInfo(settings["timezone"])
        self._interaction: str | None = None
        self._warmed = False

    def _warm_up(self):
        """Initial GET collects the session cookie + Interaction token."""
        if self._warmed:
            return
        self._warmed = True
        try:
            resp = self.session.get(f"{self.base}/webapi/view/{self.view_id}/",
                                    timeout=25, headers={"Accept": "text/html"})
            m = INTERACTION_RE.search(resp.text)
            if m:
                self._interaction = m.group(1)
        except Exception as e:  # noqa: BLE001
            log.debug("clubcaddie warm-up failed: %s", e)

    def _get_slots_html(self, day: date) -> tuple[str | None, str | None]:
        params = {"date": day.strftime("%m/%d/%Y"),
                  "player": "any", "ratetype": "any"}
        if self._interaction:
            params["Interaction"] = self._interaction
        try:
            resp = self.session.get(
                f"{self.base}/webapi/view/{self.view_id}/slots",
                params=params, timeout=25, headers={"Accept": "text/html"})
        except Exception as e:  # noqa: BLE001
            return None, f"Club Caddie request failed: {e}"
        if resp.status_code >= 400:
            return None, f"Club Caddie HTTP {resp.status_code}"
        # the app sometimes bounces the first request to mint a session token
        m = INTERACTION_RE.search(resp.text)
        if m and not self._interaction:
            self._interaction = m.group(1)
        return resp.text, None

    def fetch_day(self, day: date):
        self._warm_up()
        html, err = self._get_slots_html(day)
        if html is not None and not TIME_RE.search(html) and self._interaction:
            # retry once with the (possibly just-minted) token
            html, err = self._get_slots_html(day)
        if err:
            return self._result(day, error=err)
        if html is None:
            return self._result(day, error="Club Caddie returned no content")

        if "No online bookings" in html or "no tee times" in html.lower():
            return self._result(day, times=[])

        times = []
        matches = list(TIME_RE.finditer(html))
        for m in matches:
            hh_mm, ampm = m.group(1), m.group(2).upper()
            try:
                t = datetime.strptime(f"{hh_mm} {ampm}", "%I:%M %p").time()
            except ValueError:
                continue
            when = datetime(day.year, day.month, day.day, t.hour, t.minute,
                            tzinfo=self.tz)
            # look in the card body that follows the time for price/golfers/holes
            window = html[m.end():m.end() + 900]
            nxt = TIME_RE.search(window)
            if nxt:
                window = window[:nxt.start()]

            price = None
            pm = PRICE_RE.search(window)
            if pm:
                try:
                    price = float(pm.group(1).replace(",", ""))
                except ValueError:
                    price = None
            spots = None
            gm = GOLFERS_RE.search(window)
            if gm:
                try:
                    spots = int(gm.group(1))
                except ValueError:
                    spots = None
            holes = None
            hm = HOLES_RE.search(window)
            if hm:
                try:
                    holes = max(int(g) for g in hm.groups() if g)
                except ValueError:
                    holes = None

            times.append(TeeTime(when=when, open_spots=spots,
                                 price=price, holes=holes))

        if matches and not times:
            return self._result(day, error=(
                "Club Caddie page returned but no tee times parsed — "
                "layout may have changed"))
        return self._result(day, times=times)
