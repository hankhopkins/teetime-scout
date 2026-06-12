"""probe.py — first-run health check. Tests every course in config.yaml against
tomorrow's tee sheet and tells you exactly what works, what doesn't, and what
to pin in the config.

  python probe.py
  python probe.py --course "Braemar Championship"
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

from teetime_scout.main import PROVIDERS
from teetime_scout.providers.chronogolf import ChronogolfProvider

DEVTOOLS_HELP = """
   How to capture the real endpoint (one-time, ~2 minutes):
   1. Open the booking site in Chrome, press F12 → Network tab → filter XHR/Fetch.
   2. Pick a date on the tee sheet; watch which request returns the times as JSON.
   3. Right-click it → Copy → Copy URL. Replace the date portion with {date}.
   4. Paste into config.yaml under this course:
        cps:       api_url_template: "<url with {date}>"
      or for generic_json:
        generic:   url_template: "<url with {date}>"
   5. If the request needed special headers (devtools → Headers → Request Headers),
      add them under headers: { name: value }.
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--course", help="probe a single course by name")
    args = ap.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    settings = config["settings"]
    tz = ZoneInfo(settings["timezone"])
    # probe 2 days out — near-term sheets are usually fullest
    day = (datetime.now(tz) + timedelta(days=2)).date()

    print(f"Probing tee sheets for {day} ({day.strftime('%A')})\n" + "=" * 60)
    any_fail = False

    for course in config["courses"]:
        if args.course and course["name"] != args.course:
            continue
        name, prov_name = course["name"], course["provider"]
        provider = PROVIDERS[prov_name](course, settings)
        print(f"\n▸ {name}  [{prov_name}]")

        if isinstance(provider, ChronogolfProvider):
            info = provider.discover()
            for uuid, cname, holes in info.get("courses", []):
                print(f"   · course uuid {uuid}: {cname} ({holes} holes)")
            if info.get("courses"):
                print("   → pin the right one in config.yaml as "
                      "chronogolf: { course_uuid: ... }")

        result = provider.fetch_day(day)
        if result.error:
            any_fail = True
            print(f"   ✗ {result.error}")
            if prov_name in ("cps", "generic_json"):
                print(DEVTOOLS_HELP)
        else:
            print(f"   ✓ {len(result.times)} raw tee times returned (pre-filter)")
            for t in result.times[:5]:
                price = f" ${t.price:.0f}" if t.price is not None else ""
                spots = f" ({t.open_spots} open)" if t.open_spots is not None else ""
                print(f"     {t.when.strftime('%-I:%M %p')}{price}{spots}")
            if len(result.times) > 5:
                print(f"     … and {len(result.times) - 5} more")

    print("\n" + "=" * 60)
    print("All good — run `python -m teetime_scout.main --dry-run` next."
          if not any_fail else
          "Some courses need attention (see above). Everything else will still "
          "work; failing courses show a 'check manually' link in the digest.")


if __name__ == "__main__":
    main()
