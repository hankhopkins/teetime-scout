"""ForeUp provider (Braemar).

Public JSON endpoint:
  GET https://foreupsoftware.com/index.php/api/booking/times
      ?time=all&date=MM-DD-YYYY&holes=all&players=0
      &schedule_id={schedule_id}&booking_class={booking_class}&api_key=no_limits

The booking_class (online/public price class) is auto-discovered from the
booking page HTML on first use and cached for the run.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .base import Provider, TeeTime, log

API = "https://foreupsoftware.com/index.php/api/booking/times"
PAGE = "https://foreupsoftware.com/index.php/booking/{course_id}/{schedule_id}"


class ForeUpProvider(Provider):
    name = "foreup"

    def __init__(self, course_cfg, settings):
        super().__init__(course_cfg, settings)
        fu = course_cfg["foreup"]
        self.course_id = fu["course_id"]
        self.schedule_id = fu["schedule_id"]
        self.booking_class = fu.get("booking_class")
        self.tz = ZoneInfo(settings["timezone"])

    def _discover_booking_class(self) -> int | None:
        """Scrape the booking page for its public/online booking class id."""
        try:
            html = self.session.get(
                PAGE.format(course_id=self.course_id, schedule_id=self.schedule_id),
                timeout=25,
            ).text
            m = re.search(r'"booking_classes"\s*:\s*(\[.*?\])\s*[,}]', html, re.S)
            if not m:
                # fallback: any "booking_class_id": N occurrences
                ids = re.findall(r'booking_class(?:_id)?["\']?\s*[:=]\s*["\']?(\d+)', html)
                return int(ids[0]) if ids else None
            classes = json.loads(m.group(1))
            # prefer a class whose name suggests general public online booking
            for c in classes:
                name = (c.get("name") or "").lower()
                if "online" in name or "public" in name:
                    return int(c["booking_class_id"] if "booking_class_id" in c else c["id"])
            if classes:
                c = classes[0]
                return int(c.get("booking_class_id", c.get("id")))
        except Exception as e:  # noqa: BLE001
            log.warning("foreup booking-class discovery failed: %s", e)
        return None

    def fetch_day(self, day: date):
        if self.booking_class is None:
            self.booking_class = self._discover_booking_class()

        params = {
            "time": "all",
            "date": day.strftime("%m-%d-%Y"),
            "holes": "all",
            "players": 0,
            "schedule_id": self.schedule_id,
            "schedule_ids[]": self.schedule_id,
            "specials_only": 0,
            "api_key": "no_limits",
        }
        if self.booking_class:
            params["booking_class"] = self.booking_class

        try:
            data = self._get_json(API, params=params)
        except Exception as e:  # noqa: BLE001
            return self._result(day, error=f"ForeUp request failed: {e}")

        if not isinstance(data, list):
            return self._result(day, error=f"ForeUp returned unexpected payload: {str(data)[:200]}")

        times = []
        for slot in data:
            try:
                when = datetime.strptime(slot["time"], "%Y-%m-%d %H:%M").replace(tzinfo=self.tz)
                times.append(TeeTime(
                    when=when,
                    open_spots=slot.get("available_spots"),
                    price=slot.get("green_fee"),
                    holes=int(slot["holes"]) if str(slot.get("holes", "")).isdigit() else None,
                ))
            except Exception:  # noqa: BLE001
                continue
        return self._result(day, times=times)
