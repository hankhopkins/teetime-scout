"""CPS Golf (Club Prophet) provider — Highland National, Edinburgh USA,
Victory Links.

CPS online-reservation sites vary by version, so this provider tries the
known endpoint shapes in order. If none work for a given site, the digest
shows a clear error and probe.py prints instructions for capturing the real
XHR URL from browser devtools, which you then paste into config.yaml as
`api_url_template` (with {date} where the YYYY-MM-DD date goes).
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .base import Provider, TeeTime, log

CANDIDATE_TEMPLATES = [
    # newer onlineresweb builds
    "https://{site}.cps.golf/onlineresweb/api/v1/teetimes/GetAvailableTeeTimes"
    "?searchDate={date}&holes={holes}&numberOfPlayer=0&courseIds=&searchTimeType=0",
    "https://{site}.cps.golf/onlineres/onlineapi/api/v1/onlinereservation/TeeTimes"
    "?searchDate={date}&holes={holes}&numberOfPlayer=0",
]


class CPSProvider(Provider):
    name = "cps"

    def __init__(self, course_cfg, settings):
        super().__init__(course_cfg, settings)
        c = course_cfg["cps"]
        self.site = c["site"]
        self.manual_template = c.get("api_url_template")  # set after devtools capture
        self.extra_headers = c.get("headers") or {}
        self.tz = ZoneInfo(settings["timezone"])
        self.session.headers.update({
            "x-componentid": "1",
            "x-websitename": self.site,
            "Referer": f"https://{self.site}.cps.golf/onlineresweb/",
            **self.extra_headers,
        })

    def _candidates(self, day: date):
        holes = self.settings.get("holes", 18)
        if self.manual_template:
            yield self.manual_template.format(date=day.isoformat(), site=self.site,
                                              holes=holes)
            return
        for t in CANDIDATE_TEMPLATES:
            yield t.format(site=self.site, date=day.isoformat(), holes=holes)

    @staticmethod
    def _extract_slots(data):
        """CPS payloads differ; find the list of tee time dicts wherever it is."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("teeTimes", "teetimes", "data", "result", "items"):
                v = data.get(key)
                if isinstance(v, list):
                    return v
        return []

    def fetch_day(self, day: date):
        last_err = None
        for url in self._candidates(day):
            try:
                data = self._get_json(url)
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue

            slots = self._extract_slots(data)
            times = []
            for slot in slots:
                raw = (slot.get("startTime") or slot.get("teeTime")
                       or slot.get("time") or slot.get("startDateTime"))
                if not raw:
                    continue
                try:
                    when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    when = (when.replace(tzinfo=self.tz) if when.tzinfo is None
                            else when.astimezone(self.tz))
                except ValueError:
                    try:
                        when = datetime.combine(
                            day, datetime.strptime(str(raw), "%H:%M").time(),
                            tzinfo=self.tz)
                    except ValueError:
                        continue
                spots = (slot.get("availableParticipantNo")
                         or slot.get("availableSpots") or slot.get("maxPlayer"))
                if isinstance(spots, list):
                    spots = max(spots) if spots else None
                price = slot.get("shItemPrice") or slot.get("price") or slot.get("greenFee")
                times.append(TeeTime(
                    when=when,
                    open_spots=int(spots) if spots is not None else None,
                    price=float(price) if price is not None else None,
                    holes=self.settings.get("holes", 18),
                ))
            if times or slots == []:
                # endpoint responded sanely (possibly genuinely empty)
                return self._result(day, times=times)

        log.warning("CPS %s: all endpoints failed (%s)", self.site, last_err)
        return self._result(day, error=(
            f"CPS endpoint for '{self.site}' not responding to known URL shapes "
            f"(last error: {last_err}). Run probe.py for capture instructions."))
