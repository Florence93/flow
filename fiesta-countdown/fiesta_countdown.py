"""
Alimosho Fitness Fiesta countdown emails.

Each run (daily, via GitHub Actions):
  1. Pulls current attendees from Eventbrite and adds them to a Sender group.
  2. Works out how many days are left (Lagos time).
  3. If today is a countdown day, creates the campaign in Sender and sends it
     to the group. Skips if a campaign with the same title already exists.
"""
import os
import sys
import time
import datetime as dt
from zoneinfo import ZoneInfo

import requests

# ---------- config ----------
EVENT_DATE = dt.date.fromisoformat(os.getenv("EVENT_DATE", "2026-12-24"))
SEND_DAYS = {int(x) for x in os.getenv("SEND_DAYS", "30,27,20,13,6,1,0").split(",") if x.strip()}
TZ = ZoneInfo("Africa/Lagos")
REG_URL = "https://alimosho-fitness-fiesta.eventbrite.com"
HERO_URL = os.getenv("HERO_IMAGE_URL", "")  # public URL of assets/flyer_hero.jpg

EB_TOKEN = os.environ["EVENTBRITE_TOKEN"]
EB_EVENT_ID = os.environ["EVENTBRITE_EVENT_ID"]
SENDER_TOKEN = os.environ["SENDER_API_TOKEN"]
SENDER_GROUP_ID = os.environ["SENDER_GROUP_ID"]
FROM_NAME = os.getenv("SENDER_FROM_NAME", "Alimosho Fitness Fiesta")
FROM_EMAIL = os.environ["SENDER_FROM_EMAIL"]  # must be a verified sender in Sender

FORCE_DAY = os.getenv("FORCE_DAY", "").strip()   # test a specific milestone
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

