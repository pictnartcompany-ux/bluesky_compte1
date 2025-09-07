"""
Bluesky bot (Loufi’s Art / ArtLift) — Anti-spam safe, GitHub Actions friendly
- Max 4 posts/day, no posts at night (23:00–07:00 Europe/Brussels)
- 2 image GM/GN max/jour (1 matin, 1 soir), 2 liens max/jour, 1 long max/jour, le reste = reposts
- Créneaux souples (matin/midi/soir) + délais aléatoires → fluidité, pas d’effet “minuteur”
- Images choisies depuis ./assets/posts et évitées si utilisées dans les 14 derniers jours
- Opt-in engagements ONLY (mentions/replies). Likes autorisés ; pas de commentaires non sollicités
- Daily + hourly rate caps, random delays, 429 backoff
- Anti-répétition (texte 7j, images 14j)
- --oneshot mode pour CI

Local usage:
  pip install atproto
  export BSKY_HANDLE=your_handle.bsky.social
  export BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
  python bluesky_bot_safe.py --oneshot

Images:
  - Mettre les images dans ./assets/posts/ (jpg/jpeg/png)

Notes:
  - Bio claire sur Bluesky : "Automated account — contact @YourHuman".
  - Respect des normes communautaires ; interactions opt-in uniquement.
"""

import os
import sys
import json
import time
import random
import argparse
import datetime as dt
import pathlib
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

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

# Use explicit relative path with leading ./ so assets sit next to the script path-wise
IMAGES_DIR = "."
ALLOWED_EXTS = {".jpg", ".jpeg", ".png"}
IMAGE_RECENCY_DAYS = 14

# Quiet hours: no posting at night
NO_POST_START_HOUR = 23  # inclusive
NO_POST_END_HOUR = 7     # exclusive

# Global daily caps
MAX_POSTS_PER_DAY = 4
MAX_ENGAGEMENTS_PER_DAY = 10  # only opt-in mentions/replies

# Hourly caps (conservative)
MAX_POSTS_PER_HOUR = 2
MAX_ENGAGEMENTS_PER_HOUR = 3

# Random delay windows
DELAY_POST_MIN_S = 8
DELAY_POST_MAX_S = 28
DELAY_ENGAGE_MIN_S = 12
DELAY_ENGAGE_MAX_S = 45

# ======== CAPS PAR TYPE ========
MAX_IMG_GMGN_PER_DAY = 2      # 1 le matin + 1 le soir (au total 2)
MAX_SHORT_LINK_PER_DAY = 2    # liens bleus max/jour
MAX_GMGN_LONG_PER_DAY = 1     # texte long max/jour

# ========== TEXT LIBRARIES ==========
GM_SHORT = [
    "GM ☀️",
    "GM ✨",
    "GM 🌞",
    "GM 🌿",
    "GM 👋",
]
GN_SHORT_BASE = ["GN", "Gn", "gn", "Good night", "Night"]
RANDOM_GN_EMOJIS = ["🌙", "✨", "⭐", "💤", "🌌", "🫶", "💫", "😴", "🌠"]

GM_LONG = [
    "GM 🌱 Wishing you a day full of creativity and light.",
    "GM ✨ New day, new brushstrokes.",
    "GM 🌊 Let's dive into imagination today.",
]
GN_LONG = [
    "Good night 🌙💫 May your dreams be as colorful as art.",
    "GN 🌌 See you in tomorrow’s stories.",
    "Resting the canvas for tomorrow’s colors. GN ✨",
]

