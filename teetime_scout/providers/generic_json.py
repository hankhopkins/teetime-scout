"""Generic JSON provider — for platforms without a prebuilt adapter
(currently TeeWire / Inver Wood, which launched in 2026).

One-time setup: open the booking site with browser devtools → Network → XHR,
pick a date, and copy the request URL that returns tee times as JSON. Paste it
into config.yaml as url_template with {date} substituted in, then map the
fields. Example:

  generic:
    url_template: "https://teewire.app/api/inverwood/teetimes?date={date}"
    list_path: "data.teetimes"        # dot-path to the array ("" if root is the array)
    time_field: "start"               # field holding the tee time
    time_format: "%Y-%m-%dT%H:%M:%S"  # strptime format, or "iso"
    spots_field: "openSlots"
    price_field: "rate"
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .base import Provider, TeeTime


class GenericJSONProvider(Provider):
    name = "generic_json"

    def __init__(self, course_cfg, settings):
        super().__init__(course_cfg, settings)
        g = course_cfg["generic"]
        self.url_template = (g.get("url_template") or "").strip()
        self.list_path = g.get("list_path") or ""
        self.time_field = g.get("time_field", "time")
        self.time_format = g.get("time_format", "iso")
        self.spots_field = g.get("spots_field")
        self.price_field = g.get("price_field")
        self.headers = g.get("headers") or {}
        self.tz = ZoneInfo(settings["timezone"])

    def fetch_day(self, day: date):
        if not self.url_template:
            return self._result(day, error=(
                "No url_template configured yet — capture the tee-time XHR from the "
                "booking site once (README §TeeWire) and paste it into config.yaml."))

        try:
            data = self._get_json(self.url_template.format(date=day.isoformat()),
                                  headers=self.headers)
        except Exception as e:  # noqa: BLE001
            return self._result(day, error=f"Request failed: {e}")

        node = data
        for key in [k for k in self.list_path.split(".") if k]:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        if not isinstance(node, list):
            return self._result(day, error=f"list_path '{self.list_path}' did not "
                                           f"resolve to an array")

        times = []
        for slot in node:
            raw = slot.get(self.time_field)
            if raw is None:
                continue
            try:
                if self.time_format == "iso":
                    when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                else:
                    when = datetime.strptime(str(raw), self.time_format)
                when = (when.replace(tzinfo=self.tz) if when.tzinfo is None
                        else when.astimezone(self.tz))
            except ValueError:
                continue
            spots = slot.get(self.spots_field) if self.spots_field else None
            price = slot.get(self.price_field) if self.price_field else None
            times.append(TeeTime(
                when=when,
                open_spots=int(spots) if spots is not None else None,
                price=float(price) if price is not None else None,
            ))
        return self._result(day, times=times)
