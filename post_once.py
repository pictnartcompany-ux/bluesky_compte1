import os
from atproto import Client

HANDLE = os.getenv("BSKY_HANDLE")
APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD")
POST_TEXT = os.getenv("POST_TEXT", "").strip()

if not HANDLE or not APP_PASSWORD:
    raise SystemExit("Manque BSKY_HANDLE ou BSKY_APP_PASSWORD.")

if not POST_TEXT:
    raise SystemExit("POST_TEXT est vide.")

client = Client()
client.login(HANDLE, APP_PASSWORD)
client.send_post(POST_TEXT)
print("Publié sur Bluesky (Compte 1).")
