"""Helpers for detecting and parsing album .zip archive filenames."""

import os
import re


def looks_like_archive(path):
    """True for '.zip' files, including in-progress downloads that get a
    temporary suffix appended (e.g. 'Album - Artist.zip.crswap')."""
    lower = path.lower()
    return lower.endswith(".zip") or ".zip." in lower


def sanitize_filename(name):
    """Strip characters that are illegal in file/folder names on common
    filesystems."""
    name = re.sub(r'[<>:"/\\|?*]', "_", name or "").strip()
    return name or "Unknown"


def parse_album_zip_name(zip_path):
    """Best-effort parse of an '<Album> - <Artist>.zip' style filename
    (ignoring trailing pseudo-extensions like '.crswap'). Returns
    (album, artist), either of which may be None if the pattern doesn't match.
    """
    base = os.path.basename(zip_path)
    while True:
        root, ext = os.path.splitext(base)
        if ext.lower() in (".zip", ".crswap", ".crdownload", ".part", ".tmp"):
            base = root
        else:
            break
    if " - " in base:
        album, _sep, artist = base.rpartition(" - ")
        return album.strip() or None, artist.strip() or None
    return None, None