# Link posts must include plain URLs so Bluesky auto-detects and shows blue links.
LINK_POOLS = [
    SITE_URL,
    OPENSEA_URL,
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


def _pertype_zero() -> Dict[str, int]:
    return {"post_img_gmgn_short": 0, "post_gmgn_long": 0, "post_short_link": 0, "repost": 0}


def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                state = json.load(f)
                # Normalize shape for backward compatibility
                state.setdefault("history", [])
                state.setdefault("daily", {"date": "", "posts": 0, "engagements": 0})
                state.setdefault("hourly", {"key": "", "posts": 0, "engagements": 0})
                state.setdefault("processed_notifications", [])
                state.setdefault("recent_reposts", [])
                state.setdefault("pertype", _pertype_zero())
                return state
            except Exception:
                pass
    return {
        "history": [],
        "daily": {"date": "", "posts": 0, "engagements": 0},
        "hourly": {"key": "", "posts": 0, "engagements": 0},
        "processed_notifications": [],
        "recent_reposts": [],  # list of URIs
        "pertype": _pertype_zero(),
    }


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def reset_daily_if_needed(state: Dict[str, Any], now_local: dt.datetime) -> None:
    today = now_local.date().isoformat()
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "posts": 0, "engagements": 0}
        # trim repost memory daily as well
        state["recent_reposts"] = state.get("recent_reposts", [])[-200:]
        # reset per-type counters
        state["pertype"] = _pertype_zero()


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
    try:
        return client.app.bsky.notification.list_notifications(limit=limit)
    except TypeError:
        return client.app.bsky.notification.list_notifications()
    except Exception:
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


# Timeline fetch for safe reposts (from followed accounts only)
@with_backoff
def get_timeline(client: Client, limit: int = 50):
    try:
        return client.get_timeline(limit=limit)
    except Exception:
        # Fallback to app namespace if SDK differs
        return client.app.bsky.feed.get_timeline(limit=limit)


# ========== CONTENT PICKERS ==========

def in_time_window(now_local: dt.datetime, window: str) -> bool:
    h = now_local.hour
    if window == "morning":
        return 7 <= h < 11
    if window == "evening":
        return 19 <= h < 23
    if window == "midday":
        return 11 <= h < 19
    return False


def is_quiet_hours(now_local: dt.datetime) -> bool:
    h = now_local.hour
    if NO_POST_START_HOUR <= NO_POST_END_HOUR:
        return NO_POST_START_HOUR <= h < NO_POST_END_HOUR
    # Wrap-around case
    return h >= NO_POST_START_HOUR or h < NO_POST_END_HOUR


def pick_without_recent(state: Dict[str, Any], pool: List[str]) -> str:
    shuffled = pool[:]
    random.shuffle(shuffled)
    for s in shuffled:
        if not recently_used_text(state, s):
            return s
    return random.choice(pool)


def build_gm_short() -> str:
    return random.choice(GM_SHORT)


def build_gn_short() -> str:
    base = random.choice(GN_SHORT_BASE)
    if random.random() < 0.85:
        base = f"{base} {random.choice(RANDOM_GN_EMOJIS)}"
    return base


def pick_gmgn_text(state: Dict[str, Any], now_local: dt.datetime, long: bool = False) -> str:
    if in_time_window(now_local, "morning"):
        return pick_without_recent(state, GM_LONG) if long else build_gm_short()
    if in_time_window(now_local, "evening"):
        return pick_without_recent(state, GN_LONG) if long else build_gn_short()
    # Outside GM/GN windows, default to short neutral GM
    return build_gm_short()


def pick_link_short(state: Dict[str, Any]) -> str:
    # Always return a plain URL so Bluesky renders a blue link
    pools = LINK_POOLS[:]
    random.shuffle(pools)
    for url in pools:
        if not recently_used_text(state, url):
            return url
    return random.choice(LINK_POOLS)

# ========== ACTION SELECTION WITH CAPS ==========

def choose_action_with_caps(now_local: dt.datetime, pertype: Dict[str, int]) -> str:
    h = now_local.hour

    # Matin 07–11 : prioriser 1 image GM si pas encore postée
    if 7 <= h < 11:
        if pertype["post_img_gmgn_short"] < MAX_IMG_GMGN_PER_DAY and pertype["post_img_gmgn_short"] == 0:
            return "post_img_gmgn_short"
        if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
            return "post_short_link"
        if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
            return "post_gmgn_long"
        return "repost"

    # Milieu de journée 11–19 : liens d'abord (cap 2), sinon long (cap 1), sinon repost
    if 11 <= h < 19:
        if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
            return "post_short_link"
        if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
            return "post_gmgn_long"
        return "repost"

    # Soir 19–23 : prioriser 2e image GN si encore dispo
    if 19 <= h < 23:
        if pertype["post_img_gmgn_short"] < MAX_IMG_GMGN_PER_DAY:
            return "post_img_gmgn_short"
        if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
            return "post_short_link"
        if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
            return "post_gmgn_long"
        return "repost"

    # En dehors (au cas où) : fallback sobre
    if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
        return "post_short_link"
    if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
        return "post_gmgn_long"
    return "repost"

