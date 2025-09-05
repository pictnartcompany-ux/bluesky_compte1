"""
Bluesky bot (Loufi’s Art / ArtLift) — Anti-spam safe, GitHub Actions friendly
- Posts (GM/GN/value/NFT) with image attach windows
- Opt‑in engagements ONLY (mentions/replies to the bot)
- No hashtag scraping; no unsolicited comments
- Daily + hourly rate caps, random delays, 429 backoff
- Anti‑repetition (text 7d, images 14d)
- --oneshot mode for CI

Local usage:
  pip install atproto
  export BSKY_HANDLE=your_handle.bsky.social
  export BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
  python bluesky_bot_safe.py --oneshot

Images:
  - Put images in ./assets/posts/ (jpg/jpeg/png)

Notes:
  - Keep a clear bio on your Bluesky account: "Automated account — contact @YourHuman".
  - Respect community norms; opt-in interactions only.
"""

import os
import sys
import json
import time
import math
import random
import argparse
import datetime as dt
import pathlib
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

# Bluesky SDK
from atproto import Client, models as M

# ========== USER CONFIG ==========
SITE_URL = "https://louphi1987.github.io/Site_de_Louphi/"
OPENSEA_URL = "https://opensea.io/collection/loufis-art"
TIMEZONE = "Europe/Brussels"

IMAGES_DIR = "assets/posts"
ALLOWED_EXTS = {".jpg", ".jpeg", ".png"}
IMAGE_RECENCY_DAYS = 14

# Global daily caps (conservative)
MAX_POSTS_PER_DAY = 4
MAX_ENGAGEMENTS_PER_DAY = 10  # only opt-in mentions/replies

# Hourly caps (very conservative to avoid bursty behavior)
MAX_POSTS_PER_HOUR = 2
MAX_ENGAGEMENTS_PER_HOUR = 3

# Random delay windows
DELAY_POST_MIN_S = 8
DELAY_POST_MAX_S = 28
DELAY_ENGAGE_MIN_S = 12
DELAY_ENGAGE_MAX_S = 45

# Weights for action selection (posting only; engagements depend on inbox)
WEIGHTS = {
    "post_gm": 0.28,
    "post_value": 0.24,
    "post_nft": 0.18,
    "post_gn": 0.30,
}

# Image attach probabilities
ATTACH_IMAGE_PROB_MORNING_GM = 0.55  # 7–11h local
ATTACH_IMAGE_PROB_EVENING_GN = 0.60  # 19–23h local
ATTACH_IMAGE_PROB_OTHER = 0.15

# Anti‑spam: engage only when user explicitly addressed the bot
# We will consider notifications of type mention or reply.

DISCOVERY_TAGS = []  # intentionally unused (no hashtag scraping)

# ========== TEXT LIBRARIES ==========
GM_POSTS = [
    "GM ☀️✨ Wishing everyone a day full of creativity and inspiration!",
    "GM 🌊 Let’s dive into imagination today!",
    "GM! New day, new brushstrokes 🖌️",
    "GM 🌱 Keep growing your art, one idea at a time.",
    "GM ✨ Creating stories in color and light today.",
]

GN_SHORT_BASE = ["GN", "Gn", "gn", "Good night", "Night"]
RANDOM_GN_EMOJIS = ["🌙", "✨", "⭐", "💤", "🌌", "🫶", "💫", "😴", "🌠"]

GN_LONG = [
    "Good night 🌙💫 May your dreams be as colorful as art.",
    "GN 🌌 See you in tomorrow’s stories.",
    "Calling it a day — see you among the stars 🌠 GN!",
    "Resting the canvas for tomorrow’s colors. GN ✨",
]

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
    "Thanks for the mention!",
    "Appreciate it 🙏",
    "Thanks for looping me in ✨",
    "Thanks!",
]
COMMENT_EMOJIS = ["🔥", "👍", "👏", "😍", "✨", "🫶", "🎉", "💯", "🤝", "⚡", "🌟"]

# ========== STATE PERSISTENCE ==========
STATE_FILE = "bluesky_bot_state.json"

@dataclass
class DailyCounters:
    date: str
    posts: int
    engagements: int

@dataclass
class HourlyCounters:
    hour_key: str
    posts: int
    engagements: int


