"""Vermont Systems WebTrac provider — Chomonix (Anoka County).

WebTrac is a parks-and-rec system whose golf search (module=GR) returns HTML,
not JSON, so this provider fetches the search page for a date and parses tee
times out of the markup. The exact result format varies by WebTrac version;
parsing is regex-based and intentionally forgiving. If the probe shows zero
times on a date you know has openings, capture the search request from
devtools and adjust `search_url` / params in config.yaml.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .base import Provider, TeeTime, log

TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap]m)\b", re.I)


class WebTracProvider(Provider):
    name = "webtrac"
    impersonate = True   # myvscloud sits behind bot protection on datacenter IPs

    def __init__(self, course_cfg, settings):
        super().__init__(course_cfg, settings)
        w = course_cfg["webtrac"]
        self.search_url = w["search_url"]
        self.params = w.get("params") or {}
        self.date_param = w.get("date_param", "begindate")
        self.date_format = w.get("date_format", "%m/%d/%Y")
        self.tz = ZoneInfo(settings["timezone"])
        self.session.headers["Accept"] = "text/html,application/xhtml+xml"

    _warmed = False

    def _warm_up(self):
        """WebTrac wants a normal browsing session (cookies from the splash
        page) before it serves search results; bare deep-links can 403."""
        if self._warmed:
            return
        self.session.headers.update({
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Upgrade-Insecure-Requests": "1",
        })
        from urllib.parse import urlsplit
        root = "{0.scheme}://{0.netloc}".format(urlsplit(self.search_url))
        for url in (root + "/wbwsc/mnanokactywt.wsc/splash.html", self.search_url):
            try:
                self.session.get(url, timeout=30)
            except Exception:  # noqa: BLE001
                pass
        self._warmed = True

    def fetch_day(self, day: date):
        self._warm_up()
        params = dict(self.params)
        params[self.date_param] = day.strftime(self.date_format)
        try:
            resp = self.session.get(self.search_url, params=params, timeout=30)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:  # noqa: BLE001
            hint = (" (a 403 here is often temporary rate-limiting from "
                    "repeated probing — it typically clears within the hour, "
                    "and twice-daily production runs rarely trip it)"
                    if "403" in str(e) else "")
            return self._result(day, error=f"WebTrac request failed: {e}{hint}")

        if "captcha" in html.lower():
            return self._result(day, error="WebTrac served a CAPTCHA — booking "
                                           "site may be rate-limiting; check manually.")

        # WebTrac result rows typically contain a time plus an Add-to-Cart /
        # availability marker. We extract times from rows that look bookable.
        times: list[TeeTime] = []
        seen: set[str] = set()
        # split into result-row-sized chunks so each time is judged in context
        chunks = re.split(r"<tr[\s>]|class=\"result", html)
        for chunk in chunks:
            low = chunk.lower()
            if not TIME_RE.search(chunk):
                continue
            # skip rows that are clearly unavailable
            if any(word in low for word in ("unavailable", "sold out", "no longer")):
                continue
            bookable = any(word in low for word in
                           ("add to cart", "addtocart", "book", "available", "reserve"))
            if not bookable:
                continue
            m = TIME_RE.search(chunk)
            hh, mm, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
            if ampm == "pm" and hh != 12:
                hh += 12
            if ampm == "am" and hh == 12:
                hh = 0
            key = f"{hh:02d}:{mm:02d}"
            if key in seen:
                continue
            seen.add(key)

            spots = None
            ms = re.search(r"(\d)\s*(?:open|spots?|players?\s+available)", low)
            if ms:
                spots = int(ms.group(1))
            price = None
            mp = re.search(r"\$\s*(\d+(?:\.\d{2})?)", chunk)
            if mp:
                price = float(mp.group(1))

            times.append(TeeTime(
                when=datetime(day.year, day.month, day.day, hh, mm, tzinfo=self.tz),
                open_spots=spots, price=price,
                holes=self.settings.get("holes", 18),
            ))

        if not times and "module=gr" not in html.lower() and "webtrac" not in html.lower():
            log.warning("WebTrac response didn't look like a tee sheet (%d bytes)",
                        len(html))
            return self._result(day, error="WebTrac response didn't look like a tee "
                                           "sheet — run probe.py and see README.")
        times.sort(key=lambda t: t.when)
        return self._result(day, times=times)
