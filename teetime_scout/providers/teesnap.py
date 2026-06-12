"""Teesnap provider — Amery Golf Club (WI).

Public JSON endpoint on the club's own subdomain (captured live, June 2026):
  GET https://{subdomain}.teesnap.net/customer-api/teetimes-day
      ?course={course_id}&date=YYYY-MM-DD&players=any&holes=any

Response shape:
  { "teeTimes": {
      "bookings":  [ {"bookingId": 123, "golfers": [...], ...}, ... ],
      "teeTimes":  [ {"teeTime": "2026-06-14T10:00:00",
                      "prices": [{"roundType": "EIGHTEEN_HOLE", "price": "42.00",
                                  "priceWithAddOn": "62.00"}, ...],
                      "teeOffSections": [{"teeOff": "FRONT_NINE",
                                          "bookings": [52594684],
                                          "isHeld": false}], ...}, ... ] } }

Open spots = 4 minus the golfers already attached to the slot's bookings.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .base import Provider, TeeTime

MAX_PLAYERS = 4


class TeesnapProvider(Provider):
    name = "teesnap"

    def __init__(self, course_cfg, settings):
        super().__init__(course_cfg, settings)
        t = course_cfg["teesnap"]
        self.subdomain = t["subdomain"]            # e.g. amerygolfclub
        self.course_id = str(t["course_id"])       # e.g. 1068
        self.tz = ZoneInfo(settings["timezone"])

    def fetch_day(self, day: date):
        url = (f"https://{self.subdomain}.teesnap.net/customer-api/teetimes-day")
        try:
            data = self._get_json(url, params={
                "course": self.course_id,
                "date": day.isoformat(),
                "players": "any",
                "holes": "any",
            }, headers={"Referer": f"https://{self.subdomain}.teesnap.net/"})
        except Exception as e:  # noqa: BLE001
            return self._result(day, error=f"Teesnap request failed: {e}")

        root = data.get("teeTimes") if isinstance(data, dict) else None
        if not isinstance(root, dict):
            return self._result(day, error=(
                f"Teesnap payload shape unrecognized: {str(data)[:200]}"))

        # bookingId -> number of golfers already in that booking
        golfers_per_booking: dict = {}
        for b in root.get("bookings") or []:
            bid = b.get("bookingId") or b.get("id")
            if bid is not None:
                golfers_per_booking[str(bid)] = len(b.get("golfers") or [])

        want_holes = self.settings.get("holes", 18)
        want_round = "EIGHTEEN_HOLE" if want_holes == 18 else "NINE_HOLE"

        times = []
        for slot in root.get("teeTimes") or []:
            raw = slot.get("teeTime")
            if not raw:
                continue
            try:
                when = datetime.fromisoformat(str(raw))
            except ValueError:
                continue
            when = (when.replace(tzinfo=self.tz) if when.tzinfo is None
                    else when.astimezone(self.tz))
            if when.date() != day:
                continue

            sections = slot.get("teeOffSections") or []
            if sections and all(s.get("isHeld") for s in sections):
                continue
            taken = 0
            for s in sections:
                for bid in s.get("bookings") or []:
                    taken += golfers_per_booking.get(str(bid), 0)
            spots = max(0, MAX_PLAYERS - taken)
            if spots == 0:
                continue

            price, holes = None, None
            prices = slot.get("prices") or []
            for p in prices:
                if p.get("roundType") == want_round and p.get("price") is not None:
                    try:
                        price = float(p["price"])
                        holes = want_holes
                    except (TypeError, ValueError):
                        pass
                    break
            if price is None and prices:
                try:
                    price = float(prices[0].get("price"))
                except (TypeError, ValueError):
                    price = None

            times.append(TeeTime(when=when, open_spots=spots,
                                 price=price, holes=holes))
        return self._result(day, times=times)