# ========== OPT-IN ENGAGEMENTS (MENTIONS / REPLIES) ==========

def fetch_unprocessed_mentions(client: Client, state: Dict[str, Any], handle: str, limit: int = 40):
    res = list_notifications(client, limit=limit)
    items = getattr(res, "notifications", []) or []
    processed = set(state.get("processed_notifications", []))
    fresh = []
    for n in items:
        reason = getattr(n, "reason", None)
        if reason not in ("mention", "reply"):
            continue
        nid = getattr(n, "cid", None) or getattr(n, "id", None) or getattr(n, "uri", None)
        if not nid or nid in processed:
            continue
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

# ========== SAFE REPOST PICKER ==========

def pick_safe_repost(client: Client, state: Dict[str, Any], handle: str):
    tl = get_timeline(client, limit=50)
    feed = getattr(tl, "feed", []) or []
    recent_reposts = set(state.get("recent_reposts", []))
    # Iterate random order to avoid always top items
    random.shuffle(feed)
    for item in feed:
        post = getattr(item, "post", None)
        if not post:
            continue
        author = getattr(post, "author", None)
        if not author:
            continue
        handle_self = os.getenv("BSKY_HANDLE", "").strip()
        # Skip own posts
        if getattr(author, "handle", "") == handle_self:
            continue
        uri = getattr(post, "uri", None)
        cid = getattr(post, "cid", None)
        if not uri or not cid:
            continue
        if uri in recent_reposts:
            continue
        # Avoid posts that are themselves reposts
        reason = getattr(item, "reason", None)
        if reason and getattr(reason, "$type", "").endswith("#reasonRepost"):
            continue
        return uri, cid
    return None

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

    # 1) Opt-in engagements from mentions/replies (likes allowed)
    if can_engage(state):
        fresh_mentions = fetch_unprocessed_mentions(client, state, handle, limit=40)
        random.shuffle(fresh_mentions)
        if fresh_mentions:
            n = fresh_mentions[0]
            kind = engage_for_notification(client, n)
            if kind:
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

    # 2) Posting (only if allowed by caps and not during quiet hours)
    if can_post(state) and not is_quiet_hours(now_local):
        # choisir l'action selon créneaux + caps
        pertype = state.get("pertype", _pertype_zero())
        action = choose_action_with_caps(now_local, pertype)

        # Fallback global si un type dépasse son cap exact au moment d'exécuter
        def downgrade_from(action_name: str) -> str:
            if action_name == "post_img_gmgn_short":
                if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
                    return "post_short_link"
                if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
                    return "post_gmgn_long"
                return "repost"
            if action_name == "post_gmgn_long":
                if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
                    return "post_short_link"
                return "repost"
            if action_name == "post_short_link":
                if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
                    return "post_gmgn_long"
                return "repost"
            return "repost"

        text: Optional[str] = None
        image: Optional[str] = None

        # Gestion spéciale des reposts (immédiat)
        if action == "repost":
            pick = pick_safe_repost(client, state, handle)
            if pick:
                uri, cid = pick
                try:
                    repost_post(client, uri, cid)
                    state.setdefault("recent_reposts", []).append(uri)
                    state["recent_reposts"] = state["recent_reposts"][-400:]
                    state["daily"]["posts"] += 1
                    state["hourly"]["posts"] += 1
                    state["pertype"]["repost"] = state.get("pertype", {}).get("repost", 0) + 1
                    save_state(state)
                    nap = random.uniform(DELAY_POST_MIN_S, DELAY_POST_MAX_S)
                    print(f"Reposted {uri}. Sleeping ~{int(nap)}s…")
                    time.sleep(nap)
                    return "reposted"
                except Exception as e:
                    print(f"[repost] error: {e}", file=sys.stderr)
            # If we couldn't find a safe repost or failed, tente un lien si dispo
            if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
                action = "post_short_link"
            elif pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
                action = "post_gmgn_long"
            else:
                # rien d'autre à faire proprement
                print("Nothing to repost or fallback. Skipping.")
                return "skip"

        # Préparation selon action
        if action == "post_img_gmgn_short":
            text = pick_gmgn_text(state, now_local, long=False)
            image = pick_fresh_image(state)
            if image is None:
                # dégrader si pas d'image fraîche dispo
                action = downgrade_from("post_img_gmgn_short")

        if action == "post_gmgn_long":
            # check cap runtime
            if pertype["post_gmgn_long"] >= MAX_GMGN_LONG_PER_DAY:
                action = downgrade_from("post_gmgn_long")
            else:
                text = pick_gmgn_text(state, now_local, long=True)
                if random.random() < 0.30:
                    image = pick_fresh_image(state)

        if action == "post_short_link":
            # check cap runtime
            if pertype["post_short_link"] >= MAX_SHORT_LINK_PER_DAY:
                action = downgrade_from("post_short_link")
            else:
                text = pick_link_short(state)

        # Si après dégradations on finit encore en repost, essayer repost une dernière fois
        if action == "repost":
            pick = pick_safe_repost(client, state, handle)
            if pick:
                uri, cid = pick
                try:
                    repost_post(client, uri, cid)
                    state.setdefault("recent_reposts", []).append(uri)
                    state["recent_reposts"] = state["recent_reposts"][-400:]
                    state["daily"]["posts"] += 1
                    state["hourly"]["posts"] += 1
                    state["pertype"]["repost"] = state.get("pertype", {}).get("repost", 0) + 1
                    save_state(state)
                    nap = random.uniform(DELAY_POST_MIN_S, DELAY_POST_MAX_S)
                    print(f"Reposted {uri}. Sleeping ~{int(nap)}s…")
                    time.sleep(nap)
                    return "reposted"
                except Exception as e:
                    print(f"[repost] error: {e}", file=sys.stderr)
            print("No repost candidate found. Skipping.")
            return "skip"

        # Poster texte/image
        if not text and action != "repost":
            # Sécurité: si rien n'a été préparé (rare), on saute
            print("No content prepared for action. Skipping.")
            return "skip"

        try:
            uri = post_text(client, text, image)
        except Exception as e:
            print(f"[post] error: {e}", file=sys.stderr)
            return "post_failed"

        if uri:
            remember_post(state, text, media=image)
            state["daily"]["posts"] += 1
            state["hourly"]["posts"] += 1
            # incrément per-type
            state["pertype"][action] = state.get("pertype", {}).get(action, 0) + 1
            save_state(state)
            nap = random.uniform(DELAY_POST_MIN_S, DELAY_POST_MAX_S)
            print(f"Posted: {text[:80]}{'…' if len(text)>80 else ''} {'[+image]' if image else ''}\nSleeping ~{int(nap)}s…")
            time.sleep(nap)
            return "posted"

        print("Post failed")
        return "post_failed"

    print("Nothing to do (caps reached / quiet hours / no mentions)")
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
            cool = random.uniform(60, 120)
            print(f"[Loop warn] {e}. Cooling down {int(cool)}s", file=sys.stderr)
            time.sleep(cool)
        now_local = dt.datetime.now(tz)
        if is_quiet_hours(now_local):
            nap = random.uniform(70*60, 120*60)
        elif 7 <= now_local.hour < 23:
            nap = random.uniform(25*60, 55*60)
        else:
            nap = random.uniform(45*60, 80*60)
        if random.random() < 0.18:
            nap += random.uniform(20*60, 40*60)
        print(f"Sleeping ~{int(nap//60)} min…")
        time.sleep(nap)


if __name__ == "__main__":
    main()
