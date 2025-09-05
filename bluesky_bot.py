#!/usr/bin/env python3
"""
Bluesky bot (Loufi’s Art) — GitHub Actions friendly

Usage locally:
  pip install -r requirements.txt
  export BSKY_HANDLE=your_handle.bsky.social
  export BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
  python bluesky_bot.py --loop        # boucle continue
  python bluesky_bot.py --oneshot     # fait 1 action puis sort (pour GitHub Actions)

Notes:
- Timezone: Europe/Brussels
- Anti-répétition: évite de poster le même texte dans les 7 derniers jours
"""

import os
import sys
import json
import time
import random
import argparse
import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

# Bluesky SDK
from atproto import Client, models as M

# --------------- USER CONFIG ---------------

SITE_URL = "https://louphi1987.github.io/Site_de_Louphi/"
OPENSEA_URL = "https://opensea.io/collection/loufis-art"
TIMEZONE = "Europe/Brussels"

# Caps par jour (utiles même en oneshot si GitHub Actions déclenche souvent)
MAX_POSTS_PER_DAY = 4
MAX_ENGAGEMENTS_PER_DAY = 12

# Poids par type d’action
WEIGHTS = {
    "post_gm": 0.20,
    "post_value": 0.22,
    "post_nft": 0.18,
    "post_gn": 0.20,
    "engage": 0.20,
}

DISCOVERY_TAGS = ["art", "nft", "digitalart", "artist", "illustration", "aiart"]

# --------------- TEXT LIBRARIES ---------------

GM_POSTS = [
    "GM ☀️✨ Wishing everyone a day full of creativity and inspiration!",
    "GM 🌊 Let’s dive into imagination today!",
    "GM! New day, new brushstrokes 🖌️",
    "GM 🌱 Keep growing your art, one idea at a time.",
    "GM ✨ Creating stories in color and light today.",
]

GN_SHORT_BASE = ["GN", "Gn", "gn", "Good night", "Night"]

GN_LONG = [
    "Good night 🌙💫 May your dreams be as colorful as art.",
    "GN 🌌 See you in tomorrow’s stories.",
    "Calling it a day — see you among the stars 🌠 GN!",
    "Resting the canvas for tomorrow’s colors. GN ✨",
]

RANDOM_GN_EMOJIS = ["🌙", "✨", "⭐", "💤", "🌌", "🫶", "💫", "😴", "🌠"]

VALUE_POSTS = [
    "Fiction isn’t just escape — it’s expansion. Art gives it a shape. 🎨✨ Explore my worlds: {site}",
    "The best worlds are painted twice: once by the writer, once by the artist. {site}",
    "Passion drives creation. My universe of art & fiction grows every day 💡 Browse: {site}",
    "Art libraries are like portals. Step through: {site}",
    "Stories in motion, colors in orbit — welcome to my creative vault: {site}",
]

NFT_POSTS = [
    "New collectors, storytellers, dreamers… you’re welcome in Loufi’s Art 🌟 {opensea}",
    "NFTs aren’t just tokens — they’re stories frozen in time. Latest pieces: {opensea}",
    "Digital brushstrokes meet imagination 🚀 Collection: {opensea}",
    "Curated fragments of my universes — now on-chain. Discover: {opensea}",
    "If art is a portal, NFTs are the key 🔑 Unlock: {opensea}",
]

COMMENT_SHORT = [
    "Love the textures here 👏",
    "Feels like a dream 🌌",
    "Big fan of your style ✨",
    "Great composition 👏",
    "Beautiful palette 💫",
    "So much atmosphere here!",
]

COMMENT_EMOJIS = ["🔥", "👍", "👏", "😍", "✨", "🫶", "🎉", "💯", "🤝", "⚡", "🌟"]

# --------------- STATE PERSISTENCE ---------------

STATE_FILE = "bluesky_bot_state.json"

@dataclass
class DailyCounters:
    date: str
    posts: int
    engagements: int

def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"history": [], "daily": {"date": "", "posts": 0, "engagements": 0}}

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def reset_daily_counters_if_needed(state: Dict[str, Any], now_local: dt.datetime) -> None:
    today = now_local.date().isoformat()
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "posts": 0, "engagements": 0}