SENDER = "https://api.sender.net/v2"
SENDER_HEADERS = {
    "Authorization": f"Bearer {SENDER_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


# ---------- Eventbrite ----------
def get_attendees():
    url = f"https://www.eventbriteapi.com/v3/events/{EB_EVENT_ID}/attendees/"
    headers = {"Authorization": f"Bearer {EB_TOKEN}"}
    params = {"status": "attending"}
    people = []
    while True:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for a in data.get("attendees", []):
            if a.get("cancelled") or a.get("refunded"):
                continue
            p = a.get("profile", {})
            email = (p.get("email") or "").strip().lower()
            if email:
                people.append({
                    "email": email,
                    "firstname": (p.get("first_name") or "").strip(),
                    "lastname": (p.get("last_name") or "").strip(),
                })
        pg = data.get("pagination", {})
        if pg.get("has_more_items"):
            params["continuation"] = pg["continuation"]
        else:
            break
    # de-dupe by email (one buyer can register several tickets)
    return list({p["email"]: p for p in people}.values())


# ---------- Sender ----------
def sync_subscribers(people):
    added = 0
    for p in people:
        body = {**p, "groups": [SENDER_GROUP_ID]}
        r = requests.post(f"{SENDER}/subscribers", headers=SENDER_HEADERS, json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(10)
            r = requests.post(f"{SENDER}/subscribers", headers=SENDER_HEADERS, json=body, timeout=30)
        if r.ok:
            added += 1
        else:
            print(f"  warn: {p['email']} -> {r.status_code} {r.text[:150]}")
        time.sleep(0.15)
    print(f"Synced {added}/{len(people)} attendees to Sender group {SENDER_GROUP_ID}")


def campaign_exists(title):
    for page in range(1, 6):
        r = requests.get(f"{SENDER}/campaigns", headers=SENDER_HEADERS,
                         params={"page": page}, timeout=30)
        r.raise_for_status()
        rows = r.json().get("data", [])
        if not rows:
            return False
        if any(c.get("title") == title for c in rows):
            return True
    return False


def create_and_send(title, subject, html):
    body = {
        "title": title,
        "subject": subject,
        "from": FROM_NAME,
        "reply_to": FROM_EMAIL,
        "content_type": "html",
        "groups": [SENDER_GROUP_ID],
        "content": html,
    }
    r = requests.post(f"{SENDER}/campaigns", headers=SENDER_HEADERS, json=body, timeout=30)
    r.raise_for_status()
    cid = r.json()["data"]["id"]
    s = requests.post(f"{SENDER}/campaigns/{cid}/send", headers=SENDER_HEADERS, timeout=30)
    s.raise_for_status()
    print(f"Sent campaign {cid}: {subject}")


# ---------- email content ----------
# days_left -> (subject, headline, paragraphs)
# 30 = Nov 24, 27/20/13/6 = the four Fridays before the event, 1 = Dec 23, 0 = Dec 24 (D-Day)
EMAILS = {
    30: ("30 days to Alimosho Fitness Fiesta",
         "The countdown has begun",
         ["You are registered for Alimosho Fitness Fiesta, and our 30 day countdown starts today.",
          "Over the next few weeks we will share what to expect, how to prepare, and how to get the "
          "most out of the morning.",
          "Move. Learn. Connect. Grow."]),
    27: ("27 days to go: here is what is waiting for you",
         "What is waiting for you",
         ["Workout sessions, free health check-ups, health education, community engagement, games and "
          "giveaways, plus food, drinks, music and a DJ.",
          "Save the date: Thursday, December 24, 7:00 AM."]),
    20: ("20 days to go: bring a friend to the Fiesta",
         "Bring someone with you",
         ["Three weeks to go. Start getting your body ready with a few short walks or light workouts "
          "this week.",
          "Know someone who would enjoy a free morning of fitness and community? Send them the "
          "registration link below so they can join you."]),
    13: ("13 days to go: what to expect on the day",
         "Almost two weeks to go",
         ["Our free health check-ups and health education sessions are a great chance to learn more "
          "about your health in a relaxed setting.",
          "Plan your morning now: the event starts at 7:00 AM, so decide how you will get to Egbeda "
          "Akowonjo and who is coming with you."]),
    6: ("6 days to go: your Fiesta checklist",
        "Your checklist for Thursday",
        ["Comfortable workout clothes and shoes. A water bottle. Your registration confirmation from "
         "Eventbrite. A friend or two.",
         "Arrive early so you do not miss the opening workout."]),
    1: ("24 hours to go: Alimosho Fitness Fiesta",
        "24 hours to go",
        ["It is almost here. Tomorrow morning, 7:00 AM, Alimosho LGA, Egbeda Akowonjo.",
         "Set your alarm, lay out your workout clothes tonight, and bring your Eventbrite confirmation. "
         "Healthy living is a lifestyle, not a destination."]),
    0: ("Today is the day! See you at 7:00 AM",
        "It is D-Day!",
        ["Good morning! Alimosho Fitness Fiesta starts at 7:00 AM today at Alimosho LGA, Egbeda Akowonjo.",
         "Wear your workout clothes, bring water and your Eventbrite confirmation, and come ready to "
         "move, learn, connect and grow. See you there!"]),
}


GREEN = "#0b7a2b"
DARK = "#0a4d1f"
BLUE = "#1a3a9e"
YELLOW = "#ffe500"
FONT = "Arial Black, Arial, Helvetica, sans-serif"


def render(days_left, headline, paras):
    if days_left == 0:
        big, small = "TODAY", "IT IS D-DAY"
    elif days_left == 1:
        big, small = "24", "HOURS TO GO"
    else:
        big, small = str(days_left), "DAYS TO GO"

    hero = (f'<img src="{HERO_URL}" width="600" alt="Alimosho Fitness Fiesta" '
            f'style="display:block;width:100%;max-width:600px;height:auto;border:0;">') if HERO_URL else ""

    body = "".join(
        f'<p style="font-family:Arial,Helvetica,sans-serif;font-size:16px;line-height:1.6;'
        f'color:#1a2a1a;margin:0 0 14px;">{p}</p>' for p in paras)

    def feat(icon, label):
        return (f'<td align="center" valign="top" width="20%" style="padding:6px 3px;">'
                f'<div style="font-size:26px;line-height:30px;">{icon}</div>'
                f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:bold;'
                f'color:{BLUE};text-transform:uppercase;line-height:1.3;margin-top:4px;">{label}</div></td>')

    def info(icon, text):
        return (f'<td align="center" valign="middle" style="padding:12px 6px;">'
                f'<div style="font-size:22px;line-height:26px;">{icon}</div>'
                f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:bold;'
                f'color:{DARK};text-transform:uppercase;line-height:1.3;margin-top:3px;">{text}</div></td>')

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#dff0e3;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#dff0e3;"><tr><td align="center" style="padding:16px 8px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;">

  <tr><td style="line-height:0;font-size:0;">{hero}</td></tr>

  <tr><td align="center" style="background:{DARK};padding:18px 16px;">
    <div style="font-family:{FONT};font-size:46px;line-height:48px;font-weight:900;font-style:italic;color:{YELLOW};">{big}</div>
    <div style="font-family:{FONT};font-size:16px;letter-spacing:3px;font-style:italic;color:#ffffff;margin-top:4px;">{small}</div>
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#bfe3c8;margin-top:6px;">COUNTDOWN TO 24.12.2026</div>
  </td></tr>

  <tr><td style="padding:26px 26px 6px;">
    <div style="font-family:{FONT};font-size:26px;line-height:30px;font-style:italic;font-weight:900;color:{BLUE};text-transform:uppercase;margin:0 0 16px;">{headline}</div>
    <p style="font-family:Arial,Helvetica,sans-serif;font-size:16px;color:#1a2a1a;margin:0 0 14px;">Hi {{$firstname}},</p>
    {body}
  </td></tr>

  <tr><td style="padding:6px 20px 10px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eaf7ee;border:2px solid {GREEN};border-radius:10px;">
      <tr>
        {info("&#128197;", "Thu, Dec 24, 2026")}
        {info("&#128341;", "7:00 AM")}
        {info("&#128205;", "Alimosho LGA,<br>Egbeda Akowonjo")}
      </tr>
    </table>
  </td></tr>

  <tr><td style="padding:10px 14px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      {feat("&#127947;", "Workout sessions")}
      {feat("&#10084;&#65039;", "Free health check-ups")}
      {feat("&#128214;", "Health education")}
      {feat("&#128101;", "Community engagement")}
      {feat("&#127873;", "Games &amp; giveaways")}
    </tr></table>
  </td></tr>

  <tr><td align="center" style="padding:10px 20px 6px;">
    <div style="display:inline-block;background:{YELLOW};padding:10px 26px;border-radius:6px;transform:skewX(-8deg);">
      <span style="font-family:{FONT};font-size:28px;font-style:italic;font-weight:900;color:{BLUE};">FREE ENTRY</span>
    </div>
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:bold;color:{DARK};margin-top:8px;text-transform:uppercase;">Register to attend</div>
  </td></tr>

  <tr><td align="center" style="padding:14px 20px 26px;">
    <a href="{REG_URL}" style="background:{BLUE};color:#ffffff;text-decoration:none;padding:15px 30px;border-radius:30px;font-family:Arial,Helvetica,sans-serif;font-weight:bold;font-size:16px;display:inline-block;">View my registration &rarr;</a>
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#556;margin-top:10px;">alimosho-fitness-fiesta.eventbrite.com</div>
  </td></tr>

  <tr><td align="center" style="background:{DARK};padding:20px 16px;">
    <div style="font-family:{FONT};font-size:15px;letter-spacing:2px;font-style:italic;color:#ffffff;">MOVE &middot; LEARN &middot; CONNECT &middot; GROW</div>
    <div style="display:inline-block;background:{YELLOW};color:{BLUE};font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:bold;font-style:italic;padding:6px 14px;border-radius:4px;margin-top:12px;">&ldquo;Healthy Living is a Lifestyle, Not a Destination.&rdquo;</div>
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#bfe3c8;margin-top:12px;">Powered by RC Cola</div>
  </td></tr>

  <tr><td align="center" style="background:#f1f5f2;padding:14px 20px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#667;line-height:1.5;">
    You are receiving this because you registered for Alimosho Fitness Fiesta on Eventbrite.<br>
    <a href="{{$unsubscribe_link}}" style="color:#667;">Unsubscribe</a>
  </td></tr>

</table></td></tr></table></body></html>"""


# ---------- main ----------
def main():
    today = dt.datetime.now(TZ).date()
    days_left = int(FORCE_DAY) if FORCE_DAY else (EVENT_DATE - today).days
    print(f"Today (Lagos): {today} | days to event: {days_left}")

    people = get_attendees()
    print(f"Eventbrite attendees: {len(people)}")
    if not DRY_RUN:
        sync_subscribers(people)

    if days_left not in SEND_DAYS or days_left not in EMAILS:
        print("Not a countdown day. Done.")
        return

    subject, headline, paras = EMAILS[days_left]
    title = f"Fiesta Countdown D-{days_left}"
    if DRY_RUN:
        print(f"[dry run] would send: {title} | {subject}")
        return
    if not FORCE_DAY and campaign_exists(title):
        print(f"{title} already exists. Skipping.")
        return
    create_and_send(title, subject, render(days_left, headline, paras))


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTP error: {e.response.status_code} {e.response.text[:300]}")
        sys.exit(1)