def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                pass
    return {
        "history": [],
        "daily": {"date": "", "posts": 0, "engagements": 0},
        "hourly": {"key": "", "posts": 0, "engagements": 0},
        "processed_notifications": [],  # to avoid double-engaging
    }


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def reset_daily_if_needed(state: Dict[str, Any], now_local: dt.datetime) -> None:
    today = now_local.date().isoformat()
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "posts": 0, "engagements": 0}


def reset_hourly_if_needed(state: Dict[str, Any], now_local: dt.datetime) -> None:
    key = f"{now_local.date().isoformat()}_{now_local.hour:02d}"
    if state["hourly"].get("key") != key:
        state["hourly"] = {"key": key, "posts": 0, "engagements": 0}


def remember_post(state: Dict[str, Any], text: str, media: Optional[str] = None) -> None:
    now = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    rec = {"text": text, "ts": now}
    if media:
        rec["media"] = media
    state["history"].append(rec)
    state["history"] = state["history"][-400:]


def recently_used_text(state: Dict[str, Any], text: str, days: int = 7) -> bool:
    cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=days)
    for item in reversed(state.get("history", [])):
        ts = item.get("ts")
        if not ts:
            continue
        try:
            when = dt.datetime.fromisoformat(ts)
        except Exception:
            continue
        if when >= cutoff and item.get("text", "").strip() == text.strip():
            return True
    return False


def recently_used_media(state: Dict[str, Any], media_path: str, days: int = IMAGE_RECENCY_DAYS) -> bool:
    cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=days)
    for item in reversed(state.get("history", [])):
        ts = item.get("ts")
        mp = item.get("media")
        if not ts or not mp:
            continue
        try:
            when = dt.datetime.fromisoformat(ts)
        except Exception:
            continue
        if when >= cutoff and mp == media_path:
            return True
    return False

# ========== FILES / IMAGES ==========

def list_local_images(folder: str) -> List[str]:
    p = pathlib.Path(folder)
    if not p.exists():
        return []
    return [str(f) for f in p.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_EXTS]


def pick_fresh_image(state: Dict[str, Any]) -> Optional[str]:
    imgs = list_local_images(IMAGES_DIR)
    if not imgs:
        return None
    random.shuffle(imgs)
    for img in imgs:
        if not recently_used_media(state, img, days=IMAGE_RECENCY_DAYS):
            return img
    return random.choice(imgs)

# ========== BSKY CLIENT & BACKOFF ==========

class RateLimitError(Exception):
    pass


