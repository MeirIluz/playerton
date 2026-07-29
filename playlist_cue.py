"""Cue-sheet-backed playlists.

Real CUE sheets describe how tracks/indexes lay out across one or more
audio "FILE" statements -- normally used to describe an album that's one
continuous audio blob (e.g. a CD rip's .bin/.wav + track offsets). This
module reuses that same format to represent an ordinary playlist of
independent files instead: one FILE statement per track, each with a
single TRACK/INDEX 01 00:00:00 (its own file, so playback always starts
at the top of it). This keeps playlists in a widely-recognized format
(most cue-sheet-aware software can at least read the track file names and
titles back out of it) while still being simple enough to hand-roll a
reader/writer for, without pulling in a full third-party cue-parsing
library.

File layout written by `write_cue_playlist`:

    REM PLAYLIST_NAME "My Playlist"
    FILE "/absolute/path/to/song1.mp3" MP3
      TRACK 01 AUDIO
        TITLE "Song Title"
        PERFORMER "Artist Name"
        INDEX 01 00:00:00
    FILE "/absolute/path/to/song2.flac" WAVE
      TRACK 01 AUDIO
        TITLE "Another Song"
        INDEX 01 00:00:00

Paths are stored ABSOLUTE (unlike a typical single-album cue sheet,
which uses paths relative to the cue file, since all its audio normally
lives right next to it) since a playlist's tracks can live anywhere on
disk. `read_cue_playlist` still resolves any relative FILE path it
encounters (e.g. from a hand-edited or foreign cue file) against the cue
file's own directory, for compatibility.
"""

import os
import re

from state_cache import CACHE_DIR

# Where playlists created/imported by the app are stored by default.
PLAYLISTS_DIR = os.path.join(CACHE_DIR, "playlists")

_FILE_RE = re.compile(r'^FILE\s+"(.*)"\s+\S+\s*$')
_REM_NAME_RE = re.compile(r'^REM\s+PLAYLIST_NAME\s+"(.*)"\s*$')

_FILE_TYPE_BY_EXT = {
    ".mp3": "MP3",
    ".wav": "WAVE",
    ".aiff": "AIFF",
    ".flac": "FLAC",
    ".aac": "AAC",
    ".m4a": "AAC",
    ".ogg": "OGG",
    ".opus": "OGG",
    ".wma": "WMA",
    ".ape": "APE",
    ".alac": "AAC",
}


def _file_type_for(path):
    return _FILE_TYPE_BY_EXT.get(os.path.splitext(path)[1].lower(), "WAVE")


def _escape(value):
    # Cue sheets have no real escaping mechanism for embedded quotes;
    # substituting a plain apostrophe is the common workaround.
    return (value or "").replace('"', "'")


def write_cue_playlist(cue_path, name, track_paths, track_tags=None):
    """Write `track_paths` (a list of absolute file paths, in order) to
    `cue_path` as a cue sheet. `name` is recorded in a leading
    `REM PLAYLIST_NAME "..."` comment so the display name survives even
    if the file is renamed on disk. `track_tags` is an optional
    {path: {"title", "artist", ...}} dict (the same shape App keeps in
    `self.track_tags`) used to fill in TITLE/PERFORMER for readability in
    other cue-aware software -- purely cosmetic, `read_cue_playlist` does
    not read them back (the app always re-reads real audio tags itself)."""
    track_tags = track_tags or {}
    lines = [f'REM PLAYLIST_NAME "{_escape(name)}"']
    for path in track_paths:
        tags = track_tags.get(path, {})
        title = tags.get("title") or os.path.basename(path)
        artist = tags.get("artist", "")
        lines.append(f'FILE "{path}" {_file_type_for(path)}')
        lines.append("  TRACK 01 AUDIO")
        lines.append(f'    TITLE "{_escape(title)}"')
        if artist:
            lines.append(f'    PERFORMER "{_escape(artist)}"')
        lines.append("    INDEX 01 00:00:00")

    os.makedirs(os.path.dirname(cue_path) or ".", exist_ok=True)
    tmp_path = cue_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp_path, cue_path)


def read_cue_playlist(cue_path):
    """Parse `cue_path`, returning (name, [track_paths]). `name` falls
    back to the cue file's basename (without extension) if no
    `REM PLAYLIST_NAME` comment is present (e.g. a cue sheet written by
    other software). Raises OSError if the file can't be read."""
    name = os.path.splitext(os.path.basename(cue_path))[0]
    track_paths = []
    base_dir = os.path.dirname(cue_path)

    with open(cue_path, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip()

            name_match = _REM_NAME_RE.match(line)
            if name_match:
                name = name_match.group(1)
                continue

            file_match = _FILE_RE.match(line)
            if file_match:
                path = file_match.group(1)
                if not os.path.isabs(path):
                    path = os.path.join(base_dir, path)
                track_paths.append(os.path.normpath(path))

    return name, track_paths


def unique_cue_path(name, directory=PLAYLISTS_DIR):
    """A cue file path under `directory` for playlist `name` that doesn't
    already exist, appending " (2)", " (3)", ... to the name if needed --
    mirrors App._unique_destination's approach for archive imports."""
    safe_name = "".join(
        c for c in name if c not in '<>:"/\\|?*').strip() or "Playlist"
    candidate = os.path.join(directory, f"{safe_name}.cue")
    counter = 1
    while os.path.exists(candidate):
        counter += 1
        candidate = os.path.join(directory, f"{safe_name} ({counter}).cue")
    return candidate
