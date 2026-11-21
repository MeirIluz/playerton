"""Finding and parsing song lyrics for the lyrics panel.

Where lyrics come from, in order (first hit wins):
  1. A sidecar .lrc file next to the track (same name, ".lrc" extension).
  2. Lyrics embedded in the file's tags: FLAC/OGG "LYRICS" /
     "UNSYNCEDLYRICS" comments, MP3 USLT frames, M4A "\\xa9lyr".
  3. Online, from LRCLIB (https://lrclib.net -- free, no account), if
     enabled in Settings. Only the song's artist, title, album and length
     are sent. Results (including "not found") are cached on disk in
     CACHE_DIR/lyrics so each song is looked up once (misses are retried
     after a week).

Lyrics in LRC format ("[01:23.45] line") are "synced": the panel highlights
the current line as the song plays. Anything else is shown as plain text.

Everything here is plain Python with no Tk, so the online lookup can run
on a background thread (see App._load_lyrics).
"""

import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

from state_cache import CACHE_DIR

LYRICS_CACHE_DIR = os.path.join(CACHE_DIR, "lyrics")
LRCLIB_URL = "https://lrclib.net/api"
# LRCLIB asks clients to identify themselves; deliberately generic (no
# personal or machine details).
USER_AGENT = "Playerton (desktop music player)"
MISS_RETRY_SECONDS = 7 * 24 * 3600
INSTRUMENTAL_TEXT = "(Instrumental)"

# One or more "[mm:ss]" / "[mm:ss.xx]" / "[mm:ss:xx]" stamps before a line.
_STAMP_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_OFFSET_RE = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)


class Lyrics:
    """Lyrics for one track: `lines` is a list of (seconds, text) when
    synced, otherwise None and `text` holds the plain lyrics. `source`
    says where they came from (shown in the panel)."""

    def __init__(self, text, source):
        """Parse `text` (LRC or plain) found at `source`."""
        self.source = source
        self.lines = parse_lrc(text)
        self.text = text.strip()

    @property
    def synced(self):
        """True if every line has a timestamp to follow along with."""
        return bool(self.lines)

    def line_index_at(self, seconds):
        """Index of the synced line being sung at `seconds` (-1 before the
        first line)."""
        index = -1
        for i, (start, _text) in enumerate(self.lines or ()):
            if start <= seconds:
                index = i
            else:
                break
        return index


def parse_lrc(text):
    """Parse LRC text into a sorted list of (seconds, line). Returns None if
    the text has no timestamps (i.e. it's plain lyrics). Handles several
    stamps on one line (repeated choruses) and the [offset:ms] tag."""
    if not text:
        return None
    offset = 0.0
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        match = _OFFSET_RE.match(raw)
        if match:
            # Positive offset = lyrics appear sooner (LRC convention).
            offset = int(match.group(1)) / 1000.0
            continue
        stamps = list(_STAMP_RE.finditer(raw))
        if not stamps or stamps[0].start() != 0:
            continue  # metadata tags like [ar:...] or untimed lines
        lyric = raw[stamps[-1].end():].strip()
        for stamp in stamps:
            minutes, seconds, fraction = stamp.groups()
            value = int(minutes) * 60 + int(seconds)
            if fraction:
                value += int(fraction) / (10 ** len(fraction))
            lines.append((max(0.0, value - offset), lyric))
    if not lines:
        return None
    lines.sort(key=lambda item: item[0])
    return lines


# -- local lyrics -------------------------------------------------------------
def find_local_lyrics(path):
    """Lyrics from a sidecar .lrc file or the file's own tags, or None."""
    sidecar = os.path.splitext(path)[0] + ".lrc"
    if os.path.isfile(sidecar):
        try:
            with open(sidecar, "r", encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
            if text.strip():
                return Lyrics(text, ".lrc file")
        except OSError:
            pass
    text = _embedded_lyrics(path)
    if text:
        return Lyrics(text, "embedded in file")
    return None


def _embedded_lyrics(path):
    """Lyrics stored in the audio file's tags (various formats), or ""."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(path)
    except Exception:
        return ""
    tags = getattr(audio, "tags", None)
    if not tags:
        return ""
    try:
        # MP3 (ID3): USLT frames, keyed like "USLT::eng".
        for key in list(tags.keys()):
            if str(key).startswith("USLT"):
                text = str(getattr(tags[key], "text", "") or "")
                if text.strip():
                    return text
        # FLAC / OGG (Vorbis comments) and M4A atoms.
        for key in ("LYRICS", "UNSYNCEDLYRICS", "lyrics", "unsyncedlyrics", "\xa9lyr"):
            if key in tags:
                value = tags[key]
                text = value[0] if isinstance(value, list) else value
                text = str(getattr(text, "text", text) or "")
                if text.strip():
                    return text
    except Exception:
        return ""
    return ""


# -- online (LRCLIB) ------------------------------------------------------------
def _cache_path(artist, title, album, duration):
    """Cache file for one song's online lookup result."""
    key = "|".join([artist.lower(), title.lower(), album.lower(), str(round(duration))])
    return os.path.join(LYRICS_CACHE_DIR, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def _http_json(url):
    """GET `url` and decode JSON; None on 404."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def fetch_online_lyrics(artist, title, album, duration):
    """Look a song up on LRCLIB (cached). Returns Lyrics or None. Raises on
    network errors (the caller shows "couldn't reach LRCLIB")."""
    if not title:
        return None
    cache_file = _cache_path(artist, title, album, duration)
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("text"):
            return Lyrics(cached["text"], "LRCLIB")
        if cached.get("instrumental"):
            return Lyrics(INSTRUMENTAL_TEXT, "LRCLIB")
        if time.time() - cached.get("checked", 0) < MISS_RETRY_SECONDS:
            return None  # recently looked up and not found
    except (OSError, ValueError):
        pass

    # Exact match first (artist + title + album + duration within ~2 s)...
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = str(round(duration))
    result = _http_json(f"{LRCLIB_URL}/get?{urllib.parse.urlencode(params)}")
    # ...then a looser search, taking the closest-length result.
    if not result:
        query = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
        candidates = _http_json(f"{LRCLIB_URL}/search?{query}") or []
        if duration:
            candidates.sort(key=lambda c: abs((c.get("duration") or 0) - duration))
        result = candidates[0] if candidates else None

    text = ""
    instrumental = bool(result and result.get("instrumental"))
    if result and not instrumental:
        text = result.get("syncedLyrics") or result.get("plainLyrics") or ""
    try:
        os.makedirs(LYRICS_CACHE_DIR, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"text": text, "instrumental": instrumental,
                       "checked": time.time()}, f)
    except OSError:
        pass
    if instrumental:
        return Lyrics(INSTRUMENTAL_TEXT, "LRCLIB")
    return Lyrics(text, "LRCLIB") if text.strip() else None
