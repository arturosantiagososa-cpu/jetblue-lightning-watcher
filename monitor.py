"""
JetBlue Park lightning watcher.
Renders the Earth Networks siren widget in a headless browser, checks it every
minute Mon/Wed 3:00-6:30 PM Eastern, and pushes a phone notification (ntfy)
whenever the alarm turns ON or goes back to ALL CLEAR.

MODE=test  -> check once right now and send the result (use this to verify).
MODE=watch -> normal scheduled run.
"""
import os, re, time, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

URL = "https://oas.earthnetworks.com/widget/ResOASWidget.html?widgetId=2d51b0d2-571c-4038-b4b6-b3dc1ec5161d"
TZ = ZoneInfo("America/New_York")
DAYS = (0, 2)            # Monday=0, Wednesday=2
START = (15, 0)          # 3:00 PM
END = (18, 30)           # 6:30 PM
INTERVAL = 60            # seconds between checks

TOPIC = os.environ["NTFY_TOPIC"]
MODE = os.environ.get("MODE", "watch")

def notify(title, msg, priority="default", tags=""):
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}", data=msg.encode("utf-8"), method="POST",
        headers={"Title": title, "Priority": priority, "Tags": tags})
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        print("notify failed:", e)


def read_status(page):
    """Reads the widget's own fields (IDs confirmed on the live page):
    #SiteStatus = 'No Alert' when clear, #TimerHour/Minutes/Seconds = countdown,
    #LtgStokeDistance = last strike distance, #ConnectionStatus = 'Up'/'Down'."""
    page.reload(wait_until="networkidle", timeout=45000)
    page.wait_for_selector("#SiteStatus", timeout=30000)
    page.wait_for_timeout(4000)  # let the widget's script fill in values

    def val(sel):
        try:
            return " ".join(page.inner_text(sel).split())
        except Exception:
            return ""

    site = val("#SiteStatus")
    conn = val("#ConnectionStatus")
    h, mi, se = val("#TimerHour"), val("#TimerMinutes"), val("#TimerSeconds")
    dist = val("#LtgStokeDistance")
    text = f"Status: {site or '?'} | Connection: {conn or '?'} | Timer: {h}:{mi}:{se} | Last strike: {dist}"

    remaining = None
    if h.isdigit() and mi.isdigit() and se.isdigit():
        remaining = int(h) * 3600 + int(mi) * 60 + int(se)

    low = site.lower()
    if not low or low in ("--", "-"):
        status = "UNKNOWN"
    elif low.startswith("no ") or "all clear" in low or low == "clear":
        status = "CLEAR"
    else:
        status = "ACTIVE"   # anything other than 'No Alert' (e.g. Alert / Warning / Hold)
    if status == "CLEAR" and remaining:
        status = "ACTIVE"   # countdown running means the hold is still on

    distance = dist if dist and dist not in ("--", "-") else None
    return status, text, remaining, distance


def countdown_msg(remaining, distance):
    """e.g. 'All clear in 27m 14s (about 4:42 PM). Last strike 6.2 mi away.'"""
    if not remaining:
        msg = "Countdown not showing yet."
    else:
        eta = datetime.now(TZ) + timedelta(seconds=remaining)
        m, s = divmod(remaining, 60)
        h, m = divmod(m, 60)
        left = (f"{h}h " if h else "") + f"{m}m {s:02d}s"
        msg = f"All clear in {left} (about {eta.strftime('%-I:%M %p')})."
    if distance:
        msg += f" Last strike {distance} away."
    return msg


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)

        if MODE == "test":
            status, text, remaining, distance = read_status(page)
            page.screenshot(path="widget.png", full_page=True)
            notify(f"TEST - JetBlue lightning: {status}",
                   countdown_msg(remaining, distance) + " | Widget text: " + text[:350],
                   "high", "test_tube")
            print(status, "|", text)
            return

        now = datetime.now(TZ)
        if now.weekday() not in DAYS:
            print("Not a practice day, exiting.")
            return
        start = now.replace(hour=START[0], minute=START[1], second=0, microsecond=0)
        end = now.replace(hour=END[0], minute=END[1], second=0, microsecond=0)
        if now < start:
            wait = (start - now).total_seconds()
            print(f"Waiting {wait/60:.0f} min until 3:00 PM")
            time.sleep(wait)

        last, unknown_streak, first, last_eta = None, 0, True, None
        while datetime.now(TZ) < end:
            try:
                status, text, remaining, distance = read_status(page)
            except Exception as e:
                status, text, remaining, distance = "UNKNOWN", f"error: {e}", None, None
                try:
                    page.goto(URL, wait_until="networkidle", timeout=60000)
                except Exception:
                    pass
            print(datetime.now(TZ).strftime("%H:%M:%S"), status)

            if status == "UNKNOWN":
                unknown_streak += 1
                if unknown_streak == 5:  # ~5 min of unreadable status
                    notify("Lightning watcher: can't read status",
                           "Check the website manually. " + text[:250], "high", "warning")
            else:
                unknown_streak = 0
                if first:
                    if status == "ACTIVE":
                        notify("LIGHTNING ALARM ACTIVE at JetBlue Park",
                               "Alarm is on as of watcher start. " + countdown_msg(remaining, distance),
                               "urgent", "zap,rotating_light")
                    else:
                        notify("Lightning watcher on", "JetBlue Park is ALL CLEAR right now.",
                               "low", "white_check_mark")
                    first = False
                elif status != last:
                    page.screenshot(path="widget.png", full_page=True)
                    if status == "ACTIVE":
                        notify("LIGHTNING ALARM ACTIVE at JetBlue Park",
                               "Field is on lightning hold. " + countdown_msg(remaining, distance)
                               + " | " + text,
                               "urgent", "zap,rotating_light")
                    else:
                        notify("ALL CLEAR at JetBlue Park",
                               "Lightning alarm has reset.", "high", "white_check_mark")
                elif status == "ACTIVE" and remaining:
                    # New strike resets the timer: tell him the new all-clear time
                    eta = datetime.now(TZ) + timedelta(seconds=remaining)
                    if last_eta and eta - last_eta > timedelta(minutes=2):
                        notify("Lightning timer RESET - new strike",
                               countdown_msg(remaining, distance), "high", "zap")
                if status == "ACTIVE" and remaining:
                    last_eta = datetime.now(TZ) + timedelta(seconds=remaining)
                elif status == "CLEAR":
                    last_eta = None
                last = status
            time.sleep(INTERVAL)

        browser.close()


if __name__ == "__main__":
    main()
