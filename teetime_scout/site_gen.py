"""site_gen.py — turn fetch results into docs/data.json for the static site.

The site itself (docs/index.html) is a committed static file; only data.json
changes run-to-run. GitHub Pages serves the docs/ folder.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def build_site_data(config: dict, results: dict) -> dict:
    tz = ZoneInfo(config["settings"]["timezone"])
    now = datetime.now(tz)

    courses = []
    for course in config["courses"]:           # keep config (importance) order
        name = course["name"]
        course_results = results.get(name, [])
        errors = sorted({r.error for r in course_results if r.error})
        times = []
        for r in course_results:
            for t in r.times:
                times.append({
                    "when": t.when.isoformat(),
                    "spots": t.open_spots,
                    "price": t.price,
                    "holes": t.holes,
                })
        times.sort(key=lambda x: x["when"])
        courses.append({
            "name": name,
            "booking_url": course.get("booking_url"),
            "link_only": bool(course.get("link_only")),
            "note": course.get("note"),
            "errors": errors,
            "times": times,
        })

    return {
        "generated_at": now.isoformat(),
        "generated_at_display": now.strftime("%A, %B %-d · %-I:%M %p %Z"),
        "total_times": sum(len(c["times"]) for c in courses),
        "courses": courses,
    }


def write_site_data(config: dict, results: dict, out_dir: str = "docs") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "data.json"
    path.write_text(json.dumps(build_site_data(config, results),
                               indent=1, ensure_ascii=False))
    return path
