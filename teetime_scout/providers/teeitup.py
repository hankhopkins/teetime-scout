"""TeeItUp (Golf Genius / Kenna) provider — used by Ramsey County (Keller).

Public JSON endpoint:
  GET https://phx-api-be-east-1b.kenna.io/v2/tee-times?date=YYYY-MM-DD&facilityIds={id}
  header: x-be-alias: {alias}     e.g. ramsey-county-golf
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .base import Provider, TeeTime

API = "https://phx-api-be-east-1b.kenna.io/v2/tee-times"


class TeeItUpProvider(Provider):
    name = "teeitup"

    def __init__(self, course_cfg, settings):
        super().__init__(course_cfg, settings)
        t = course_cfg["teeitup"]
        self.alias = t["alias"]
        self.facility_id = t["facility_id"]
        self.tz = ZoneInfo(settings["timezone"])

    def fetch_day(self, day: date):
        try:
            data = self._get_json(
                API,
                params={"date": day.isoformat(), "facilityIds": self.facility_id},
                headers={"x-be-alias": self.alias},
            )
        except Exception as e:  # noqa: BLE001
            return self._result(day, error=f"TeeItUp request failed: {e}")

        times = []
        try:
            buckets = data if isinstance(data, list) else [data]
            for bucket in buckets:
                for slot in bucket.get("teetimes", []):
                    raw = slot.get("teetime") or slot.get("teeTime")
                    if not raw:
                        continue
                    when = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(self.tz)

                    price = None
                    holes = None
                    for rate in slot.get("rates", []):
                        cents = rate.get("greenFeeWalking") or rate.get("greenFeeCart")
                        h = rate.get("holes")
                        if cents and (holes is None or h == self.settings.get("holes", 18)):
                            price = cents / 100.0
                            holes = h
                    times.append(TeeTime(
                        when=when,
                        open_spots=slot.get("maxPlayers"),
                        price=price,
                        holes=holes,
                    ))
        except Exception as e:  # noqa: BLE001
            return self._result(day, error=f"TeeItUp parse failed: {e}")

        return self._result(day, times=times)
