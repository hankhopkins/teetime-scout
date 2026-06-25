"""inverwood_alert.py — one-off weekend watcher.

Checks Inver Wood (TeeWire) for tee times on Sat Jun 27 2026 between 4:00 and
5:20 PM. If any are open, emails + texts (Verizon vtext gateway). Re-alerts
every run while slots remain open. Self-disables after Sat Jun 27 2:00 PM CT.

Standalone on purpose: does not import the scraper package, so nothing here can
affect the main site/digest. Reuses only the GMAIL_* secrets.
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import datetime, time, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("America/Chicago")
TARGET_DATE = "2026-06-27"          # Saturday
WINDOW_START = time(16, 0)          # 4:00 PM
WINDOW_END = time(17, 20)           # 5:20 PM (inclusive)
STOP_AFTER = datetime(2026, 6, 27, 14, 0, tzinfo=TZ)   # Sat 2:00 PM CT
REALERT_AFTER = timedelta(minutes=30)   # re-alert an open slot at most this often
STATE_FILE = Path("inverwood_alert_state.json")

SMS_TO = ["6514700685@vtext.com"]   # Verizon SMS gateway (works)
# 612-232-9336 is AT&T — both SMS (txt.att.net) and MMS (mms.att.net) gateways
# are decommissioned (DNS no longer resolves), so that person gets email only.
EXTRA_EMAILS = ["newman.nick3@gmail.com"]
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
        # Require at least 2 open spots. If the count is unknown, exclude it —
        # we can't confirm 2 are available, so we don't alert on it.
        if spots is None or spots < 2:
            continue
        out.append((when.strftime("%-I:%M %p"), spots))
    return out


def send(subject: str, body_text: str):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    inbox = os.environ.get("TO_EMAIL", sender)
    recipients = [inbox, *EXTRA_EMAILS, *SMS_TO]

    # Plain text keeps the SMS gateway happy (it strips HTML anyway).
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(recipients)
    msg.attach(MIMEText(body_text, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, password)
        smtp.sendmail(sender, recipients, msg.as_string())
    print(f"alert sent to {recipients}")


def load_state() -> dict:
    """State maps slot-time string -> ISO timestamp of our last alert for it."""
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001  (missing/corrupt -> start fresh)
        return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=1))


def main():
    if "--test" in sys.argv or os.environ.get("ALERT_TEST") == "1":
        send("⛳ Inver Wood alert TEST",
             "Test message — alerts are wired up. "
             "If you got this as a text, the vtext gateway works. "
             "Real alerts fire for Sat 4:00–5:20 PM openings.")
        return

    if datetime.now(TZ) > STOP_AFTER:
        print(f"past stop time ({STOP_AFTER}); exiting without checking.")
        return

    try:
        slots = fetch_open_slots()
    except Exception as e:  # noqa: BLE001
        print(f"fetch failed: {e}", file=sys.stderr)
        # don't alert (or mutate state) on errors; exit green
        return

    now = datetime.now(TZ)
    current = {t: sp for t, sp in slots}          # time -> spots, qualifying now
    state = load_state()                          # time -> last-alert ISO
    new_state: dict = {}

    newly_open = []     # (time, spots) — first time we've seen it qualify
    re_alert = []       # (time, spots) — open >= 30 min since last alert
    for t, sp in current.items():
        last = state.get(t)
        if last is None:
            newly_open.append((t, sp))
            new_state[t] = now.isoformat()
        else:
            try:
                last_dt = datetime.fromisoformat(last)
            except ValueError:
                last_dt = now - REALERT_AFTER     # treat bad data as due
            if now - last_dt >= REALERT_AFTER:
                re_alert.append((t, sp))
                new_state[t] = now.isoformat()
            else:
                new_state[t] = last               # keep the original alert time

    # Gone: was in state last run, not qualifying now (missing or < 2 spots).
    gone = [t for t in state.keys() if t not in current]

    def fmt(rows):
        return "\n".join(f"  • {t} ({sp} spots)" for t, sp in sorted(rows))

    sent_any = False
    if newly_open or re_alert:
        opened = newly_open + re_alert
        body = ("Inver Wood — Sat Jun 27, 4:00–5:20 PM\n\n"
                + fmt(opened)
                + f"\n\nBook: {BOOKING_URL}")
        tag = "OPEN" if newly_open else "still open"
        subject = f"⛳ Inver Wood {tag}: {len(opened)} slot(s) 4–5:20 PM Sat"
        send(subject, body)
        sent_any = True

    if gone:
        body = ("These Inver Wood slots are no longer open (Sat 4:00–5:20 PM):\n\n"
                + "\n".join(f"  • {t}" for t in sorted(gone))
                + f"\n\nBook: {BOOKING_URL}")
        send(f"⛳ Inver Wood GONE: {len(gone)} slot(s) closed", body)
        sent_any = True

    save_state(new_state)
    print(f"{now:%H:%M} — qualifying={list(current)} "
          f"new={[t for t,_ in newly_open]} re={[t for t,_ in re_alert]} "
          f"gone={gone} sent={sent_any}")


if __name__ == "__main__":
    main()
