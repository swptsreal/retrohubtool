# -*- coding: utf-8 -*-
"""YouTube client via InnerTube API for RetroHub on TrimUI devices."""

import html
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request

_EMOJI_PATTERN = re.compile(
    "[\U00010000-\U0010ffff"  # Supplemental symbols & pictographs, emojis
    "\u2600-\u26ff"            # Misc symbols
    "\u2700-\u27bf"            # Dingbats
    "\u2300-\u23ff"            # Misc technical
    "\ufe00-\ufe0f"            # Variation selectors
    "\u200d"                   # Zero-width joiner
    "]+",
    flags=re.UNICODE
)


def clean_yt_text(text: str) -> str:
    """Sanitize HTML entities, emojis, and unrenderable characters for SDL_ttf."""
    if not text:
        return ""
    # 1. Unescape HTML entities (&amp; -> &, &#39; -> ', &quot; -> ", etc.)
    text = html.unescape(text)
    # 2. Replace typographic quotes / dashes with standard ASCII equivalents
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    text = text.replace("–", "-").replace("—", "-")
    # 3. Strip emojis / high unicode pictographs
    text = _EMOJI_PATTERN.sub("", text)
    # 4. Collapse multiple whitespaces
    text = re.sub(r"\s+", " ", text).strip()
    return text


from rh.paths import (
    YT_HISTORY_FILE,
    YT_FEED_CACHE_FILE,
    YT_FEED_FALLBACK_FILE,
    YT_FAVORITES_FILE,
    YT_FAVORITES_FALLBACK_FILE,
    SDCARD_PATH,
)
from .neterrors import classify_error as _classify_error

# Last network error key (an i18n key from rh.neterrors), so the UI can explain
# an empty result instead of just saying "no videos". Cleared on every fetch.
_LAST_ERROR = ""


def get_last_error() -> str:
    return _LAST_ERROR

INNERTUBE_URL = "https://www.youtube.com/youtubei/v1"
INNERTUBE_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

DEFAULT_QUERIES = ["Music", "KPOP", "USUK", "Tiktok"]
DEFAULT_PRESET_QUERIES = ("Music", "KPOP", "USUK", "Tiktok")
LEGACY_PRESETS = {
    "mv", "nhạc trẻ", "nhạc tiktok", "kpop", "nhảy tiktok", "game", "remix",
    "mv vpop", "nhạc hot tiktok", "hot girl tiktok", "mv kpop",
}


def parse_age_hours(text: str) -> float:
    """Parse relative time string into hours if needed."""
    if not text:
        return 999999.0
    s = text.strip().lower()
    if "hôm nay" in s:
        return 4.0
    if "hôm qua" in s or "yesterday" in s:
        return 24.0
    m = re.search(r"(\d+)", s)
    if not m:
        return 999999.0
    val = float(m.group(1))
    if "giây" in s or "second" in s:
        return val / 3600.0
    elif "phút" in s or "minute" in s:
        return val / 60.0
    elif "giờ" in s or "hour" in s:
        return val
    elif "ngày" in s or "day" in s:
        return val * 24.0
    elif "tuần" in s or "week" in s:
        return val * 24.0 * 7.0
    elif "tháng" in s or "month" in s:
        return val * 24.0 * 30.0
    elif "năm" in s or "year" in s:
        return val * 24.0 * 365.0
    return 999999.0


def get_effective_query(query: str) -> str:
    """Return cleaned query keyword without altering sorting or appending year."""
    return (query or "").strip()


