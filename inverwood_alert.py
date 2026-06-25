"""inverwood_alert.py — one-off weekend watcher.

Checks Inver Wood (TeeWire) for tee times on Sat Jun 27 2026 between 4:00 and
5:20 PM. If any are open, emails + texts (Verizon vtext gateway). Re-alerts
every run while slots remain open. Self-disables after Sat Jun 27 2:00 PM CT.

Standalone on purpose: does not import the scraper package, so nothing here can
affect the main site/digest. Reuses only the GMAIL_* secrets.
"""
from __future__ import annotations

import os
import smtplib
import sys
from datetime import datetime, time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("America/Chicago")
TARGET_DATE = "2026-06-27"          # Saturday
WINDOW_START = time(16, 0)          # 4:00 PM
WINDOW_END = time(17, 20)           # 5:20 PM (inclusive)
STOP_AFTER = datetime(2026, 6, 27, 14, 0, tzinfo=TZ)   # Sat 2:00 PM CT

SMS_TO = "6514700685@vtext.com"     # Verizon SMS gateway
BOOKING_URL = ("https://teewire.app/inverwood/index.php"
               "?controller=FrontV2&action=load&cid=3&view=list")
API_URL = ("https://teewire.app/inverwood/online/application/web/api/"
           "golf-api.php?action=tee-times&calendar_id=3&date={date}"
           "&starting_tee=1")


def in_window(t: time) -> bool:
    return WINDOW_START <= t <= WINDOW_END


def parse_time(raw, day_date):
    """Mirror the production generic_json provider's parsing exactly."""
    s = str(raw).strip()
    when = None
    try:
        when = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p",
                    "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(s, fmt)
                when = (datetime.combine(day_date, parsed.time())
                        if parsed.year == 1900 else parsed)
                break
            except ValueError:
                continue
    if when is None:
        return None
    when = (when.replace(tzinfo=TZ) if when.tzinfo is None
            else when.astimezone(TZ))
    return when


def fetch_open_slots():
    """Return list of (display_time, spots) for open slots in the window."""
    url = API_URL.format(date=TARGET_DATE)
    resp = requests.get(url, timeout=25, headers={"Referer": BOOKING_URL})
    resp.raise_for_status()
    data = resp.json()
    slots = data.get("data", {}).get("tee_times", []) or []
    target = datetime.strptime(TARGET_DATE, "%Y-%m-%d").date()
    out = []
    for s in slots:
        raw = s.get("time")
        if not raw:
            continue
        when = parse_time(raw, target)
        if when is None or when.date() != target or not in_window(when.time()):
            continue
        avail = s.get("availability", {}) or {}
        spots = avail.get("available_spots")
        if spots is not None and spots <= 0:
            continue
        out.append((when.strftime("%-I:%M %p"), spots))
    return out


def send(subject: str, body_text: str):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    inbox = os.environ.get("TO_EMAIL", sender)
    recipients = [inbox, SMS_TO]

    # Plain text keeps the SMS gateway happy (it strips HTML anyway).
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(recipients)
    msg.attach(MIMEText(body_text, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.sendmail(sender, recipients, msg.as_string())
    print(f"alert sent to {recipients}")


def main():
    now = datetime.now(TZ)
    if now > STOP_AFTER:
        print(f"past stop time ({STOP_AFTER}); exiting without checking.")
        return

    try:
        slots = fetch_open_slots()
    except Exception as e:  # noqa: BLE001
        print(f"fetch failed: {e}", file=sys.stderr)
        # don't alert on errors; just exit non-fatally so the run goes green
        return

    if not slots:
        print(f"{now:%H:%M} — no open slots in 4:00–5:20 PM window.")
        return

    lines = [f"  • {t}" + (f" ({sp} spots)" if sp is not None else "")
             for t, sp in slots]
    body = ("Inver Wood — Sat Jun 27, 4:00–5:20 PM\n\n"
            + "\n".join(lines)
            + f"\n\nBook: {BOOKING_URL}")
    subject = f"⛳ Inver Wood OPEN: {len(slots)} slot(s) 4–5:20 PM Sat"
    send(subject, body)


if __name__ == "__main__":
    main()
