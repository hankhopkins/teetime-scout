"""Tee Time Scout — fetch tee sheets, filter by your rules, email a digest.

Usage:
  python -m teetime_scout.main              # fetch + send email
  python -m teetime_scout.main --dry-run    # fetch + print digest, no email
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import smtplib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import yaml

from .providers.base import FetchResult, log
from .providers.chronogolf import ChronogolfProvider
from .providers.cps import CPSProvider
from .providers.foreup import ForeUpProvider
from .providers.generic_json import GenericJSONProvider
from .providers.teeitup import TeeItUpProvider
from .providers.prophet_v3 import ProphetV3Provider
from .providers.webtrac import WebTracProvider
from .providers.teesnap import TeesnapProvider
from .providers.clubcaddie import ClubCaddieProvider

PROVIDERS = {
    "chronogolf": ChronogolfProvider,
    "cps": CPSProvider,
    "foreup": ForeUpProvider,
    "teeitup": TeeItUpProvider,
    "generic_json": GenericJSONProvider,
    "prophet_v3": ProphetV3Provider,
    "webtrac": WebTracProvider,
    "teesnap": TeesnapProvider,
    "clubcaddie": ClubCaddieProvider,
}

DAY_ALIASES = {
    "weekdays": ["mon", "tue", "wed", "thu", "fri"],
    "weekend": ["sat", "sun"],
}
DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ── rules ────────────────────────────────────────────────────────────────────
def expand_days(days: list[str]) -> set[int]:
    out: set[int] = set()
    for d in days:
        for dd in DAY_ALIASES.get(d.lower(), [d.lower()]):
            out.add(DAY_INDEX[dd])
    return out


def parse_window(window: str) -> tuple[time, time]:
    start, end = window.split("-")
    return (time(*map(int, start.strip().split(":"))),
            time(*map(int, end.strip().split(":"))))


def matches_rules(when: datetime, rules: list[dict]) -> bool:
    if not rules:          # no rules configured = no time restrictions
        return True
    for rule in rules:
        if when.weekday() not in expand_days(rule["days"]):
            continue
        start, end = parse_window(rule["window"])
        if start <= when.time() <= end:
            return True
    return False


def days_wanted(rules: list[dict]) -> set[int]:
    if not rules:          # no rules configured = every day
        return set(range(7))
    out: set[int] = set()
    for rule in rules:
        out |= expand_days(rule["days"])
    return out


# ── fetching ─────────────────────────────────────────────────────────────────
def course_dates(course_cfg: dict, settings: dict, today: date) -> list[date]:
    """Dates to check for this course, honoring its real-world booking window.

    booking_window may be an int (days bookable in advance) or a dict like
    {weekdays: 7, weekends: 4} (e.g. Keller). Defaults to settings.days_ahead.
    """
    window = course_cfg.get("booking_window", settings.get("days_ahead", 7))
    out = []
    max_span = window if isinstance(window, int) else max(window.values())
    for i in range(max_span + 1):
        day = today + timedelta(days=i)
        if isinstance(window, dict):
            limit = window["weekends" if day.weekday() >= 5 else "weekdays"]
        else:
            limit = window
        if i <= limit:
            out.append(day)
    return out


def fetch_course(course_cfg: dict, settings: dict, today: date) -> list[FetchResult]:
    if course_cfg.get("link_only"):
        return []          # no scraping; the site/email just shows the booking link
    provider = PROVIDERS[course_cfg["provider"]](course_cfg, settings)
    rules = course_cfg.get("rules") or []
    wanted = days_wanted(rules)
    results = []
    for day in course_dates(course_cfg, settings, today):
        if day.weekday() not in wanted:
            continue
        res = provider.fetch_day(day)
        min_spots = settings.get("min_open_spots", 1)
        res.times = [
            t for t in res.times
            if matches_rules(t.when, rules)
            and (t.open_spots is None or t.open_spots >= min_spots)
        ]
        res.times.sort(key=lambda t: t.when)
        results.append(res)
    return results


def run_all(config: dict) -> dict[str, list[FetchResult]]:
    settings = config["settings"]
    tz = ZoneInfo(settings["timezone"])
    today = datetime.now(tz).date()

    out: dict[str, list[FetchResult]] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_course, c, settings, today): c["name"]
                   for c in config["courses"]}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                out[name] = fut.result()
            except Exception as e:  # noqa: BLE001
                log.exception("course %s failed", name)
                out[name] = [FetchResult(name, today, [], f"Unexpected failure: {e}")]
    return out


# ── email ────────────────────────────────────────────────────────────────────
GREEN, GOLD, CREAM = "#1b4332", "#c9a227", "#faf7ef"


def build_html(config: dict, results: dict[str, list[FetchResult]]) -> tuple[str, str, int]:
    tz = ZoneInfo(config["settings"]["timezone"])
    now = datetime.now(tz)
    total = sum(len(r.times) for rs in results.values() for r in rs)

    rows = [f"""
    <div style="background:{GREEN};color:{CREAM};padding:22px 26px;border-radius:10px 10px 0 0;">
      <div style="font-size:22px;font-weight:700;letter-spacing:.5px;">⛳ Tee Time Scout</div>
      <div style="font-size:13px;color:{GOLD};margin-top:4px;">
        {now.strftime('%A, %B %-d · %-I:%M %p %Z')} &nbsp;·&nbsp; {total} matching tee time{'s' if total != 1 else ''}
        across each course's full booking window
      </div>
    </div>"""]

    for course in config["courses"]:  # keep importance order
        name = course["name"]
        course_results = results.get(name, [])
        booking = course.get("booking_url", "#")
        n = sum(len(r.times) for r in course_results)
        errors = [r for r in course_results if r.error]

        rows.append(f"""
        <div style="padding:16px 26px;border-bottom:1px solid #e4ddcc;">
          <div style="font-size:16px;font-weight:700;color:{GREEN};">
            <a href="{html.escape(booking)}" style="color:{GREEN};text-decoration:none;">{html.escape(name)}</a>
            <span style="font-weight:400;color:#8a8472;font-size:13px;">&nbsp;{n} time{'s' if n != 1 else ''}</span>
          </div>""")

        if errors:
            msg = html.escape(errors[0].error or "")
            rows.append(f"""<div style="font-size:12px;color:#a33;margin-top:4px;">
              ⚠ {msg} — <a href="{html.escape(booking)}">check manually</a></div>""")

        by_day: dict[date, list] = {}
        for r in course_results:
            for t in r.times:
                by_day.setdefault(t.when.date(), []).append(t)

        for day in sorted(by_day):
            chips = []
            for t in sorted(by_day[day], key=lambda x: x.when):
                bits = [t.when.strftime("%-I:%M%p").lower()]
                if t.price is not None:
                    bits.append(f"${t.price:.0f}")
                if t.open_spots is not None:
                    bits.append(f"{t.open_spots} open")
                chips.append(
                    f"""<span style="display:inline-block;background:{CREAM};
                    border:1px solid #d8cfa9;border-radius:6px;padding:3px 8px;
                    margin:2px 3px 2px 0;font-size:12.5px;color:#333;">{' · '.join(bits)}</span>""")
            rows.append(f"""
            <div style="margin-top:8px;">
              <div style="font-size:13px;font-weight:600;color:#555;">{day.strftime('%a %b %-d')}</div>
              <div>{''.join(chips)}</div>
            </div>""")

        if not n and not errors:
            rows.append("""<div style="font-size:12.5px;color:#999;margin-top:4px;">
              No openings in your windows.</div>""")
        rows.append("</div>")

    rows.append(f"""
    <div style="padding:14px 26px;font-size:11px;color:#999;">
      Generated by Tee Time Scout · windows &amp; courses configured in config.yaml
    </div>""")

    body = f"""<div style="max-width:680px;margin:0 auto;font-family:Georgia,'Times New Roman',serif;
      background:#fff;border:1px solid #e4ddcc;border-radius:10px;">{''.join(rows)}</div>"""
    subject = f"{config['email'].get('subject_prefix', 'Tee Time Scout')}: {total} openings · {now.strftime('%a %-m/%-d %-I%p').lower()}"
    return subject, body, total