def load_search_history() -> list:
    """Load list of recent search queries from disk. Falls back to DEFAULT_QUERIES if empty or missing."""
    try:
        if os.path.exists(YT_HISTORY_FILE) and os.path.getsize(YT_HISTORY_FILE) > 2:
            with open(YT_HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and data:
                    cleaned = []
                    for q in data:
                        q_str = str(q).strip()
                        if q_str and q_str not in cleaned:
                            cleaned.append(q_str)
                    if cleaned:
                        return cleaned[:10]
    except Exception as e:
        pass
    return list(DEFAULT_QUERIES)


def save_search_history(queries: list):
    """Save list of recent search queries to disk."""
    try:
        clean_list = []
        for q in queries:
            q_str = str(q).strip()
            if q_str and q_str not in clean_list:
                clean_list.append(q_str)
        os.makedirs(os.path.dirname(YT_HISTORY_FILE), exist_ok=True)
        with open(YT_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(clean_list[:10], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[rh.yt] Error saving search history: {e}")


def remove_search_history_item(query_to_remove: str) -> list:
    """Remove any search keyword (preset or custom) from history and save to disk."""
    q_clean = (query_to_remove or "").strip().lower()
    if not q_clean:
        return load_search_history()

    current_history = load_search_history()
    new_history = [q for q in current_history if q.strip().lower() != q_clean]
    if not new_history:
        new_history = list(DEFAULT_QUERIES)

    save_search_history(new_history)
    return new_history


def load_favorites() -> list:
    """Load user favorite videos list from persistent storage."""
    for p in (YT_FAVORITES_FILE, YT_FAVORITES_FALLBACK_FILE):
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    cleaned = []
                    for it in data:
                        if isinstance(it, dict) and it.get("id"):
                            it["title"] = clean_yt_text(it.get("title", ""))
                            it["channel"] = clean_yt_text(it.get("channel", ""))
                            it["disp_title"] = it["title"] if len(it["title"]) <= 120 else it["title"][:117] + "..."
                            cleaned.append(it)
                    return cleaned
            except Exception:
                pass
    return []


def save_favorites(favorites: list):
    """Save user favorite videos list to persistent storage."""
    for p in (YT_FAVORITES_FILE, YT_FAVORITES_FALLBACK_FILE):
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(favorites, f, ensure_ascii=False, indent=2)
            return
        except Exception:
            continue


def is_favorite(video_id: str, favorites_list: list = None) -> bool:
    """Check if a video ID is in favorites."""
    if not video_id:
        return False
    favs = favorites_list if favorites_list is not None else load_favorites()
    return any(item.get("id") == video_id for item in favs)


def toggle_favorite(video: dict, favorites_list: list) -> tuple:
    """Toggle favorite status of a video. Returns (new_favorites_list, is_added)."""
    if not video or not video.get("id") or video.get("id") == "__LOAD_MORE__":
        return favorites_list, False
    vid = video["id"]
    new_favs = [v for v in favorites_list if v.get("id") != vid]
    if len(new_favs) == len(favorites_list):
        # Video was not in favorites, add it
        new_favs.insert(0, dict(video))
        save_favorites(new_favs)
        return new_favs, True
    else:
        # Video was in favorites, removed
        save_favorites(new_favs)
        return new_favs, False


def load_feed_cache(category: str = "trending") -> tuple:
    """Load cached feed items for category. Returns (items_list, timestamp)."""
    for path in (YT_FEED_CACHE_FILE, YT_FEED_FALLBACK_FILE):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    cat = data.get(category)
                    if isinstance(cat, dict):
                        return cat.get("items", []), float(cat.get("timestamp", 0))
            except Exception:
                pass
    return [], 0.0


def save_feed_cache(category: str, items: list):
    """Save feed items for category to disk cache."""
    if not items:
        return
    for path in (YT_FEED_CACHE_FILE, YT_FEED_FALLBACK_FILE):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            if not isinstance(data, dict):
                data = {}
            data[category] = {
                "timestamp": time.time(),
                "items": items,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            break
        except Exception:
            continue

WEB_CONTEXT = {
    "client": {
        "clientName": "WEB",
        "clientVersion": "2.20240101.00.00",
        "hl": "vi",
        "gl": "VN",
    }
}

# Fallback client used when the web client returns nothing (some networks/IPs
# get a consent or throttled response for WEB).
ANDROID_CONTEXT = {
    "client": {
        "clientName": "ANDROID",
        "clientVersion": "19.09.37",
        "androidSdkVersion": 30,
        "hl": "vi",
        "gl": "VN",
    }
}


def _get_ssl_context():
    """Create unverified SSL context for embedded Linux devices without root CAs."""
    try:
        return ssl._create_unverified_context()
    except Exception:
        return None


def _make_request(endpoint: str, payload: dict, timeout: int = 7) -> dict:
    """Send JSON POST request to YouTube InnerTube endpoint with SSL bypass.

    Includes the public web API key and the client headers YouTube expects;
    without them some networks get a consent/error page instead of JSON.
    """
    url = f"{INNERTUBE_URL}/{endpoint}?key={INNERTUBE_API_KEY}&prettyPrint=false"
    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=req_data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Origin": "https://www.youtube.com",
            "Referer": "https://www.youtube.com/",
            "Accept-Language": "vi,en;q=0.9",
            "X-YouTube-Client-Name": "1",
            "X-YouTube-Client-Version": WEB_CONTEXT["client"]["clientVersion"],
        },
    )
    ctx = _get_ssl_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _extract_videos_from_json(node, found_list: list, limit: int = 30):
    """Recursively traverse JSON structure to find all videoRenderer objects."""
    if len(found_list) >= limit:
        return

    if isinstance(node, dict):
        v = node.get("videoRenderer") or node.get("gridVideoRenderer") or node.get("compactVideoRenderer")
        if v:
            vid = v.get("videoId")
            if vid:
                # Title
                title_runs = v.get("title", {}).get("runs", [])
                title = title_runs[0].get("text", "") if title_runs else v.get("title", {}).get("simpleText", "")
                title = clean_yt_text(title)
                if not title:
                    title = "Video YouTube"

                # Channel
                owner_runs = (
                    v.get("ownerText", {}).get("runs", [])
                    or v.get("shortBylineText", {}).get("runs", [])
                    or v.get("longBylineText", {}).get("runs", [])
                )
                channel = clean_yt_text(owner_runs[0].get("text", "") if owner_runs else "")

                # Duration
                duration = clean_yt_text(v.get("lengthText", {}).get("simpleText", ""))

                # Thumbnail: Always use standard YouTube 16:9 JPEG (mqdefault.jpg: 320x180)
                # YouTube InnerTube returns WebP which SDL_image on TrimUI cannot decode.
                thumb_url = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"

                # Upload relative date & computed age in hours
                pub = clean_yt_text(v.get("publishedTimeText", {}).get("simpleText", ""))
                age = parse_age_hours(pub)

                # Pre-format truncated titles and channel info for zero-overhead UI rendering
                disp_title = title if len(title) <= 120 else title[:117] + "..."
                if pub:
                    info_str = f"{channel} • {pub}" if channel else pub
                else:
                    info_str = channel
                disp_info = info_str if len(info_str) <= 34 else info_str[:32] + "..."

                # Avoid duplicate video IDs
                if not any(it["id"] == vid for it in found_list):
                    found_list.append({
                        "id": vid,
                        "title": title,
                        "disp_title": disp_title,
                        "channel": channel,
                        "disp_info": disp_info,
                        "duration": duration,
                        "thumb": thumb_url,
                        "pub": pub,
                        "age": age,
                    })

        for val in node.values():
            _extract_videos_from_json(val, found_list, limit)
            if len(found_list) >= limit:
                break

    elif isinstance(node, list):
        for item in node:
            _extract_videos_from_json(item, found_list, limit)
            if len(found_list) >= limit:
                break


def _find_continuation_token(node) -> str:
    """Recursively search for continuation token in response dict/list."""
    if isinstance(node, dict):
        if "continuationCommand" in node:
            tok = node["continuationCommand"].get("token")
            if tok:
                return tok
        for val in node.values():
            tok = _find_continuation_token(val)
            if tok:
                return tok
    elif isinstance(node, list):
        for item in node:
            tok = _find_continuation_token(item)
            if tok:
                return tok
    return ""


_CONTINUATION_TOKENS = {}


def get_continuation_token(query: str) -> str:
    """Get stored continuation token for a search query."""
    return _CONTINUATION_TOKENS.get(query, "")


def set_continuation_token(query: str, token: str):
    """Store continuation token for pagination."""
    if token:
        _CONTINUATION_TOKENS[query] = token
    else:
        _CONTINUATION_TOKENS.pop(query, None)


def fetch_more_youtube(query: str, cont_token: str = None) -> tuple:
    """Fetch next batch of videos using continuation token. Returns (videos_list, next_token)."""
    token = cont_token or get_continuation_token(query)
    if not token:
        return [], ""

    payload = {
        "context": WEB_CONTEXT,
        "continuation": token,
    }
    try:
        cont_data = _make_request("search", payload, timeout=6)
    except Exception as e:
        print(f"[rh.yt] Error fetching more videos for '{query}': {e}")
        return [], ""

    more_videos = []
    try:
        _extract_videos_from_json(cont_data, more_videos, limit=18)
    except Exception as e:
        print(f"[rh.yt] Extract more videos error: {e}")

    next_token = _find_continuation_token(cont_data)
    set_continuation_token(query, next_token)
    return more_videos, next_token


def search_youtube(query: str, limit: int = 24) -> list:
    """Search videos on YouTube by keyword, preserving natural YouTube ranking without sorting.

    Returns a list of dicts:
        [{'id': str, 'title': str, 'channel': str, 'duration': str, 'thumb': str, 'pub': str, 'age': float}]
    """
    global _LAST_ERROR
    clean_query = (query or "").strip()
    if not clean_query:
        return []
    _LAST_ERROR = ""

    eff_query = get_effective_query(clean_query)

    payload = {
        "context": WEB_CONTEXT,
        "query": eff_query,
    }

    try:
        data = _make_request("search", payload)
    except Exception as e:
        print(f"[rh.yt] Search error for '{eff_query}': {e}")
        _LAST_ERROR = _classify_error(e)
        return []

    results = []
    try:
        _extract_videos_from_json(data, results, limit=limit)
    except Exception as e:
        print(f"[rh.yt] Extract videos error: {e}")

    # The web client sometimes returns an empty shell (consent/throttling).
    # Retry once with the Android client before giving up.
    if not results:
        try:
            data2 = _make_request("search", {"context": ANDROID_CONTEXT, "query": eff_query})
            _extract_videos_from_json(data2, results, limit=limit)
        except Exception as e:
            print(f"[rh.yt] Android fallback error: {e}")
            _LAST_ERROR = _classify_error(e)

    # Extract and store continuation token for "Load more" at the end of the list
    cont_token = _find_continuation_token(data)
    set_continuation_token(clean_query, cont_token)

    # Only fetch continuation if the first page returned fewer than 6 candidate videos
    if len(results) < 6 and cont_token:
        try:
            cont_payload = {"context": WEB_CONTEXT, "continuation": cont_token}
            cont_data = _make_request("search", cont_payload, timeout=5)
            _extract_videos_from_json(cont_data, results, limit=limit)
            next_token = _find_continuation_token(cont_data)
            set_continuation_token(clean_query, next_token)
        except Exception as e:
            print(f"[rh.yt] Continuation fetch error: {e}")

    final_results = results[:limit]
    if final_results:
        save_feed_cache(clean_query, final_results)
    return final_results


def get_trending(limit: int = 24) -> list:
    """Fetch YouTube Trending / Music videos."""
    cached, ts = load_feed_cache("Music")
    if cached and (time.time() - ts) < 3600:
        return cached
    items = search_youtube("Nhạc Trẻ Trending Việt Nam", limit=limit)
    if items:
        save_feed_cache("Music", items)
    return items


def _deep_find(node, key):
    """Return the first value stored under *key* anywhere in a nested JSON tree."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for val in node.values():
            found = _deep_find(val, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _deep_find(item, key)
            if found is not None:
                return found
    return None


def fetch_watch_metadata(video_id: str) -> dict:
    """Fetch a video's detail page: title, channel, views, description, related.

    Uses the InnerTube `next` endpoint. Returns None on network failure so the
    caller can fall back to the data already known from the grid.
    """
    if not video_id:
        return None

    data = None
    for ctx in (WEB_CONTEXT, ANDROID_CONTEXT):
        try:
            data = _make_request("next", {"context": ctx, "videoId": video_id}, timeout=7)
        except Exception as e:
            print(f"[rh.yt] Watch metadata error for {video_id}: {e}")
            data = None
            continue
        if data and _deep_find(data, "videoPrimaryInfoRenderer"):
            break
        data = None
    if not data:
        return None

    primary = _deep_find(data, "videoPrimaryInfoRenderer") or {}
    secondary = _deep_find(data, "videoSecondaryInfoRenderer") or {}

    title_runs = primary.get("title", {}).get("runs", [])
    title = title_runs[0].get("text", "") if title_runs else primary.get("title", {}).get("simpleText", "")

    views = ""
    vc = primary.get("viewCount", {})
    if isinstance(vc, dict):
        views = (
            vc.get("videoViewCountRenderer", {}).get("viewCount", {}).get("simpleText", "")
            or vc.get("simpleText", "")
        )

    published = primary.get("dateText", {}).get("simpleText", "")

    owner = secondary.get("owner", {}).get("videoOwnerRenderer", {})
    o_runs = owner.get("title", {}).get("runs", [])
    channel = o_runs[0].get("text", "") if o_runs else ""

    description = secondary.get("attributedDescription", {}).get("content", "")
    if not description:
        d_runs = secondary.get("description", {}).get("runs", [])
        description = " ".join(r.get("text", "") for r in d_runs)

    # Related videos now come back as lockupViewModel (not videoRenderer), so
    # use the playlist extractor which understands both shapes. Keep only real
    # 11-char video IDs (drops channel/playlist lockups) and the current video.
    related = []
    try:
        _extract_playlist_videos_from_json(data, related, limit=24)
    except Exception:
        pass
    if not related:
        try:
            _extract_videos_from_json(data, related, limit=13)
        except Exception:
            pass
    related = [r for r in related
               if r.get("id") and r.get("id") != video_id and len(r.get("id", "")) == 11][:12]
    for r in related:
        ch = r.get("channel", "")
        r["pub"] = ""
        r["disp_info"] = ch if len(ch) <= 34 else ch[:32] + "..."

    return {
        "title": clean_yt_text(title),
        "channel": clean_yt_text(channel),
        "views": clean_yt_text(views),
        "published": clean_yt_text(published),
        "description": clean_yt_text(description),
        "related": related,
    }


# Aliases for cross-module compatibility
search_videos = search_youtube
fetch_trending = get_trending


def extract_playlist_id(url_or_text: str) -> str:
    """Extract YouTube playlist ID from various URL formats or raw ID string."""
    s = str(url_or_text or "").strip()
    if not s:
        return ""
    m = re.search(r"[?&]list=([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    clean = s.split("/")[-1].split("?")[0].strip()
    if clean.startswith("VL"):
        clean = clean[2:]
    return clean


def _extract_playlist_videos_from_json(node, found_list: list, limit: int = 150):
    """Extract video items from playlist browse/search InnerTube response."""
    if len(found_list) >= limit:
        return

    if isinstance(node, dict):
        # 1. lockupViewModel (modern InnerTube)
        if "lockupViewModel" in node:
            vm = node["lockupViewModel"]
            vid = vm.get("contentId")
            if not vid:
                try:
                    vid = (
                        vm.get("rendererContext", {})
                        .get("commandContext", {})
                        .get("onTap", {})
                        .get("innertubeCommand", {})
                        .get("watchEndpoint", {})
                        .get("videoId")
                    )
                except Exception:
                    pass

            if vid and not any(it["id"] == vid for it in found_list):
                # Title
                v_title = (
                    vm.get("metadata", {})
                    .get("lockupMetadataViewModel", {})
                    .get("title", {})
                    .get("content", "")
                )
                if not v_title:
                    v_title = (
                        vm.get("rendererContext", {})
                        .get("accessibilityContext", {})
                        .get("label", "")
                    )
                v_title = clean_yt_text(v_title) or f"Video {vid}"

                # Channel
                v_channel = ""
                try:
                    rows = (
                        vm.get("metadata", {})
                        .get("lockupMetadataViewModel", {})
                        .get("metadata", {})
                        .get("contentMetadataViewModel", {})
                        .get("metadataRows", [])
                    )
                    if rows and rows[0].get("metadataParts"):
                        v_channel = (
                            rows[0]["metadataParts"][0]
                            .get("text", {})
                            .get("content", "")
                        )
                except Exception:
                    pass
                v_channel = clean_yt_text(v_channel)

                # Duration
                v_dur = ""
                try:
                    cimg = vm.get("contentImage", {})
                    t_vm = (
                        cimg.get("thumbnailViewModel")
                        or cimg.get("collectionThumbnailViewModel", {})
                        .get("primaryThumbnail", {})
                        .get("thumbnailViewModel", {})
                    )
                    for ov in t_vm.get("overlays", []):
                        tb = ov.get(
                            "thumbnailOverlayTimeStatusRenderer", {}
                        ).get("text", {})
                        if tb.get("simpleText"):
                            v_dur = tb.get("simpleText")
                            break
                        elif tb.get("runs"):
                            v_dur = tb["runs"][0].get("text", "")
                            break
                        for b_item in ov.get(
                            "thumbnailBottomOverlayViewModel", {}
                        ).get("badges", []):
                            txt = b_item.get(
                                "thumbnailBadgeViewModel", {}
                            ).get("text")
                            if txt:
                                v_dur = str(txt)
                                break
                        if v_dur:
                            break
                        txt = ov.get(
                            "thumbnailOverlayTimeStatusViewModel", {}
                        ).get("text")
                        if txt:
                            v_dur = str(txt)
                            break
                except Exception:
                    pass
                v_dur = clean_yt_text(v_dur)

                thumb_url = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
                disp_title = (
                    v_title if len(v_title) <= 120 else v_title[:117] + "..."
                )
                info_str = f"{v_channel} • Playlist" if v_channel else "Playlist"
                disp_info = (
                    info_str if len(info_str) <= 34 else info_str[:32] + "..."
                )

                found_list.append({
                    "id": vid,
                    "title": v_title,
                    "disp_title": disp_title,
                    "channel": v_channel,
                    "disp_info": disp_info,
                    "duration": v_dur,
                    "thumb": thumb_url,
                    "pub": "Playlist",
                    "age": 0.0,
                })

        # 2. playlistVideoRenderer / videoRenderer / gridVideoRenderer
        for r_key in (
            "playlistVideoRenderer",
            "videoRenderer",
            "gridVideoRenderer",
        ):
            if r_key in node:
                vr = node[r_key]
                vid = vr.get("videoId")
                if vid and not any(it["id"] == vid for it in found_list):
                    t_runs = vr.get("title", {}).get("runs", [])
                    v_title = (
                        t_runs[0].get("text", "")
                        if t_runs
                        else vr.get("title", {}).get("simpleText", "")
                    )
                    v_title = clean_yt_text(v_title) or f"Video {vid}"

                    o_runs = vr.get("shortBylineText", {}).get("runs", []) or vr.get(
                        "ownerText", {}
                    ).get("runs", [])
                    v_channel = clean_yt_text(
                        o_runs[0].get("text", "") if o_runs else ""
                    )

                    v_dur = clean_yt_text(
                        vr.get("lengthText", {}).get("simpleText", "")
                    )
                    if not v_dur:
                        sec = vr.get("lengthSeconds")
                        if sec:
                            try:
                                s = int(sec)
                                v_dur = f"{s//60}:{s%60:02d}"
                            except Exception:
                                pass

                    thumb_url = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
                    disp_title = (
                        v_title
                        if len(v_title) <= 120
                        else v_title[:117] + "..."
                    )
                    info_str = (
                        f"{v_channel} • Playlist" if v_channel else "Playlist"
                    )
                    disp_info = (
                        info_str
                        if len(info_str) <= 34
                        else info_str[:32] + "..."
                    )

                    found_list.append({
                        "id": vid,
                        "title": v_title,
                        "disp_title": disp_title,
                        "channel": v_channel,
                        "disp_info": disp_info,
                        "duration": v_dur,
                        "thumb": thumb_url,
                        "pub": "Playlist",
                        "age": 0.0,
                    })

        for val in node.values():
            _extract_playlist_videos_from_json(val, found_list, limit)
            if len(found_list) >= limit:
                break

    elif isinstance(node, list):
        for item in node:
            _extract_playlist_videos_from_json(item, found_list, limit)
            if len(found_list) >= limit:
                break


def fetch_playlist_info_and_videos(playlist_id_or_url: str, limit: int = 150) -> dict:
    """Fetch all videos and metadata from a YouTube Playlist using InnerTube with HTML scraping fallback."""
    pid = extract_playlist_id(playlist_id_or_url)
    if not pid:
        return {"ok": False, "error": "ID hoặc Link Playlist không hợp lệ"}

    browse_id = f"VL{pid}" if not pid.startswith("VL") else pid
    payload = {
        "context": WEB_CONTEXT,
        "browseId": browse_id,
    }

    data = None
    try:
        data = _make_request("browse", payload, timeout=10)
    except Exception as e:
        print(f"[rh.yt] InnerTube browse error for playlist {pid}: {e}")

    videos = []
    title = ""

    if data:
        header = data.get("header", {})
        if "pageHeaderRenderer" in header:
            phr = header["pageHeaderRenderer"]
            title = phr.get("pageTitle", "")
            if not title:
                title = (
                    phr.get("content", {})
                    .get("pageHeaderViewModel", {})
                    .get("title", {})
                    .get("dynamicTextViewModel", {})
                    .get("text", {})
                    .get("content", "")
                )
        if not title and "playlistHeaderRenderer" in header:
            plhr = header["playlistHeaderRenderer"]
            runs = plhr.get("title", {}).get("runs", [])
            title = (
                runs[0].get("text", "")
                if runs
                else plhr.get("title", {}).get("simpleText", "")
            )
        if not title:
            meta = data.get("metadata", {}).get("playlistMetadataRenderer", {})
            title = meta.get("title", "")

        _extract_playlist_videos_from_json(data, videos, limit=limit)

        cont_token = _find_continuation_token(data)
        while cont_token and len(videos) < limit:
            try:
                cont_payload = {"context": WEB_CONTEXT, "continuation": cont_token}
                cont_data = _make_request("browse", cont_payload, timeout=8)
                _extract_playlist_videos_from_json(cont_data, videos, limit=limit)
                cont_token = _find_continuation_token(cont_data)
            except Exception:
                break

    if not videos:
        try:
            pl_url = f"https://www.youtube.com/playlist?list={pid}"
            req = urllib.request.Request(pl_url, headers={"User-Agent": USER_AGENT})
            ctx = _get_ssl_context()
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                html_text = resp.read().decode("utf-8", errors="ignore")

            m = re.search(r"var\s+ytInitialData\s*=\s*({.+?});</script>", html_text)
            if not m:
                m = re.search(r"ytInitialData\s*=\s*({.+?});", html_text)
            if m:
                js_data = json.loads(m.group(1))
                if not title:
                    title = (
                        js_data.get("metadata", {})
                        .get("playlistMetadataRenderer", {})
                        .get("title", "")
                    )
                _extract_playlist_videos_from_json(js_data, videos, limit=limit)
        except Exception as e:
            print(f"[rh.yt] HTML scrape fallback error for playlist {pid}: {e}")

    title = clean_yt_text(title)
    if not title:
        title = f"Playlist {pid}"

    if not videos:
        return {
            "ok": False,
            "error": f"Không tìm thấy video trong Playlist {pid} (Có thể playlist là riêng tư hoặc không tồn tại).",
        }

    return {
        "ok": True,
        "pid": pid,
        "title": title,
        "count": len(videos),
        "videos": videos,
    }



def fetch_thumbnail(url: str, cache_dir: str, video_id: str) -> str:
    """Download standard YouTube 16:9 JPEG thumbnail and return local cached path."""
    if not video_id:
        return ""

    os.makedirs(cache_dir, exist_ok=True)
    target_path = os.path.join(cache_dir, f"{video_id}.jpg")

    # Fast cache hit check (avoid file open / re-read overhead on every frame)
    if os.path.exists(target_path) and os.path.getsize(target_path) > 500:
        return target_path

    # Standard YouTube 16:9 JPEG thumbnail (mqdefault: 320x180, ~10-15KB)
    std_url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    dl_urls = [std_url]
    if url and url != std_url:
        dl_urls.append(url)

    ctx = _get_ssl_context()
    for u in dl_urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=4, context=ctx) as resp:
                data = resp.read()
                # Verify JPEG header (FF D8) before saving
                if len(data) > 500 and data[:2] == b"\xff\xd8":
                    with open(target_path, "wb") as f:
                        f.write(data)
                    return target_path
        except Exception:
            continue

    return ""


YT_VIDEO_CACHE_DIR = "/tmp/yt_cache"


def resolve_ytdlp():
    """Import yt_dlp, preferring the RAM-unzipped copy used by the player.

    rh.yt_player already unzips bin/yt-dlp into /tmp/ytdlp_cache (tmpfs) so the
    module imports without touching the zip; reusing that avoids importing from
    a 3 MB zip on every call.
    """
    try:
        from .yt_player import ensure_ytdlp_ready
        ensure_ytdlp_ready()
    except Exception:
        pass
    try:
        import yt_dlp
        return yt_dlp
    except ImportError:
        pass

    sdcard = os.environ.get("SDCARD_PATH", "/mnt/SDCARD")
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(app_dir, "bin", "yt-dlp"),
        os.path.join(sdcard, "Apps", "RetroHub", "bin", "yt-dlp"),
        os.path.join(sdcard, ".retrohub", "bin", "yt-dlp"),
    ]
    for c in candidates:
        if os.path.exists(c) and c not in sys.path:
            sys.path.insert(0, c)
    try:
        import yt_dlp
        return yt_dlp
    except ImportError:
        return None


def extract_stream_url(video_id: str) -> tuple:
    """Uses yt-dlp to resolve direct streaming URL (format 18 / 360p progressive)."""
    yt_dlp = resolve_ytdlp()
    if not yt_dlp:
        return None, None

    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "format": "18/best[height<=720][ext=mp4]/best[ext=mp4]/b/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios"]
            }
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(yt_url, download=False)
        return info.get("url"), info.get("title", video_id)


def resolve_audio_stream(video_id: str) -> tuple:
    """Resolve an audio-only stream (m4a/webm) for audio-only playback."""
    yt_dlp = resolve_ytdlp()
    if not yt_dlp:
        return None, None

    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    base_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
    }
    # The default client exposes audio-only formats; the mobile clients are a
    # fallback for videos where the default client is challenged.
    for ext_args in (None, {"youtube": {"player_client": ["android", "ios"]}}):
        opts = dict(base_opts)
        if ext_args:
            opts["extractor_args"] = ext_args
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(yt_url, download=False)
            url = info.get("url")
            if not url and info.get("requested_formats"):
                url = info["requested_formats"][0].get("url")
            if url:
                return url, info.get("title", video_id)
        except Exception as e:
            print(f"[rh.yt] Audio stream error for {video_id}: {e}")
    return None, None


QUALITY_HEIGHTS = {"360": 360, "480": 480, "720": 720}

def ytdlp_zip_path() -> str:
    """Path of the bundled yt-dlp package (a zip the app imports directly)."""
    sdcard = os.environ.get("SDCARD_PATH", "/mnt/SDCARD")
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for c in (os.path.join(app_dir, "bin", "yt-dlp"),
              os.path.join(sdcard, "Apps", "RetroHub", "bin", "yt-dlp"),
              os.path.join(sdcard, ".retrohub", "bin", "yt-dlp")):
        if os.path.exists(c):
            return c
    return ""


def resolve_info_json(video_id: str, quality: str = "360", path: str = None) -> str:
    """Dump yt-dlp's metadata for a video so a section can be fetched later.

    yt-dlp needs the format/fragment metadata to download a *time range*; loading
    this dump (`--load-info-json`) skips a second extraction on every seek, which
    is what makes section-based seeking quick. Returns the file path or "".
    """
    yt_dlp = resolve_ytdlp()
    if not yt_dlp or not video_id:
        return ""
    # Kept on the card (not tmpfs) so resuming the same video later does not have
    # to run the extractor again. The URLs inside expire, so re-dump after an
    # hour; the fragment metadata itself does not change.
    path = path or os.path.join(SDCARD_PATH, ".retrohub", "cache",
                                "yt_info_%s.json" % video_id)
    try:
        if os.path.exists(path) and os.path.getsize(path) > 1024:
            if time.time() - os.path.getmtime(path) < 3600:
                return path
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    h = QUALITY_HEIGHTS.get(str(quality), 360)
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "skip_download": True,
        "check_formats": False,
        "format": (f"bestvideo[height<={h}][vcodec^=avc1]+"
                   f"bestaudio[acodec^=mp4a]/bestvideo[height<={h}]+bestaudio/"
                   f"best[height<={h}]/18/best"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(yt_url, download=False)
            data = ydl.sanitize_info(info)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        print(f"[rh.yt] info dump: {path} ({os.path.getsize(path)} bytes)")
        return path
    except Exception as e:
        print(f"[rh.yt] info dump error for {video_id}: {e}")
        return ""


# Stream-URL cache for the in-app player. A googlevideo URL stays valid for
# hours and resolving one costs seconds of yt-dlp work, so results are cached in
# RAM (tmpfs) per video+quality. The RetroArch handoff path has its own cache in
# rh.yt_player; this one covers resolve_streams().
_STREAM_CACHE_FILE = "/tmp/yt_streams_inapp.json"
_STREAM_CACHE_TTL = 3 * 3600
_STREAM_CACHE = {}
_STREAM_CACHE_LOADED = False


def _load_stream_cache():
    global _STREAM_CACHE, _STREAM_CACHE_LOADED
    _STREAM_CACHE_LOADED = True
    try:
        if os.path.exists(_STREAM_CACHE_FILE):
            with open(_STREAM_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                now = time.time()
                _STREAM_CACHE = {k: v for k, v in data.items()
                                 if isinstance(v, dict) and v.get("exp", 0) > now}
    except Exception:
        _STREAM_CACHE = {}


def _save_stream_cache():
    try:
        now = time.time()
        fresh = {k: v for k, v in _STREAM_CACHE.items() if v.get("exp", 0) > now}
        with open(_STREAM_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(fresh, f)
    except Exception:
        pass


def get_cached_streams(video_id: str, quality: str = "360"):
    """Cached resolve_streams() result for the in-app player, or None."""
    if not video_id:
        return None
    global _STREAM_CACHE
    if not _STREAM_CACHE_LOADED:
        _load_stream_cache()
    key = "%s|%s" % (video_id, quality)
    entry = _STREAM_CACHE.get(key)
    if entry and entry.get("exp", 0) > time.time() and entry.get("video_url"):
        res = dict(entry)
        res.pop("exp", None)
        return res
    return None


def cache_streams(video_id: str, quality: str, res: dict):
    """Store a resolve_streams() result so the next playback skips yt-dlp."""
    if not video_id or not res or not res.get("video_url"):
        return
    entry = dict(res)
    entry["exp"] = time.time() + _STREAM_CACHE_TTL
    _STREAM_CACHE["%s|%s" % (video_id, quality)] = entry
    _save_stream_cache()


def clear_cached_streams(video_id: str, quality: str = None):
    """Drop cached resolution(s) for a video (an expired URL must be re-resolved)."""
    if not video_id:
        return
    if quality is None:
        keys = [k for k in _STREAM_CACHE if k.startswith(video_id + "|")]
    else:
        keys = ["%s|%s" % (video_id, quality)]
    for k in keys:
        _STREAM_CACHE.pop(k, None)
    if keys:
        _save_stream_cache()


def resolve_streams(video_id: str, quality: str = "360") -> dict:
    """Resolve video + audio stream URLs for the in-app player.

    Returns {video_url, audio_url, title, height, progressive} or None. A
    progressive format carries both tracks (same URL for both); a DASH format
    returns separate video/audio URLs, which the two-process player handles.
    """
    cached = get_cached_streams(video_id, quality)
    if cached:
        print(f"[rh.yt] stream cache hit for {video_id} ({quality}p)")
        return cached

    yt_dlp = resolve_ytdlp()
    if not yt_dlp:
        return None

    h = QUALITY_HEIGHTS.get(str(quality), 360)
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    # Prefer H.264 + AAC: the device ffmpeg/player may lack AV1/VP9/Opus.
    # Progressive format 18 carries both tracks in one request, so it is the
    # fastest and most compatible target at 360p; higher qualities need the
    # separate DASH video/audio pair.
    if h <= 360:
        format_list = [
            "18",
            f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio[acodec^=mp4a]",
            f"best[height<={h}][ext=mp4]",
        ]
    else:
        format_list = [
            f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio[acodec^=mp4a]",
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]",
            f"best[height<={h}][ext=mp4]",
            "18/best[ext=mp4]/best",
        ]
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "skip_download": True,
        "check_formats": False,
    }

    # The default client is the only one that exposes separate DASH video/audio
    # formats (needed for 480p/720p); forcing android/ios makes the DASH
    # selector fail. Try default first, then the mobile clients as a fallback.
    client_configs = (None, {"youtube": {"player_client": ["android", "ios"]}})

    info = None
    for ext_args in client_configs:
        for fmt in format_list:
            opts = dict(base_opts)
            opts["format"] = fmt
            if ext_args:
                opts["extractor_args"] = ext_args
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    candidate = ydl.extract_info(yt_url, download=False)
                if candidate and (candidate.get("url") or candidate.get("requested_formats")):
                    info = candidate
                    break
            except Exception as e:
                msg = str(e)
                print(f"[rh.yt] resolve_streams fmt '{fmt}' failed: {msg}")
                # No other format/parser can help with a dead or private video.
                if any(t in msg for t in ("Video unavailable", "Private video",
                                          "members-only", "has been removed",
                                          "This video is not available")):
                    return None
        if info:
            break

    if not info:
        return None

    video_url = audio_url = None
    progressive = True
    requested = info.get("requested_formats")
    if requested:
        progressive = False
        for f in requested:
            if f.get("vcodec") not in (None, "none") and not video_url:
                video_url = f.get("url")
            elif f.get("acodec") not in (None, "none") and not audio_url:
                audio_url = f.get("url")
    if not video_url:
        video_url = info.get("url")
        audio_url = info.get("url")
    if not audio_url:
        audio_url = video_url
    if not video_url:
        return None

    res = {
        "video_url": video_url,
        "audio_url": audio_url,
        "title": info.get("title", video_id),
        "height": info.get("height") or h,
        "progressive": progressive,
    }
    cache_streams(video_id, quality, res)
    return res


def get_cached_video_path(video_id: str) -> str:
    """Return local path if video is already downloaded and valid, else empty string."""
    target = os.path.join(YT_VIDEO_CACHE_DIR, f"{video_id}.mp4")
    if os.path.exists(target) and os.path.getsize(target) > 500 * 1024:
        return target
    return ""


def cleanup_cache(max_mb: int = 250):
    """Keep the /tmp/yt_cache directory within reasonable size limits."""
    try:
        if not os.path.exists(YT_VIDEO_CACHE_DIR):
            return
        files = []
        total_bytes = 0
        for fn in os.listdir(YT_VIDEO_CACHE_DIR):
            if fn.endswith(".mp4"):
                fp = os.path.join(YT_VIDEO_CACHE_DIR, fn)
                sz = os.path.getsize(fp)
                mt = os.path.getmtime(fp)
                files.append((mt, sz, fp))
                total_bytes += sz

        max_bytes = max_mb * 1024 * 1024
        if total_bytes > max_bytes:
            # Sort oldest modified first
            files.sort(key=lambda x: x[0])
            for _, sz, fp in files:
                if total_bytes <= max_bytes * 0.6:
                    break
                try:
                    os.remove(fp)
                    total_bytes -= sz
                except Exception:
                    pass
    except Exception:
        pass


def download_video_stream(video_id: str, progress_cb=None, cancel_fn=None) -> tuple:
    """Download video to local cache with progress reporting.

    Args:
        video_id: YouTube video ID.
        progress_cb: fn(pct, cur_mb, tot_mb, speed_mb)
        cancel_fn: fn() -> bool, returns True if user pressed Cancel.

    Returns:
        (file_path, title, err_msg)
    """
    os.makedirs(YT_VIDEO_CACHE_DIR, exist_ok=True)
    dest_path = os.path.join(YT_VIDEO_CACHE_DIR, f"{video_id}.mp4")
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 500 * 1024:
        return dest_path, "", None

    cleanup_cache(max_mb=250)

    try:
        stream_url, title = extract_stream_url(video_id)
    except Exception as e:
        return None, "", f"Lỗi lấy link: {e}"

    if not stream_url:
        return None, "", "Không lấy được link luồng video"

    if cancel_fn and cancel_fn():
        return None, title, "Đã hủy"

    part_path = dest_path + ".part"
    try:
        ctx = _get_ssl_context()
        req = urllib.request.Request(stream_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp, open(part_path, "wb") as f:
            total_len = resp.headers.get("Content-Length")
            total_bytes = int(total_len) if total_len and total_len.isdigit() else 0
            downloaded = 0
            chunk_size = 256 * 1024
            start_time = time.time()

            while True:
                if cancel_fn and cancel_fn():
                    f.close()
                    if os.path.exists(part_path):
                        os.remove(part_path)
                    return None, title, "Đã hủy tải"

                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)

                if progress_cb:
                    elapsed = time.time() - start_time
                    speed_mb = (downloaded / elapsed) / (1024 * 1024) if elapsed > 0 else 0
                    pct = int(downloaded * 100 / total_bytes) if total_bytes > 0 else 0
                    cur_mb = downloaded / (1024 * 1024)
                    tot_mb = total_bytes / (1024 * 1024) if total_bytes > 0 else 0
                    progress_cb(pct, cur_mb, tot_mb, speed_mb)

        if os.path.exists(dest_path):
            os.remove(dest_path)
        os.rename(part_path, dest_path)
        return dest_path, title, None
    except Exception as e:
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
        return None, title, f"Lỗi tải: {e}"


def build_play_command(video_id: str, info_file: str = "/tmp/yt_stream_info.json") -> str:
    """Build the shell command string to execute in handoff script /tmp/launch_game.sh.

    Streams YouTube video immediately using rh.yt_player with RetroArch FFMPEG core.
    If info_file exists, passes pre-extracted stream URL to bypass yt-dlp extraction entirely.
    """
    info_arg = f'--info-file "{info_file}"' if info_file else ""
    cmd = f"""#!/bin/sh
SDCARD_PATH="${{SDCARD_PATH:-/mnt/SDCARD}}"
APP_DIR="$SDCARD_PATH/Apps/RetroHub"
LOG_FILE="$SDCARD_PATH/RetroHub-yt.log"

echo "=== YouTube Streaming: {video_id} ($(date 2>/dev/null)) ===" > "$LOG_FILE"

# Ensure System/lib is in LD_LIBRARY_PATH for OpenSSL 1.1.1 and SDL2
export LD_LIBRARY_PATH="/mnt/SDCARD/System/lib:/usr/trimui/lib:$LD_LIBRARY_PATH"

PY3="python3"
if [ -f "$APP_DIR/python/bin/python3" ]; then
    PY3="$APP_DIR/python/bin/python3"
elif [ -f "$SDCARD_PATH/System/bin/python3" ]; then
    PY3="$SDCARD_PATH/System/bin/python3"
elif [ -f "$SDCARD_PATH/.retrohub/python/bin/python3" ]; then
    PY3="$SDCARD_PATH/.retrohub/python/bin/python3"
elif which python3 >/dev/null 2>&1; then
    PY3="python3"
fi

cd "$APP_DIR"
"$PY3" -m rh.yt_player "{video_id}" {info_arg} >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

echo "Streaming Exit Code: $EXIT_CODE" >> "$LOG_FILE"
"""
    return cmd