def remember_post(state: Dict[str, Any], text: str) -> None:
    now = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    state["history"].append({"text": text, "ts": now})
    state["history"] = state["history"][-300:]

def recently_used(state: Dict[str, Any], text: str, days: int = 7) -> bool:
    cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=days)
    for item in reversed(state["history"]):
        try:
            ts = dt.datetime.fromisoformat(item["ts"])
        except Exception:
            continue
        if ts >= cutoff and item["text"].strip() == text.strip():
            return True
    return False

# --------------- BLUESKY HELPERS ---------------

def bsky_login() -> Client:
    handle = os.getenv("BSKY_HANDLE", "").strip()
    app_pw = os.getenv("BSKY_APP_PASSWORD", "").strip()
    if not handle or not app_pw:
        print("ERROR: Missing BSKY_HANDLE or BSKY_APP_PASSWORD in environment", file=sys.stderr)
        sys.exit(1)
    client = Client()
    client.login(handle, app_pw)
    return client

def post_text(client: Client, text: str) -> Optional[str]:
    try:
        resp = client.send_post(text=text)
        return getattr(resp, "uri", None)
    except Exception as e:
        print(f"[post_text] Error: {e}", file=sys.stderr)
        return None

def search_recent_posts(client: Client, tags: List[str], limit: int = 30):
    try:
        q = " OR ".join([f"#{t}" for t in tags])
        res = client.app.bsky.feed.search_posts(q=q, limit=limit, sort="latest")
        return res.posts or []
    except Exception as e:
        print(f"[search_recent_posts] Error: {e}", file=sys.stderr)
        return []

def like_post(client: Client, uri: str, cid: str) -> bool:
    try:
        client.like(uri=uri, cid=cid)
        return True
    except Exception as e:
        print(f"[like_post] Error: {e}", file=sys.stderr)
        return False

def repost_post(client: Client, uri: str, cid: str) -> bool:
    try:
        client.repost(uri=uri, cid=cid)
        return True
    except Exception as e:
        print(f"[repost_post] Error: {e}", file=sys.stderr)
        return False

def reply_to_post(client: Client, parent_uri: str, parent_cid: str, text: str) -> bool:
    try:
        client.send_post(text=text, reply_to=M.AppBskyFeedPost.ReplyRef(
            parent=M.AppBskyFeedPost.ReplyRefParent(uri=parent_uri, cid=parent_cid)))
        return True
    except Exception as e:
        print(f"[reply_to_post] Error: {e}", file=sys.stderr)
        return False

# --------------- CONTENT PICKERS ---------------

def pick_without_recent(state: Dict[str, Any], pool: List[str]) -> str:
    shuffled = pool[:]
    random.shuffle(shuffled)
    for candidate in shuffled:
        if not recently_used(state, candidate):
            return candidate
    return random.choice(pool)

def build_gn_short() -> str:
    base = random.choice(GN_SHORT_BASE)
    if random.random() < 0.75:
        base = f"{base} {random.choice(RANDOM_GN_EMOJIS)}"
    return base

def pick_post_text(state: Dict[str, Any], kind: str) -> str:
    if kind == "post_gm":
        return pick_without_recent(state, GM_POSTS)
    if kind == "post_value":
        return pick_without_recent(state, VALUE_POSTS).format(site=SITE_URL)
    if kind == "post_nft":
        return pick_without_recent(state, NFT_POSTS).format(opensea=OPENSEA_URL)
    if kind == "post_gn":
        return pick_without_recent(state, GN_LONG) if random.random() < 0.55 else build_gn_short()
    raise ValueError(f"Unknown kind: {kind}")

def pick_comment_text() -> str:
    if random.random() < 0.6:
        if random.random() < 0.25:
            return random.choice(COMMENT_EMOJIS) + " " + random.choice(COMMENT_EMOJIS)
        return random.choice(COMMENT_EMOJIS)
    return random.choice(COMMENT_SHORT)

# --------------- SCHEDULING / RHYTHM ---------------