def send_email(subject: str, body_html: str):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("TO_EMAIL", sender)

    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.attach(MIMEText(body_html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.sendmail(sender, [to], msg.as_string())
    log.info("email sent to %s", to)


# ── entry point ──────────────────────────────────────────────────────────────
def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="print digest, don't email")
    ap.add_argument("--site", action="store_true",
                    help="write docs/data.json for the GitHub Pages site (no email)")
    ap.add_argument("--email", action="store_true",
                    help="also send the email digest when used with --site")
    args = ap.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    results = run_all(config)

    if args.site:
        from .site_gen import write_site_data
        path = write_site_data(config, results)
        total = sum(len(r.times) for rs in results.values() for r in rs)
        log.info("wrote %s (%d tee times)", path, total)
        if args.email:
            subject, body, _ = build_html(config, results)
            send_email(subject, body)
        return

    subject, body, total = build_html(config, results)

    if args.dry_run:
        print(f"SUBJECT: {subject}\n")
        for course in config["courses"]:
            name = course["name"]
            for r in results.get(name, []):
                if r.error:
                    print(f"  ⚠ {name} {r.day}: {r.error}")
                for t in r.times:
                    price = f" ${t.price:.0f}" if t.price is not None else ""
                    spots = f" ({t.open_spots} open)" if t.open_spots is not None else ""
                    print(f"  {name}: {t.when.strftime('%a %m/%d %-I:%M%p')}{price}{spots}")
        print(f"\n{total} total matching tee times")
        return

    send_email(subject, body)


if __name__ == "__main__":
    sys.exit(main())
