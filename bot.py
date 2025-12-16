import json
import os
import re
import logging
from datetime import datetime, timedelta
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
STATE_FILE = "posted.json"

UK_TZ = ZoneInfo("Europe/London")

# -------------------------------------------------
# Regex
# -------------------------------------------------
SCORE_PATTERN = re.compile(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b")
FIXTURE_DT_PATTERN = re.compile(r"\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2})\b")

KNOWN_TEAMS = [
    "Capitals", "Warriors", "Rockets", "Pirates", "Tigers",
    "Kestrels", "Wild", "Thunder", "Lynx", "Sharks", "Stars",
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
        return {"results": [], "pregame": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return {"results": [], "pregame": []}

    state.setdefault("results", [])
    state.setdefault("pregame", [])
    return state


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def find_teams(text: str) -> List[str]:
    return [
        t for t in KNOWN_TEAMS
        if re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE)
    ]


def parse_datetimes(text: str) -> List[datetime]:
    out = []
    for m in FIXTURE_DT_PATTERN.finditer(text):
        try:
            dt = datetime.strptime(m.group(1), "%d %b %Y %H:%M").replace(tzinfo=UK_TZ)
            out.append(dt)
        except ValueError:
            pass
    return out

# -------------------------------------------------
# 1) FINAL RESULT POSTING
# -------------------------------------------------
def post_final_results() -> None:
    try:
        html = requests.get(FIXTURES_URL, timeout=20).text
    except requests.RequestException:
        return

    soup = BeautifulSoup(html, "html.parser")
    lines = [norm(x) for x in soup.get_text("\n").split("\n") if x.strip()]

    detected = []
    for i, line in enumerate(lines):
        if "Capitals" not in line:
            continue

        window = " ".join(lines[max(0, i - 4): min(len(lines), i + 10)])
        score = SCORE_PATTERN.search(window)
        if not score:
            continue

        teams = find_teams(window)
        if "Capitals" not in teams or len(teams) < 2:
            continue

        opponent = next(t for t in teams if t != "Capitals")
        s1, s2 = score.group(1), score.group(2)

        msg = (
            f"🏒 **Final Score — Edinburgh Capitals**\n"
            f"vs **{opponent}**\n"
            f"Result: **{s1}-{s2}**"
        )

        match_id = norm(f"{opponent}-{s1}-{s2}-{window[:80]}")
        detected.append({"id": match_id, "msg": msg})

    if not detected:
        return

    state = load_state()
    posted = set(state["results"])

    for d in {x["id"]: x for x in detected}.values():
        if d["id"] in posted:
            continue
        post_to_discord(d["msg"])
        posted.add(d["id"])

    state["results"] = sorted(posted)
    save_state(state)

# -------------------------------------------------
# 2) DAY-BEFORE GAME ALERT (T-24h)
# -------------------------------------------------
def get_next_capitals_game() -> Optional[Tuple[datetime, str]]:
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

        dts = [dt for dt in parse_datetimes(window) if dt > now]
        if not dts:
            continue

        teams = find_teams(window)
        if "Capitals" not in teams or len(teams) < 2:
            continue

        opponent = next(t for t in teams if t != "Capitals")
        candidates.append((min(dts), opponent))

    return min(candidates, key=lambda x: x[0]) if candidates else None


def post_day_before_alert() -> None:
    nxt = get_next_capitals_game()
    if not nxt:
        return

    game_dt, opponent = nxt
    now = datetime.now(UK_TZ)

    # Trigger window: between 24h and 23h before face-off
    delta_hours = (game_dt - now).total_seconds() / 3600
    if not (23 <= delta_hours <= 25):
        return

    state = load_state()
    pid = f"{game_dt.isoformat()}|{opponent}"
    if pid in state["pregame"]:
        return

    msg = (
        f"🏒 **Game Tomorrow — Edinburgh Capitals**\n"
        f"vs **{opponent}**\n"
        f"⏰ Face-off: **{game_dt:%a %d %b %Y, %H:%M}** (UK time)"
    )
    post_to_discord(msg)

    state["pregame"].append(pid)
    save_state(state)

# -------------------------------------------------
# Main
# -------------------------------------------------
if __name__ == "__main__":
    logging.info("Edinburgh Capitals Match Bot running")
    post_day_before_alert()
    post_final_results()