def in_time_window(now_local: dt.datetime, window: str) -> bool:
    hh = now_local.hour
    if window == "morning":
        return 7 <= hh < 11
    if window == "midday":
        return 11 <= hh < 16
    if window == "evening":
        return 19 <= hh < 23
    return False

def choose_action(now_local: dt.datetime, daily: DailyCounters) -> str:
    win = ("morning" if in_time_window(now_local, "morning")
           else "midday" if in_time_window(now_local, "midday")
           else "evening" if in_time_window(now_local, "evening")
           else "off")
    weights = WEIGHTS.copy()
    if win == "morning":
        weights["post_gm"] += 0.20
        weights["post_gn"] -= 0.10
    elif win == "evening":
        weights["post_gn"] += 0.25

    can_post = daily.posts < MAX_POSTS_PER_DAY
    can_engage = daily.engagements < MAX_ENGAGEMENTS_PER_DAY

    options = [(k, w) for k, w in weights.items()
               if (k.startswith("post") and can_post) or (k == "engage" and can_engage)]

    if not options:
        return "none"

    total = sum(w for _, w in options)
    r = random.random() * total
    cum = 0.0
    for k, w in options:
        cum += w
        if r <= cum:
            return k
    return options[-1][0]

# --------------- TICK (one action) ---------------

def do_one_action(client: Client, state: Dict[str, Any], tz: ZoneInfo) -> str:
    now_local = dt.datetime.now(tz)
    reset_daily_counters_if_needed(state, now_local)
    daily = DailyCounters(
        date=state["daily"]["date"],
        posts=state["daily"]["posts"],
        engagements=state["daily"]["engagements"],
    )

    action = choose_action(now_local, daily)
    if action == "none":
        return "skip"

    if action.startswith("post"):
        text = pick_post_text(state, action)
        if post_text(client, text):
            remember_post(state, text)
            state["daily"]["posts"] += 1
            save_state(state)
            print(f"Posted: {text}")
            return "posted"
        print("Post failed")
        return "post_failed"

    if action == "engage":
        posts = search_recent_posts(client, DISCOVERY_TAGS, limit=30)
        random.shuffle(posts)
        target_n = random.choice([2, 2, 3])
        done = 0
        for p in posts:
            if state["daily"]["engagements"] >= MAX_ENGAGEMENTS_PER_DAY:
                break
            if not getattr(p, "uri", None) or not getattr(p, "cid", None):
                continue
            r = random.random()
            if r < 0.65:
                success = like_post(client, p.uri, p.cid); action_name = "like"
            elif r < 0.80:
                success = repost_post(client, p.uri, p.cid); action_name = "repost"
            else:
                reply_text = pick_comment_text()
                success = reply_to_post(client, p.uri, p.cid, reply_text)
                action_name = f"reply({reply_text})"
            if success:
                state["daily"]["engagements"] += 1
                done += 1
                print(f"Engaged: {action_name} on {p.uri}")
                time.sleep(random.uniform(2.0, 5.0))
            if done >= target_n:
                break
        save_state(state)
        return "engaged"

    return "unknown"

# --------------- MAIN ---------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oneshot", action="store_true", help="Perform one action and exit (GitHub Actions mode)")
    parser.add_argument("--loop", action="store_true", help="Run continuous loop with sleeps")
    args = parser.parse_args()

    tz = ZoneInfo(TIMEZONE)
    client = bsky_login()
    state = load_state()

    if args.oneshot:
        status = do_one_action(client, state, tz)
        print(f"Status: {status}")
        sys.exit(0)

    # default to loop if --loop given (or nothing passed locally)
    print("Loop mode. Ctrl+C to stop.")
    while True:
        do_one_action(client, state, tz)
        # si tu utilises loop localement, on dort entre 25–55 min
        nap = random.uniform(25*60, 55*60)
        if random.random() < 0.18:
            nap += random.uniform(20*60, 40*60)
        now_local = dt.datetime.now(tz)
        if not (7 <= now_local.hour < 23):
            nap = max(nap, random.uniform(70*60, 120*60))
        print(f"Sleeping ~{int(nap//60)} min...")
        time.sleep(nap)

if __name__ == "__main__":
    main()
