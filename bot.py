import json
import os
import re
import logging
from datetime import datetime
from typing import List, Dict
import requests
from bs4 import BeautifulSoup

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Environment Variables
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
if not WEBHOOK_URL:
    logging.error("DISCORD_WEBHOOK_URL not set in environment variables.")
    raise EnvironmentError("DISCORD_WEBHOOK_URL not set in environment variables.")

# Constants
FIXTURES_URL = "https://www.edcapitals.com/25-26-snl-fixtures/"
STATE_FILE = "posted.json"

# Compile reusable regex patterns
SCORE_PATTERN = re.compile(r'\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b')
KNOWN_TEAMS = [
    "Capitals", "Warriors", "Rockets", "Pirates", "Tigers", "Kestrels",
    "Wild", "Thunder", "Lynx", "Sharks", "Stars",
]

# Utility Functions
def post_to_discord(content: str) -> None:
    try:
        response = requests.post(WEBHOOK_URL, json={"content": content}, timeout=20)
        response.raise_for_status()
        logging.info(f"Successfully posted to Discord: {content}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to post to Discord: {e}")
        raise

def load_state() -> Dict[str, List[str]]:
    if not os.path.exists(STATE_FILE):
        logging.info("State file not found. Initializing new state.")
        return {"posted": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.warning(f"Error loading state file: {e}")
        return {"posted": []}

def save_state(state: Dict[str, List[str]]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        logging.info("State saved successfully.")
    except Exception as e:
        logging.error(f"Failed to save state: {e}")
        raise

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def scrape_capitals_results() -> None:
    """
    Scrapes the Edinburgh Capitals SNL fixtures page for recent match results.
    Posts updates to Discord for matches not previously posted.
    """
    try:
        html = requests.get(FIXTURES_URL, timeout=20).text
        soup = BeautifulSoup(html, "html.parser")
        lines = [norm(x) for x in soup.get_text("\n").split("\n") if x.strip()]
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to fetch fixtures: {e}")
        return

    results = []
    for i, line in enumerate(lines):
        if "Capitals" not in line:
            continue

        # Contextual window around the "Capitals" line
        window = " ".join(lines[max(0, i - 4): min(len(lines), i + 10)])
        score_match = SCORE_PATTERN.search(window)
        if not score_match:
            continue

        # Extract scores
        team_a_score, team_b_score = int(score_match.group(1)), int(score_match.group(2))
        
        # Detect teams in the window
        teams_found = [team for team in KNOWN_TEAMS if re.search(rf"\b{re.escape(team)}\b", window, re.IGNORECASE)]
        if "Capitals" not in teams_found or len(teams_found) < 2:
            continue

        opponent = next(team for team in teams_found if team != "Capitals")
        result_description = f"Capitals {team_a_score} - {team_b_score} {opponent}"
        results.append(result_description)

    if not results:
        logging.info("No new results found.")
        return

    state = load_state()
    posted = state.get("posted", [])

    for result in results:
        if result in posted:
            logging.debug(f"Result already posted: {result}")
            continue
        post_to_discord(result)
        posted.append(result)
        logging.info(f"New result posted: {result}")

    state["posted"] = posted
    save_state(state)

if __name__ == "__main__":
    logging.info("Starting Capitals SNL Discord Bot...")
    scrape_capitals_results()
