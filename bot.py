import os
import requests

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

def post(msg):
    requests.post(WEBHOOK_URL, json={"content": msg}, timeout=15)

post("🏒 Edinburgh Capitals SNL bot is live and reporting.")
