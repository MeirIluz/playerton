"""Persistent session cache: remembers library folders/playlist, chosen
background images, the last-playing track/position, and UI settings
(theme, etc.) across app restarts, stored as JSON under the user's config
directory. Saving/loading failures are silently ignored -- this is a
convenience cache, not critical state."""

import json
import os

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".config", "tiny_player")
CACHE_FILE = os.path.join(CACHE_DIR, "state.json")


def load_cache():
    """Return the saved state dict, or {} if none exists or it's corrupt."""
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(data):
    """Write `data` to the cache file, creating the config directory if
    needed. Writes to a temp file and renames into place so a crash mid-
    write can't corrupt the previous cache."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp_path = CACHE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, CACHE_FILE)
    except OSError:
        pass
