"""Chronogolf (Lightspeed Golf) provider — Meadowbrook, Columbia, Gross,
Baker National, Brookview, Oak Marsh (and Chaska, if its club sells online).

Modern marketplace API (captured from the live site, June 2026):
  GET https://www.chronogolf.com/marketplace/v2/teetimes
      ?start_date=YYYY-MM-DD&course_ids={uuid}[,{uuid}]&holes=18&page=N

Courses are addressed by UUID. UUIDs are discovered from the __NEXT_DATA__
JSON embedded in the club page and can be pinned in config.yaml as
chronogolf: { course_uuid: ... } to skip discovery.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .base import Provider, TeeTime, log

BASE = "https://www.chronogolf.com"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class ChronogolfProvider(Provider):
    name = "chronogolf"

    def __init__(self, course_cfg, settings):
        super().__init__(course_cfg, settings)
        c = course_cfg["chronogolf"]
        self.slug = c["slug"]
        self.course_uuid = c.get("course_uuid")      # pin this after probe
        self.course_uuids: list[str] = ([self.course_uuid] if self.course_uuid
                                        else [])
        self.tz = ZoneInfo(settings["timezone"])
        self._discovered = False
        # browsing the club page first also collects Cloudflare cookies,
        # which keeps the API friendly
        self._warmed = False

    # -- discovery --------------------------------------------------------
    @staticmethod
    def _walk(node, pred, out):
        if isinstance(node, dict):
            if pred(node):
                out.append(node)
            for v in node.values():
                ChronogolfProvider._walk(v, pred, out)
        elif isinstance(node, list):
            for v in node:
                ChronogolfProvider._walk(v, pred, out)

    def discover(self) -> dict:
        """Pull course UUIDs out of the club page's __NEXT_DATA__ blob."""
        info: dict = {"courses": []}
        try:
            html = self.session.get(f"{BASE}/club/{self.slug}", timeout=30,
                                    headers={"Accept": "text/html"}).text
            self._warmed = True
        except Exception as e:  # noqa: BLE001
            log.warning("chronogolf: club page fetch failed for %s: %s",
                        self.slug, e)
            return info
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            log.warning("chronogolf: no __NEXT_DATA__ for %s", self.slug)
            return info
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            log.warning("chronogolf: __NEXT_DATA__ parse failed for %s: %s",
                        self.slug, e)
            return info

        courses: list = []
        self._walk(data, lambda d: "holes" in d and "name" in d
                   and any(UUID_RE.match(str(d.get(k, ""))) for k in ("uuid", "id")),
                   courses)
        seen = set()
        for c in courses:
            uuid = c.get("uuid") if UUID_RE.match(str(c.get("uuid", ""))) else c.get("id")
            if uuid in seen:
                continue
            seen.add(uuid)
            info["courses"].append((uuid, c.get("name"), c.get("holes")))
        return info

    def _ensure_uuids(self):
        if self.course_uuids or self._discovered:
            return
        info = self.discover()
        self._discovered = True
        want = self.settings.get("holes", 18)
        matching = [u for u, _, h in info["courses"] if h == want]
        self.course_uuids = matching or [u for u, _, _ in info["courses"]]
        if self.course_uuids:
            log.info("chronogolf %s: course uuids %s", self.slug, self.course_uuids)

    def _warm_up(self):
        if self._warmed:
            return
        try:
            self.session.get(f"{BASE}/club/{self.slug}", timeout=30,
                             headers={"Accept": "text/html"})
        except Exception:  # noqa: BLE001
            pass
        self._warmed = True

    # -- parsing helpers ----------------------------------------------------
    def _slot_time(self, slot, day: date) -> datetime | None:
        for key in ("start_time", "startTime", "time", "teetime", "teeTime",
                    "date_time", "dateTime", "startDateTime"):
            raw = slot.get(key)
            if not raw:
                continue
            s = str(raw)
            if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
                hh, mm = int(s.split(":")[0]), int(s.split(":")[1])
                return datetime(day.year, day.month, day.day, hh, mm, tzinfo=self.tz)
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                return (dt.replace(tzinfo=self.tz) if dt.tzinfo is None
                        else dt.astimezone(self.tz))
            except ValueError:
                continue
        return None

    @staticmethod
    def _first_number(node, keys):
        """Find the first numeric value under any of `keys`, searching nested
        dicts/lists one level of indirection deep."""
        if isinstance(node, dict):
            for k in keys:
                v = node.get(k)
                if isinstance(v, (int, float)):
                    return v
                if isinstance(v, str):
                    try:
                        return float(v)
                    except ValueError:
                        pass
            for v in node.values():
                if isinstance(v, (dict, list)):
                    found = ChronogolfProvider._first_number(v, keys)
                    if found is not None:
                        return found
        elif isinstance(node, list):
            for v in node:
                found = ChronogolfProvider._first_number(v, keys)
                if found is not None:
                    return found
        return None

    # -- fetch ---------------------------------------------------------------
    def fetch_day(self, day: date):
        self._ensure_uuids()
        if not self.course_uuids:
            return self._result(day, error=(
                "Chronogolf course UUID not discovered — run probe.py; if the "
                "club page lists no courses it may not sell tee times online."))
        self._warm_up()

        holes = self.settings.get("holes", 18)
        times, page = [], 1
        while page <= 5:
            params = {
                "start_date": day.isoformat(),
                "course_ids": ",".join(str(u) for u in self.course_uuids),
                "holes": str(holes),
                "page": str(page),
            }
            try:
                resp = self.session.get(f"{BASE}/marketplace/v2/teetimes",
                                        params=params, timeout=25,
                                        headers={"Accept": "application/json",
                                                 "Referer": f"{BASE}/club/{self.slug}"})
                if resp.status_code >= 400:
                    return self._result(day, error=(
                        f"Chronogolf v2 HTTP {resp.status_code}: "
                        f"{resp.text[:200]}".replace("\n", " ")))
                data = resp.json()
            except Exception as e:  # noqa: BLE001
                return self._result(day, error=f"Chronogolf v2 request failed: {e}")

            slots = None
            if isinstance(data, list):
                slots = data
            elif isinstance(data, dict):
                for key in ("teetimes", "tee_times", "data", "results", "items"):
                    if isinstance(data.get(key), list):
                        slots = data[key]
                        break
            if slots is None:
                return self._result(day, error=(
                    f"Chronogolf v2 payload shape unrecognized: "
                    f"{json.dumps(data)[:250]}"))
            if not slots:
                break

            parsed_any = False
            for slot in slots:
                when = self._slot_time(slot, day)
                if when is None:
                    continue
                if when.date() != day:        # API may roll into next dates
                    continue
                if slot.get("out_of_capacity"):
                    continue
                parsed_any = True
                spots = self._first_number(slot, (
                    "free_slots", "freeSlots", "available_spots",
                    "availableSpots", "max_players", "maxPlayers"))
                price = self._first_number(slot, (
                    "green_fee", "greenFee", "price", "rate", "amount"))
                times.append(TeeTime(
                    when=when,
                    open_spots=int(spots) if spots is not None else None,
                    price=float(price) if price is not None else None,
                    holes=holes,
                ))
            if not parsed_any and slots:
                return self._result(day, error=(
                    f"Chronogolf v2 returned {len(slots)} slots but none parsed — "
                    f"first slot: {json.dumps(slots[0])[:250]}"))
            page += 1

        return self._result(day, times=times)
