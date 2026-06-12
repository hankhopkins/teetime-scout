"""Chronogolf (Lightspeed Golf) provider — Meadowbrook, Columbia, Gross,
Baker National, Chaska Town Course, Brookview, Oak Marsh.

Discovery:
  GET https://www.chronogolf.com/marketplace/clubs/{slug}        (JSON club record)
  — falls back to scraping numeric ids out of the club page HTML.

Tee times (marketplace API):
  GET https://www.chronogolf.com/marketplace/clubs/{club_id}/teetimes
      ?date=YYYY-MM-DD&course_id={course_id}&nb_holes=18
      &affiliation_type_ids[]=X&affiliation_type_ids[]=X   (one per player)

Run probe.py once: it prints the discovered club_id / course_id /
affiliation_type_id so you can pin them in config.yaml and skip discovery.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .base import Provider, TeeTime, log

BASE = "https://www.chronogolf.com"


class ChronogolfProvider(Provider):
    name = "chronogolf"

    def __init__(self, course_cfg, settings):
        super().__init__(course_cfg, settings)
        c = course_cfg["chronogolf"]
        self.slug = c["slug"]
        self.club_id = c.get("club_id")
        self.course_id = c.get("course_id")
        self.affiliation_type_id = c.get("affiliation_type_id")
        self.tz = ZoneInfo(settings["timezone"])
        self._discovered = False

    # -- discovery -------------------------------------------------------------
    def discover(self) -> dict:
        """Resolve club_id, course_id, affiliation_type_id from the slug."""
        info: dict = {}
        for url in (f"{BASE}/marketplace/clubs/{self.slug}",
                    f"{BASE}/api/v2/clubs/{self.slug}"):
            try:
                data = self._get_json(url)
                club = data.get("club", data)
                info["club_id"] = club.get("id")
                courses = club.get("courses") or []
                want_holes = self.settings.get("holes", 18)
                # prefer an 18-hole course; else first listed
                pick = next((c for c in courses if c.get("holes") == want_holes), None) \
                    or (courses[0] if courses else None)
                if pick:
                    info["course_id"] = pick.get("id")
                    info["courses"] = [(c.get("id"), c.get("name"), c.get("holes"))
                                       for c in courses]
                # affiliation types live on the club or course green fees
                affs = club.get("affiliation_types") or []
                pub = next((a for a in affs if "public" in (a.get("name") or "").lower()), None) \
                    or (affs[0] if affs else None)
                if pub:
                    info["affiliation_type_id"] = pub.get("id")
                if info.get("club_id"):
                    return info
            except Exception as e:  # noqa: BLE001
                log.debug("chronogolf discovery via %s failed: %s", url, e)

        # last resort: scrape ids from the public club page HTML
        try:
            html = self.session.get(f"{BASE}/club/{self.slug}", timeout=25,
                                    headers={"Accept": "text/html"}).text
            m = re.search(r'"club"\s*:\s*{\s*"id"\s*:\s*(\d+)', html) \
                or re.search(r'club_id["\']?\s*[:=]\s*["\']?(\d+)', html)
            if m:
                info["club_id"] = int(m.group(1))
            m = re.search(r'"course(?:_id)?"\s*:\s*{?\s*"?id"?\s*:?\s*(\d+)', html)
            if m:
                info["course_id"] = int(m.group(1))
        except Exception as e:  # noqa: BLE001
            log.warning("chronogolf HTML discovery failed for %s: %s", self.slug, e)
        return info

    def _ensure_ids(self):
        if self.club_id and self.course_id and self.affiliation_type_id:
            return
        if self._discovered:
            return
        info = self.discover()
        self.club_id = self.club_id or info.get("club_id")
        self.course_id = self.course_id or info.get("course_id")
        self.affiliation_type_id = self.affiliation_type_id or info.get("affiliation_type_id")
        self._discovered = True

    # -- fetch -------------------------------------------------------------
    def fetch_day(self, day: date):
        self._ensure_ids()
        if not self.club_id:
            return self._result(day, error=(
                "Chronogolf club_id could not be discovered automatically — "
                "run probe.py and pin ids in config.yaml (see README)."))

        params: list[tuple[str, str]] = [
            ("date", day.isoformat()),
            ("nb_holes", str(self.settings.get("holes", 18))),
        ]
        if self.course_id:
            params.append(("course_id", str(self.course_id)))
        n_players = max(self.settings.get("min_open_spots", 2), 1)
        if self.affiliation_type_id:
            params += [("affiliation_type_ids[]", str(self.affiliation_type_id))] * n_players

        try:
            data = self._get_json(f"{BASE}/marketplace/clubs/{self.club_id}/teetimes",
                                  params=params)
        except Exception as e:  # noqa: BLE001
            return self._result(day, error=f"Chronogolf request failed: {e}")

        times = []
        slots = data if isinstance(data, list) else data.get("teetimes", [])
        for slot in slots:
            try:
                if slot.get("out_of_capacity"):
                    continue
                start = slot.get("start_time") or slot.get("time")
                when = datetime.combine(
                    day, datetime.strptime(start, "%H:%M").time(), tzinfo=self.tz)
                fees = slot.get("green_fees") or []
                price = None
                if fees:
                    raw = fees[0].get("green_fee") or fees[0].get("price")
                    price = float(raw) if raw is not None else None
                free = slot.get("free_slots")
                if free is None and fees:
                    free = len(fees)
                times.append(TeeTime(when=when, open_spots=free, price=price,
                                     holes=self.settings.get("holes", 18)))
            except Exception:  # noqa: BLE001
                continue
        return self._result(day, times=times)
