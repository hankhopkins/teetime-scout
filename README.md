# ⛳ Tee Time Scout

Twice-daily email digest of open tee times at your Twin Cities courses,
filtered to the days and time windows you actually want to play. Runs free on
GitHub Actions — same pattern as the SAGL GHIN handicap puller.

## How it works

1. A GitHub Actions cron fires at ~10:00 AM and ~10:00 PM Central.
2. `teetime_scout/main.py` queries each course's booking platform directly
   (Chronogolf, ForeUp, TeeItUp, CPS Golf), looking `days_ahead` days out.
3. Tee times are filtered against per-course rules in `config.yaml`
   (e.g. Braemar M–Th 11am–7pm, Fri–Sun 9am–6:30pm).
4. A styled HTML digest lands in your inbox, grouped by course (in your
   priority order) and date, with price, open spots, and a one-tap booking link.

## Setup (~10 minutes)

### 1. Repo
Push this folder to a new private GitHub repository.

### 2. Gmail app password
Google account → Security → 2-Step Verification → App passwords → create one
named "teetime-scout". (Regular passwords won't work with SMTP.)

### 3. GitHub secrets
Repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `GMAIL_ADDRESS` | the Gmail account that sends the digest |
| `GMAIL_APP_PASSWORD` | the 16-char app password |
| `TO_EMAIL` | where the digest goes (can equal GMAIL_ADDRESS) |

### 4. Probe (do this locally first)
```bash
pip install -r requirements.txt
python probe.py
```
The probe hits every course for a date two days out and reports per course:
✓ working (with sample times), or ✗ with exactly what to fix. For Chronogolf
courses it also prints the discovered `club_id` / `course_id` /
`affiliation_type_id` — paste those into `config.yaml` so production runs skip
discovery. For Brookview, pick the **Regulation** course id from the printed
list (the club also has a Par 3).

### 5. Dry run, then ship
```bash
python -m teetime_scout.main --dry-run   # prints the digest to terminal
git push                                  # cron takes it from here
```
Use the **Run workflow** button (Actions tab) to trigger a real email on demand.

## Tuning your windows

Edit `config.yaml` and push. Each course takes any number of rules:

```yaml
rules:
  - { days: [mon, tue, wed, thu], window: "11:00-19:00" }
  - { days: [fri, sat, sun],      window: "09:00-18:30" }
```
`weekdays` and `weekend` work as shortcuts. A course with no rule for a day is
simply never checked that day (Brookview/Oak Marsh/Victory Links have no
weekend rules, per your spec). Other knobs in `settings`: `days_ahead`,
`min_open_spots`, `holes`.

### Booking windows
Each course also carries a `booking_window` — how far in advance that course
actually releases tee times — so the scout searches the full real horizon per
course instead of one global number:

```yaml
booking_window: 10                              # Chaska: 10 days online
booking_window: { weekdays: 7, weekends: 4 }    # Keller: county splits these
```
Verified from official course policies where published: Victory Links 14,
Chaska 10, Braemar 8 (online), Inver Wood / Oak Marsh / Baker National 7,
Keller 7/4, Brookview 5, Chomonix 10 (15/21 for members). The MPRB courses, Edinburgh, and Highland National
don't publish a public window, so they default to 7 (marked UNVERIFIED in the
config) — if the probe shows times appearing further out, raise them. The
practical payoff of the Keller split: Saturday times open exactly 4 days out,
so the Tuesday 10 PM digest is your first look at the weekend there.

## Platform notes

| Platform | Courses | Status |
|---|---|---|
| Chronogolf | Meadowbrook, Columbia, Gross, Baker National, Chaska Town Course, Brookview, Oak Marsh | Auto-discovery built in; pin ids after probe |
| ForeUp | Braemar (21445/7829) | Booking class auto-discovered |
| TeeItUp | Keller (ramsey-county-golf / 17055) | Should work out of the box |
| CPS Golf | Highland National, Edinburgh USA, Victory Links | Tries known endpoint shapes; may need a one-time devtools capture |
| TeeWire | Inver Wood | New platform (2026) — needs a one-time devtools capture |
| WebTrac | Chomonix | HTML scrape of Anoka County's parks system; verify via probe |

### TeeWire (Inver Wood) — one-time capture
Inver Wood switched to teewire.app this season, so its API isn't pre-wired:
1. Open https://teewire.app/inverwood/ in Chrome → F12 → Network → Fetch/XHR.
2. Pick a date; find the request whose JSON response contains the tee times.
3. Copy its URL, swap the date for `{date}`, and fill in the `generic:` block
   for Inver Wood in `config.yaml` (url_template + field names from the JSON).
Same procedure applies to any CPS course the probe flags.

### Failure behavior
A course whose fetch fails never blocks the digest — it shows a ⚠ line with
the error and a "check manually" booking link. Booking platforms change;
when one breaks, re-run `probe.py` to diagnose.

### Cron + DST
GitHub cron is UTC: `15:00`/`03:00` UTC = 10am/10pm CDT. GitHub also queues
scheduled jobs, so expect "10-ish" (up to ~15 min drift). In CST months the
times shift to 9am/9pm; bump the crons an hour if that bothers you in January.
