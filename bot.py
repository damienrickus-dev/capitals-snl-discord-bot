import json
import os
import re
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# -------------------------------------------------
# Logging
# -------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# -------------------------------------------------
# Environment
# -------------------------------------------------
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
if not WEBHOOK_URL:
    raise EnvironmentError("DISCORD_WEBHOOK_URL not set")

# -------------------------------------------------
# Constants
# -------------------------------------------------
FIXTURES_URL = "https://www.edcapitals.com/25-26-snl-fixtures/"
HOME_URL = "https://www.edcapitals.com/"
SCOREBOARD_URL = "https://www.edcapitals.com/tournament/2526-scottish-national-league/regular-season/"

STATE_FILE = "posted.json"
UK_TZ = ZoneInfo("Europe/London")

PREGAME_WINDOW_HOURS = 24
DAILY_SCOREBOARD_HOUR = 18
DAILY_SCOREBOARD_WINDOW_MIN = 5

# -------------------------------------------------
# Regex
# -------------------------------------------------
SCORE_PATTERN = re.compile(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b")
FIXTURE_DT_PATTERN = re.compile(r"\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2})\b")

KNOWN_TEAMS = [
    "Capitals", "Warriors", "Rockets", "Pirates", "Tigers", "Kestrels",
    "Wild", "Thunder", "Lynx", "Sharks", "Stars",
]

# -------------------------------------------------
# Utilities
# -------------------------------------------------
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def post_to_discord(content: str) -> None:
    r = requests.post(WEBHOOK_URL, json={"content": content}, timeout=20)
    r.raise_for_status()


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"posted": [], "pregame": [], "scoreboard_daily_date": ""}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return {"posted": [], "pregame": [], "scoreboard_daily_date": ""}

    state.setdefault("posted", [])
    state.setdefault("pregame", [])
    state.setdefault("scoreboard_daily_date", "")
    return state


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def find_teams(text: str) -> List[str]:
    return [
        t for t in KNOWN_TEAMS
        if re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE)
    ]


def parse_fixture_datetimes(text: str) -> List[datetime]:
    out = []
    for m in FIXTURE_DT_PATTERN.finditer(text):
        try:
            dt = datetime.strptime(m.group(1), "%d %b %Y %H:%M").replace(tzinfo=UK_TZ)
            out.append(dt)
        except ValueError:
            pass
    return out

# -------------------------------------------------
# Results (final scores)
# -------------------------------------------------
def scrape_capitals_results() -> None:
    try:
        html = requests.get(FIXTURES_URL, timeout=20).text
    except requests.RequestException:
        return

    soup = BeautifulSoup(html, "html.parser")
    lines = [norm(x) for x in soup.get_text("\n").split("\n") if x.strip()]

    found = []
    for i, line in enumerate(lines):
        if "Capitals" not in line:
            continue

        window = " ".join(lines[max(0, i - 4): min(len(lines), i + 10)])
        m = SCORE_PATTERN.search(window)
        if not m:
            continue

        teams = find_teams(window)
        if "Capitals" not in teams or len(teams) < 2:
            continue

        opponent = next(t for t in teams if t != "Capitals")
        s1, s2 = m.group(1), m.group(2)

        msg = (
            f"🏒 **Edinburgh Capitals — Result**\n"
            f"Detected result vs **{opponent}**: **{s1}-{s2}**"
        )

        match_id = norm(f"{opponent}-{s1}-{s2}-{window[:80]}")
        found.append({"id": match_id, "msg": msg})

    if not found:
        return

    uniq = {f["id"]: f for f in found}.values()
    state = load_state()
    posted = set(state["posted"])

    for f in uniq:
        if f["id"] in posted:
            continue
        post_to_discord(f["msg"])
        posted.add(f["id"])

    state["posted"] = sorted(posted)
    save_state(state)

# -------------------------------------------------
# Pregame (within 24h)
# -------------------------------------------------
def scrape_next_game() -> Optional[Tuple[datetime, str]]:
    try:
        html = requests.get(HOME_URL, timeout=20).text
    except requests.RequestException:
        return None

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")

    if "Upcoming Games" in text:
        text = text.split("Upcoming Games", 1)[1]

    lines = [norm(x) for x in text.split("\n") if x.strip()]
    now = datetime.now(UK_TZ)
    candidates = []

    for i, line in enumerate(lines):
        if "Capitals" not in line:
            continue

        window = " ".join(lines[max(0, i - 8): min(len(lines), i + 20)])
        if SCORE_PATTERN.search(window):
            continue

        dts = [dt for dt in parse_fixture_datetimes(window) if dt > now]
        if not dts:
            continue

        teams = find_teams(window)
        if "Capitals" not in teams or len(teams) < 2:
            continue

        opponent = next(t for t in teams if t != "Capitals")
        candidates.append((min(dts), opponent))

    return min(candidates, key=lambda x: x[0]) if candidates else None


def run_pregame() -> None:
    nxt = scrape_next_game()
    if not nxt:
        return

    dt, opponent = nxt
    now = datetime.now(UK_TZ)
    if (dt - now).total_seconds() / 3600 > PREGAME_WINDOW_HOURS:
        return

    state = load_state()
    pid = f"{dt.isoformat()}|{opponent}"
    if pid in state["pregame"]:
        return

    msg = (
        f"🏒 **Game Day Alert — Edinburgh Capitals**\n"
        f"vs **{opponent}**\n"
        f"⏰ Face-off: **{dt:%a %d %b %Y, %H:%M}** (UK time)"
    )
    post_to_discord(msg)

    state["pregame"].append(pid)
    save_state(state)

# -------------------------------------------------
# Daily scoreboard snapshot at 18:00
# -------------------------------------------------
def scrape_scoreboard_snapshot() -> Optional[str]:
    try:
        html = requests.get(SCOREBOARD_URL, timeout=20).text
    except requests.RequestException:
        return None

    soup = BeautifulSoup(html, "html.parser")
    lines = [norm(x) for x in soup.get_text("\n").split("\n") if x.strip()]

    if "Latest Scores" not in lines:
        return None

    start = lines.index("Latest Scores")
    chunk = lines[start:start + 200]

    items = []
    for i in range(len(chunk) - 8):
        window = " ".join(chunk[i:i + 10])
        m = SCORE_PATTERN.search(window)
        if not m:
            continue

        teams = find_teams(window)
        if len(teams) < 2:
            continue

        a, b = m.group(1), m.group(2)
        items.append(f"{teams[0]} {a}-{b} {teams[1]}")

    if not items:
        return None

    uniq = list(dict.fromkeys(items))[:5]
    return "🏒 **SNL Scoreboard (Latest Scores)**\n" + "\n".join(f"• {x}" for x in uniq)


def run_daily_scoreboard() -> None:
    now = datetime.now(UK_TZ)
    if not (now.hour == DAILY_SCOREBOARD_HOUR and now.minute <= DAILY_SCOREBOARD_WINDOW_MIN):
        return

    state = load_state()
    today = now.strftime("%Y-%m-%d")
    if state["scoreboard_daily_date"] == today:
        return

    msg = scrape_scoreboard_snapshot()
    if not msg:
        return

    post_to_discord(msg)
    state["scoreboard_daily_date"] = today
    save_state(state)

# -------------------------------------------------
# Main
# -------------------------------------------------
if __name__ == "__main__":
    logging.info("Edinburgh Capitals SNL bot running")
    scrape_capitals_results()
    run_pregame()
    run_daily_scoreboard()

    logging.info("Starting Edinburgh Capitals SNL Discord Bot...")
    scrape_capitals_results()
    run_pregame()
