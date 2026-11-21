"""Album info links for the "Album Info" window.

Two kinds of links:
  * Search links (search_links): ready-made searches for the album on
    RateYourMusic, Discogs, MusicBrainz, AllMusic, Last.fm, Bandcamp and
    Wikipedia. Built locally -- nothing is sent anywhere until a link is
    clicked (then the browser opens that site's search page).
  * Direct links (find_musicbrainz_links): the album is looked up on
    MusicBrainz (https://musicbrainz.org, a free open music database) and
    the links its editors attached to it -- Discogs, Wikipedia, AllMusic,
    RateYourMusic, Bandcamp, official pages, ... -- are returned. Only the
    artist and album name are sent, and only when the window is opened.
    Results are cached in CACHE_DIR/album_info (misses retried after a week).

Plain Python, no Tk: the MusicBrainz lookup runs on a background thread.
"""

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request

from state_cache import CACHE_DIR

ALBUM_INFO_CACHE_DIR = os.path.join(CACHE_DIR, "album_info")
MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
# MusicBrainz asks for an identifying User-Agent; deliberately generic (no
# personal details).
USER_AGENT = "Playerton/1.0 (desktop music player)"
MISS_RETRY_SECONDS = 7 * 24 * 3600
MIN_MATCH_SCORE = 85  # MusicBrainz search score (0-100) to accept a match

# Friendly names for the sites MusicBrainz links to, by domain.
_SITE_NAMES = {
    "discogs.com": "Discogs",
    "rateyourmusic.com": "RateYourMusic",
    "allmusic.com": "AllMusic",
    "wikipedia.org": "Wikipedia",
    "wikidata.org": "Wikidata",
    "bandcamp.com": "Bandcamp",
    "last.fm": "Last.fm",
    "genius.com": "Genius",
    "metal-archives.com": "Metal Archives",
    "spotify.com": "Spotify",
    "music.apple.com": "Apple Music",
    "deezer.com": "Deezer",
    "tidal.com": "Tidal",
    "youtube.com": "YouTube",
    "music.youtube.com": "YouTube Music",
    "soundcloud.com": "SoundCloud",
    "albumoftheyear.org": "Album of the Year",
    "vgmdb.net": "VGMdb",
    "secondhandsongs.com": "SecondHandSongs",
    "setlist.fm": "setlist.fm",
}


def search_links(artist, album):
    """(site, url) search links for the album on the usual music sites."""
    query = " ".join(part for part in (artist, album) if part).strip()
    q = urllib.parse.quote_plus(query)
    links = [
        ("RateYourMusic", f"https://rateyourmusic.com/search?searchterm={q}&searchtype=l"),
        ("Discogs", f"https://www.discogs.com/search/?q={q}&type=master"),
        ("MusicBrainz", "https://musicbrainz.org/search?" + urllib.parse.urlencode(
            {"query": query, "type": "release_group", "method": "indexed"})),
        ("AllMusic", f"https://www.allmusic.com/search/albums/{urllib.parse.quote(query)}"),
        ("Bandcamp", f"https://bandcamp.com/search?q={q}&item_type=a"),
        ("Wikipedia", "https://en.wikipedia.org/w/index.php?" + urllib.parse.urlencode(
            {"search": f"{album} {artist} album".strip()})),
    ]
    if artist and album:
        # Last.fm has a predictable album page URL.
        links.insert(4, ("Last.fm", "https://www.last.fm/music/{}/{}".format(
            urllib.parse.quote_plus(artist), urllib.parse.quote_plus(album))))
    return links


def site_name(url):
    """Friendly site name for a URL (by domain), e.g. "Discogs"."""
    host = urllib.parse.urlparse(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    for domain, name in _SITE_NAMES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or url


# -- MusicBrainz ---------------------------------------------------------------
def _get_json(url):
    """GET a MusicBrainz API URL and decode the JSON reply."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _lucene_quote(value):
    """Quote a value for a MusicBrainz (Lucene) search query."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _cache_file(artist, album):
    """Cache file for one album's MusicBrainz result."""
    key = f"{artist.lower()}|{album.lower()}"
    return os.path.join(ALBUM_INFO_CACHE_DIR,
                        hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def find_musicbrainz_links(artist, album):
    """Look the album up on MusicBrainz (cached). Returns a dict with the
    matched "title", "artist", "year", "musicbrainz" page URL and "links"
    [(site, url), ...], or None if there's no confident match. Raises on
    network errors."""
    if not album:
        return None
    cache_file = _cache_file(artist, album)
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("result"):
            result = cached["result"]
            result["links"] = [tuple(link) for link in result["links"]]
            return result
        if time.time() - cached.get("checked", 0) < MISS_RETRY_SECONDS:
            return None
    except (OSError, ValueError):
        pass

    # 1) Find the album ("release group" = an album across all its editions).
    query = "releasegroup:" + _lucene_quote(album)
    if artist:
        query += " AND artist:" + _lucene_quote(artist)
    found = _get_json(f"{MUSICBRAINZ_API}/release-group/?" + urllib.parse.urlencode(
        {"query": query, "fmt": "json", "limit": 5}))
    groups = [g for g in found.get("release-groups", [])
              if int(g.get("score", 0)) >= MIN_MATCH_SCORE]
    result = None
    if groups:
        group = groups[0]
        # 2) Fetch the links attached to it. MusicBrainz allows one request
        #    per second, so wait out the rest of that second first.
        time.sleep(1.1)
        detail = _get_json(f"{MUSICBRAINZ_API}/release-group/{group['id']}?" +
                           urllib.parse.urlencode({"inc": "url-rels", "fmt": "json"}))
        links, seen = [], set()
        for relation in detail.get("relations", []):
            url = (relation.get("url") or {}).get("resource")
            if url and url not in seen:
                seen.add(url)
                links.append((site_name(url), url))
        links.sort(key=lambda link: link[0].lower())
        credit = "".join(
            (c.get("name") or "") + (c.get("joinphrase") or "")
            for c in group.get("artist-credit", []))
        result = {
            "title": group.get("title") or album,
            "artist": credit or artist,
            "year": (group.get("first-release-date") or "")[:4],
            "type": group.get("primary-type") or "",
            "musicbrainz": f"https://musicbrainz.org/release-group/{group['id']}",
            "links": links,
        }
    try:
        os.makedirs(ALBUM_INFO_CACHE_DIR, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"result": result, "checked": time.time()}, f)
    except OSError:
        pass
    return result
