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


def _dig(node, path):
    """Resolve a dot-path like 'availability.available_spots' inside a slot."""
    if not path:
        return None
    for key in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


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
        if not isinstance(node, list) and isinstance(node, dict):
            # no list_path configured (or wrong) — try common containers
            for key in ("times", "tee_times", "teetimes", "teeTimes", "data",
                        "results", "items", "slots", "list"):
                if isinstance(node.get(key), list):
                    node = node[key]
                    break
        if not isinstance(node, list):
            import json as _json
            return self._result(day, error=(
                f"couldn't find the tee-time array in the response — "
                f"payload starts: {_json.dumps(data)[:250]}"))

        times = []
        for slot in node:
            raw = slot.get(self.time_field)
            if raw is None:
                continue
            when = None
            s = str(raw).strip()
            fmts = ([self.time_format] if self.time_format not in ("iso", "auto")
                    else [])
            try:
                when = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                for fmt in fmts + ["%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p",
                                   "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]:
                    try:
                        parsed = datetime.strptime(s, fmt)
                        if parsed.year == 1900:   # time-only format
                            when = datetime.combine(day, parsed.time())
                        else:
                            when = parsed
                        break
                    except ValueError:
                        continue
            if when is None:
                continue
            when = (when.replace(tzinfo=self.tz) if when.tzinfo is None
                    else when.astimezone(self.tz))
            if when.date() != day:
                continue
            spots = _dig(slot, self.spots_field)
            price = _dig(slot, self.price_field)
            try:
                times.append(TeeTime(
                    when=when,
                    open_spots=int(spots) if spots is not None else None,
                    price=float(price) if price is not None else None,
                ))
            except (TypeError, ValueError):
                times.append(TeeTime(when=when))
        if node and not times:
            import json as _json
            return self._result(day, error=(
                f"{len(node)} slots returned but none parsed — first slot: "
                f"{_json.dumps(node[0])[:250]}"))
        return self._result(day, times=times)
