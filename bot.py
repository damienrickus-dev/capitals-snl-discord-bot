import json
import os
import re
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ----------------------------
# Environment Variables
# ----------------------------
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
if not WEBHOOK_URL:
    logging.error("DISCORD_WEBHOOK_URL not set in environment variables.")
    raise EnvironmentError("DISCORD_WEBHOOK_URL not set in environment variables.")

# ----------------------------
# Constants
# ----------------------------
FIXTURES_URL = "https://www.edcapitals.com/25-26-snl-fixtures/"
HOME_URL = "https://www.edcapitals.com/"
STATE_FILE = "posted.json"

UK_TZ = ZoneInfo("Europe/London")

# Pre-game settings: post once when the next game is within this many hours
PREGAME_WINDOW_HOURS = 24

# Regex patterns
SCORE_PATTERN = re.compile(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b")  # e.g. 3-2 or 3–2
FIXTURE_DT_PATTERN = re.compile(r"\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2})\b")  # e.g. 27 Dec 2025 19:30

# Team list used for opponent detection (expand anytime)
KNOWN_TEAMS = [
    "Capitals", "Warriors", "Rockets", "Pirates", "Tigers", "Kestrels",
    "Wild", "Thunder", "Lynx", "Sharks", "Stars",
]

# ----------------------------
# Utility Functions
# ----------------------------
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def post_to_discord(content: str) -> None:
    try:
        response = requests.post(WEBHOOK_URL, json={"content": content}, timeout=20)
        response.raise_for_status()
        logging.info("Posted to Discord.")
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to post to Discord: {e}")
        raise


def load_state() -> Dict[str, List[str]]:
    if not os.path.exists(STATE_FILE):
        logging.info("State file not found. Initializing new state.")
        return {"posted": [], "pregame": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Ensure keys exist
        if "posted" not in state:
            state["posted"] = []
        if "pregame" not in state:
            state["pregame"] = []
        return state
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.warning(f"Error loading state file: {e}. Reinitializing.")
        return {"posted": [], "pregame": []}


def save_state(state: Dict[str, List[str]]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        logging.info("State saved successfully.")
    except Exception as e:
        logging.error(f"Failed to save state: {e}")
        raise


def find_teams_in_text(text: str) -> List[str]:
    return [
        team for team in KNOWN_TEAMS
        if re.search(rf"\b{re.escape(team)}\b", text, re.IGNORECASE)
    ]


def parse_fixture_datetimes(text: str) -> List[datetime]:
    """
    Extract fixture datetimes like: '27 Dec 2025 19:30'
    Returns list of timezone-aware datetimes (Europe/London).
    """
    dts: List[datetime] = []
    for m in FIXTURE_DT_PATTERN.finditer(text):
        raw = m.group(1)
        try:
            dt = datetime.strptime(raw, "%d %b %Y %H:%M").replace(tzinfo=UK_TZ)
            dts.append(dt)
        except ValueError:
            continue
    return dts


# ----------------------------
# Results Scraper (final results only)
# ----------------------------
def scrape_capitals_results() -> None:
    """
    Scrapes the Capitals fixtures page for completed match results.
    Posts updates to Discord for results not previously posted.
    Conservative wording: does not assume which score belongs to Capitals.
    """
    try:
        html = requests.get(FIXTURES_URL, timeout=20).text
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to fetch fixtures: {e}")
        return

    soup = BeautifulSoup(html, "html.parser")
    lines = [norm(x) for x in soup.get_text("\n").split("\n") if x.strip()]

    results = []
    for i, line in enumerate(lines):
        if "Capitals" not in line:
            continue

        window = " ".join(lines[max(0, i - 4): min(len(lines), i + 10)])
        score_match = SCORE_PATTERN.search(window)
        if not score_match:
            continue

        s1, s2 = score_match.group(1), score_match.group(2)

        teams_found = find_teams_in_text(window)
        if "Capitals" not in teams_found or len(teams_found) < 2:
            continue

        opponent = next((t for t in teams_found if t != "Capitals"), None)
        if not opponent:
            continue

        message = f"🏒 **Edinburgh Capitals — Result Update**\nDetected result vs **{opponent}**: **{s1}-{s2}**"

        # Stable-ish ID so small window changes don’t spam duplicates
        match_id = norm(f"{opponent}-{s1}-{s2}-{window[:80]}")
        results.append({"id": match_id, "message": message})

    if not results:
        logging.info("No results detected on fixtures page.")
        return

    # Dedupe within the run
    uniq = {r["id"]: r for r in results}
    results = list(uniq.values())

    state = load_state()
    posted = set(state.get("posted", []))

    new_posts = 0
    for r in results:
        if r["id"] in posted:
            continue
        post_to_discord(r["message"])
        posted.add(r["id"])
        new_posts += 1

    state["posted"] = sorted(posted)
    save_state(state)
    logging.info(f"Results: posted {new_posts} new update(s).")


# ----------------------------
# Pre-game Scraper (upcoming games)
# ----------------------------
def scrape_next_capitals_game() -> Optional[Tuple[datetime, str]]:
    """
    Scrapes the Capitals homepage for the next upcoming fixture involving Capitals.
    Returns (fixture_datetime, opponent) or None if not detected.
    Conservative parsing: looks for a future datetime near a 'Capitals' mention,
    and avoids blocks that include a scoreline (likely "recent results").
    """
    try:
        html = requests.get(HOME_URL, timeout=20).text
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to fetch homepage: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")

    # Reduce noise by focusing after "Upcoming Games" if present
    if "Upcoming Games" in text:
        text = text.split("Upcoming Games", 1)[1]

    text = text[:10000]
    lines = [norm(x) for x in text.split("\n") if x.strip()]

    now = datetime.now(UK_TZ)
    candidates: List[Tuple[datetime, str]] = []

    for i, line in enumerate(lines):
        if "Capitals" not in line:
            continue

        window = " ".join(lines[max(0, i - 8): min(len(lines), i + 20)])

        # Skip anything that looks like a completed result
        if SCORE_PATTERN.search(window):
            continue

        dts = parse_fixture_datetimes(window)
        if not dts:
            continue

        future_dts = [dt for dt in dts if dt > now]
        if not future_dts:
            continue

        teams_found = find_teams_in_text(window)
        if "Capitals" not in teams_found or len(teams_found) < 2:
            continue

        opponent = next((t for t in teams_found if t != "Capitals"), None)
        if not opponent:
            continue

        fixture_dt = sorted(future_dts)[0]
        candidates.append((fixture_dt, opponent))

    if not candidates:
        return None

    # Return the earliest upcoming candidate
    return sorted(candidates, key=lambda x: x[0])[0]


def run_pregame() -> None:
    nxt = scrape_next_capitals_game()
    if not nxt:
        logging.info("No upcoming Capitals game detected.")
        return

    fixture_dt, opponent = nxt
    now = datetime.now(UK_TZ)
    hours_to = (fixture_dt - now).total_seconds() / 3600

    if hours_to > PREGAME_WINDOW_HOURS:
        logging.info(f"Next game is in {hours_to:.1f}h; outside pregame window ({PREGAME_WINDOW_HOURS}h).")
        return

    state = load_state()
    pre = set(state.get("pregame", []))

    pre_id = f"{fixture_dt.isoformat()}|{opponent}"
    if pre_id in pre:
        logging.info("Pregame already posted for this fixture.")
        return

    msg = (
        f"🏒 **Game Day Alert — Edinburgh Capitals**\n"
        f"Next game vs **{opponent}**\n"
        f"⏰ Face-off: **{fixture_dt:%a %d %b %Y, %H:%M}** (UK time)"
    )
    post_to_discord(msg)

    pre.add(pre_id)
    state["pregame"] = sorted(pre)
    save_state(state)
    logging.info("Pregame message posted.")


# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    logging.info("Starting Edinburgh Capitals SNL Discord Bot...")
    scrape_capitals_results()
    run_pregame()
