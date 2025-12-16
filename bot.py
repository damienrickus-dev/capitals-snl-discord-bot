import json
import os
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

# Edinburgh Capitals publish SNL fixtures/results on their site.
FIXTURES_URL = "https://www.edcapitals.com/25-26-snl-fixtures/"

STATE_FILE = "posted.json"


def post_to_discord(content: str) -> None:
    r = requests.post(WEBHOOK_URL, json={"content": content}, timeout=20)
    r.raise_for_status()


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"posted": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def scrape_capitals_results():
    """
    Scrapes the Capitals fixtures page and looks for lines containing a score.
    This is heuristic-based because the page is not a formal API.
    It will only post when it detects a clear scoreline containing 'Capitals'.
    """
    html = requests.get(FIXTURES_URL, timeout=20).text
    soup = BeautifulSoup(html, "html.parser")

    # Convert page to a clean line list
    lines = [norm(x) for x in soup.get_text("\n").split("\n")]
    lines = [x for x in lines if x]

    results = []
    for i, line in enumerate(lines):
        if "Capitals" not in line:
            continue

        # Look around the "Capitals" line for a score pattern
        window = " ".join(lines[max(0, i - 4): min(len(lines), i + 10)])

        # Require a simple scoreline like "3 - 2" or "3–2" or "3 2" nearby
        m = re.search(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b", window)
        if not m:
            continue

        a = int(m.group(1))
        b = int(m.group(2))

        # Try to infer opponent from nearby known SNL teams (expand this list anytime)
        known = [
            "Capitals",
            "Warriors",
            "Rockets",
            "Pirates",
            "Tigers",
            "Kestrels",
            "Wild",
            "Thunder",
            "Lynx",
            "Sharks",
            "Stars",
        ]
        teams_found = [t for t in known if re.search(rf"\b{re.escape(t)}\b", window)]
        if "Capitals" not in teams_found or len(teams_found) <