def _needs_backoff(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "ratelimit" in msg or "rate limit" in msg


def with_backoff(fn):
    def wrapper(*args, **kwargs):
        delay = 5.0
        tries = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                tries += 1
                if _needs_backoff(e):
                    sleep_s = min(delay * (2 ** (tries - 1)), 60.0)
                    print(f"[BACKOFF] Rate limited; sleeping {sleep_s:.1f}s", file=sys.stderr)
                    time.sleep(sleep_s)
                    continue
                # transient network errors: brief retry
                if tries <= 2:
                    sleep_s = 2.0 * tries
                    print(f"[RETRY] {e}; sleeping {sleep_s:.1f}s", file=sys.stderr)
                    time.sleep(sleep_s)
                    continue
                raise
    return wrapper


@with_backoff
def bsky_login() -> Client:
    handle = os.getenv("BSKY_HANDLE", "").strip()
    app_pw = os.getenv("BSKY_APP_PASSWORD", "").strip()
    if not handle or not app_pw:
        raise RuntimeError("Missing BSKY_HANDLE or BSKY_APP_PASSWORD in env")
    client = Client()
    client.login(handle, app_pw)
    return client


@with_backoff
def post_text(client: Client, text: str, image_path: Optional[str] = None) -> Optional[str]:
    if not image_path:
        resp = client.send_post(text=text)
        return getattr(resp, "uri", None)
    with open(image_path, "rb") as f:
        blob = client.upload_blob(f.read())
    image_ref = getattr(blob, "blob", None) or getattr(blob, "data", None)
    embed = M.AppBskyEmbedImages.Main(
        images=[M.AppBskyEmbedImages.Image(alt="Artwork from Loufi’s Art", image=image_ref)]
    )
    resp = client.send_post(text=text, embed=embed)
    return getattr(resp, "uri", None)


@with_backoff
def list_notifications(client: Client, limit: int = 50):
    """Wrapper to handle SDK differences.
    Some atproto versions don't accept keyword args and will bubble a
    TypeError like: Client.request() got an unexpected keyword argument 'limit'.
    In that case, call without kwargs.
    """
    try:
        return client.app.bsky.notification.list_notifications(limit=limit)
    except TypeError:
        # Older SDK: no kwargs supported; returns default page size
        return client.app.bsky.notification.list_notifications()
    except Exception:
        # As a last resort, try without kwargs
        return client.app.bsky.notification.list_notifications()


@with_backoff
def like_post(client: Client, uri: str, cid: str) -> bool:
    client.like(uri=uri, cid=cid)
    return True


@with_backoff
def repost_post(client: Client, uri: str, cid: str) -> bool:
    client.repost(uri=uri, cid=cid)
    return True


@with_backoff
def reply_to_post(client: Client, parent_uri: str, parent_cid: str, text: str) -> bool:
    client.send_post(
        text=text,
        reply_to=M.AppBskyFeedPost.ReplyRef(
            parent=M.AppBskyFeedPost.ReplyRefParent(uri=parent_uri, cid=parent_cid)
        ),
    )
    return True

# ========== CONTENT PICKERS ==========

def in_time_window(now_local: dt.datetime, window: str) -> bool:
    h = now_local.hour
    if window == "morning":
        return 7 <= h < 11
    if window == "evening":
        return 19 <= h < 23
    if window == "midday":
        return 11 <= h < 16
    return False


def choose_post_kind(now_local: dt.datetime) -> str:
    weights = WEIGHTS.copy()
    if in_time_window(now_local, "morning"):
        weights["post_gm"] += 0.18
        weights["post_gn"] -= 0.10
    elif in_time_window(now_local, "evening"):
        weights["post_gn"] += 0.22

    total = sum(weights.values())
    r = random.random() * total
    cum = 0.0
    for k, w in weights.items():
        cum += w
        if r <= cum:
            return k
    return "post_value"


def pick_without_recent(state: Dict[str, Any], pool: List[str]) -> str:
    shuffled = pool[:]
    random.shuffle(shuffled)
    for s in shuffled:
        if not recently_used_text(state, s):
            return s
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
    return random.choice(VALUE_POSTS).format(site=SITE_URL)


def should_attach_image(now_local: dt.datetime, kind: str) -> bool:
    if kind == "post_gm":
        return random.random() < (ATTACH_IMAGE_PROB_MORNING_GM if in_time_window(now_local, "morning") else ATTACH_IMAGE_PROB_OTHER)
    if kind == "post_gn":
        return random.random() < (ATTACH_IMAGE_PROB_EVENING_GN if in_time_window(now_local, "evening") else ATTACH_IMAGE_PROB_OTHER)
    return random.random() < ATTACH_IMAGE_PROB_OTHER

# ========== OPT‑IN ENGAGEMENTS (MENTIONS / REPLIES) ==========

def fetch_unprocessed_mentions(client: Client, state: Dict[str, Any], handle: str, limit: int = 40):
    res = list_notifications(client, limit=limit)
    items = getattr(res, "notifications", []) or []
    processed = set(state.get("processed_notifications", []))
    fresh = []
    for n in items:
        # n.reason can be 'mention', 'reply', 'quote', 'like', 'repost', 'follow'
        reason = getattr(n, "reason", None)
        if reason not in ("mention", "reply"):
            continue
        if getattr(n, "isRead", False):
            # even if read, process only once
            pass
        nid = getattr(n, "cid", None) or getattr(n, "id", None) or getattr(n, "uri", None)
        if not nid or nid in processed:
            continue
        # Basic guard: ensure the post text contains our handle if reason==reply (optional)
        post = getattr(n, "record", None)
        # Some SDK versions expose the post text at n.record.text; we keep it optional
        txt = getattr(post, "text", "") if post else ""
        if reason == "reply" and handle not in (txt or ""):
            # allow replies even if handle not explicitly present; opt-in via thread
            pass
        fresh.append(n)
    return fresh


def engage_for_notification(client: Client, n) -> Optional[str]:
    uri = getattr(n, "uri", None)
    cid = getattr(n, "cid", None)
    if not uri or not cid:
        return None
    # Keep engagement minimal: like OR brief thank-you reply (75/25)
    if random.random() < 0.75:
        like_post(client, uri, cid)
        return "like"
    else:
        reply = random.choice(COMMENT_SHORT) if random.random() < 0.7 else random.choice(COMMENT_EMOJIS)
        reply_to_post(client, uri, cid, reply)
        return f"reply:{reply}"

# ========== ACTION ENGINE ==========

def can_post(state: Dict[str, Any]) -> bool:
    return state["daily"]["posts"] < MAX_POSTS_PER_DAY and state["hourly"]["posts"] < MAX_POSTS_PER_HOUR


def can_engage(state: Dict[str, Any]) -> bool:
    return state["daily"]["engagements"] < MAX_ENGAGEMENTS_PER_DAY and state["hourly"]["engagements"] < MAX_ENGAGEMENTS_PER_HOUR


def do_one_action(client: Client, state: Dict[str, Any], tz: ZoneInfo) -> str:
    now_local = dt.datetime.now(tz)
    reset_daily_if_needed(state, now_local)
    reset_hourly_if_needed(state, now_local)

    handle = os.getenv("BSKY_HANDLE", "").strip()

    # 1) Opt‑in engagements from mentions/replies
    if can_engage(state):
        fresh_mentions = fetch_unprocessed_mentions(client, state, handle, limit=40)
        random.shuffle(fresh_mentions)
        if fresh_mentions:
            n = fresh_mentions[0]
            kind = engage_for_notification(client, n)
            if kind:
                # mark processed
                nid = getattr(n, "cid", None) or getattr(n, "id", None) or getattr(n, "uri", None)
                if nid:
                    state.setdefault("processed_notifications", []).append(nid)
                    state["processed_notifications"] = state["processed_notifications"][-500:]
                state["daily"]["engagements"] += 1
                state["hourly"]["engagements"] += 1
                save_state(state)
                nap = random.uniform(DELAY_ENGAGE_MIN_S, DELAY_ENGAGE_MAX_S)
                print(f"Engaged ({kind}). Sleeping ~{int(nap)}s...")
                time.sleep(nap)
                return "engaged"

    # 2) Posting (only if allowed by caps)
    if can_post(state):
        kind = choose_post_kind(now_local)
        text = pick_post_text(state, kind)
        image = None
        if kind in ("post_gm", "post_gn") and should_attach_image(now_local, kind):
            image = pick_fresh_image(state)
        try:
            uri = post_text(client, text, image)
        except Exception as e:
            print(f"[post] error: {e}", file=sys.stderr)
            return "post_failed"
        if uri:
            remember_post(state, text, media=image)
            state["daily"]["posts"] += 1
            state["hourly"]["posts"] += 1
            save_state(state)
            nap = random.uniform(DELAY_POST_MIN_S, DELAY_POST_MAX_S)
            print(f"Posted: {text[:80]}{'…' if len(text)>80 else ''} {'[+image]' if image else ''}\nSleeping ~{int(nap)}s…")
            time.sleep(nap)
            return "posted"
        print("Post failed")
        return "post_failed"

    # 3) Nothing to do within safe limits
    print("Nothing to do (caps reached / no mentions)")
    return "skip"

# ========== MAIN ==========

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oneshot", action="store_true", help="Perform one safe action and exit (CI mode)")
    parser.add_argument("--loop", action="store_true", help="Run continuous loop with sleeps (local use)")
    args = parser.parse_args()

    tz = ZoneInfo(TIMEZONE)
    client = bsky_login()
    state = load_state()

    if args.oneshot or not args.loop:
        status = do_one_action(client, state, tz)
        print(f"Status: {status}")
        sys.exit(0)

    print("Loop mode (anti-spam). Ctrl+C to stop.")
    while True:
        try:
            do_one_action(client, state, tz)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # On unexpected error, cool down generously
            cool = random.uniform(60, 120)
            print(f"[Loop warn] {e}. Cooling down {int(cool)}s", file=sys.stderr)
            time.sleep(cool)
        # Between cycles, sleep a broader window depending on day time
        now_local = dt.datetime.now(tz)
        if 7 <= now_local.hour < 23:
            nap = random.uniform(25*60, 55*60)
        else:
            nap = random.uniform(70*60, 120*60)
        # occasional extra long nap
        if random.random() < 0.18:
            nap += random.uniform(20*60, 40*60)
        print(f"Sleeping ~{int(nap//60)} min…")
        time.sleep(nap)


if __name__ == "__main__":
    main()
