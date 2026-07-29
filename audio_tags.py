"""Audio tag reading/writing and metadata helpers (mutagen-based), plus
album-art extraction/placeholder image helpers (Pillow-based)."""

import io
import os

from mutagen import File as MutagenFile
from PIL import Image, ImageTk

from constants import COMMON_TAG_FIELDS


def format_duration(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def read_track_tags(path):
    """Return (artist, title, album, duration_str) for the playlist columns.
    Falls back to the filename as title if tags are unavailable."""
    artist = title = album = ""
    duration = ""
    try:
        audio = MutagenFile(path, easy=True)
        if audio is not None:
            artist = (audio.tags.get("artist", [""])[0] if audio.tags else "")
            title = (audio.tags.get("title", [""])[0] if audio.tags else "")
            album = (audio.tags.get("album", [""])[0] if audio.tags else "")
            if getattr(audio, "info", None) is not None:
                duration = format_duration(getattr(audio.info, "length", None))
    except Exception:
        pass
    if not title:
        title = os.path.basename(path)
    return artist, title, album, duration


def get_track_duration(path):
    try:
        audio = MutagenFile(path)
    except Exception:
        return 0
    if audio is None or getattr(audio, "info", None) is None:
        return 0
    return getattr(audio.info, "length", 0) or 0


def get_embedded_art_bytes(path):
    """Best-effort extraction of embedded cover art bytes (FLAC/OGG pictures,
    ID3 APIC frames, MP4 covr atoms). Returns None if no art is found."""
    try:
        audio = MutagenFile(path)
    except Exception:
        return None
    if audio is None:
        return None

    pictures = getattr(audio, "pictures", None)
    if pictures:
        return pictures[0].data

    tags = getattr(audio, "tags", None)
    if not tags:
        return None

    try:
        for key in tags.keys():
            if str(key).startswith("APIC"):
                return tags[key].data
    except Exception:
        pass

    try:
        covers = tags.get("covr")
        if covers:
            return bytes(covers[0])
    except Exception:
        pass

    return None


def make_placeholder_art(size=(72, 72)):
    image = Image.new("RGB", size, color="#3a3a3a")
    return ImageTk.PhotoImage(image)


def get_track_art_image(path, size=(72, 72)):
    data = get_embedded_art_bytes(path)
    if data is None:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image = image.convert("RGB")
        image.thumbnail(size)
        return ImageTk.PhotoImage(image)
    except Exception:
        return None


def read_full_metadata(path):
    """Return a list of (label, value) pairs describing the file's tags and
    technical audio properties, for display in a Properties dialog."""
    rows = [("File", path)]
    try:
        audio = MutagenFile(path)
    except Exception as exc:
        rows.append(("Error", str(exc)))
        return rows

    if audio is None:
        rows.append(("Error", "Unsupported or unrecognized audio format"))
        return rows

    info = getattr(audio, "info", None)
    if info is not None:
        length = getattr(info, "length", None)
        if length:
            rows.append(("Duration", format_duration(length)))
        bitrate = getattr(info, "bitrate", None)
        if bitrate:
            rows.append(("Bitrate", f"{bitrate // 1000} kbps"))
        sample_rate = getattr(info, "sample_rate", None)
        if sample_rate:
            rows.append(("Sample rate", f"{sample_rate} Hz"))
        channels = getattr(info, "channels", None)
        if channels:
            rows.append(("Channels", str(channels)))
        bits = getattr(info, "bits_per_sample", None)
        if bits:
            rows.append(("Bits per sample", str(bits)))

    if audio.tags:
        for key in sorted(audio.tags.keys()):
            try:
                value = audio.tags[key]
            except Exception:
                continue
            if isinstance(value, list):
                value = "; ".join(str(v) for v in value)
            rows.append((str(key), str(value)))

    return rows


def read_common_tags(path):
    """Return a dict of the common editable tag fields for `path`, using
    mutagen's "easy" tag interface. Missing fields are empty strings."""
    result = {key: "" for key, _label in COMMON_TAG_FIELDS}
    try:
        audio = MutagenFile(path, easy=True)
        if audio is not None and audio.tags:
            for key, _label in COMMON_TAG_FIELDS:
                values = audio.tags.get(key)
                if values:
                    result[key] = values[0]
    except Exception:
        pass
    return result


def apply_common_tags(path, updates, clear_blank=False):
    """Write `updates` (dict of tag-key -> new value) to `path` using
    mutagen's easy tag interface. If `clear_blank` is True, an empty value
    removes that tag; otherwise empty values are skipped (left unchanged),
    which is used for bulk edits where a blank field means "don't touch".
    Returns True if the file was modified.
    """
    audio = MutagenFile(path, easy=True)
    if audio is None:
        raise ValueError("Unsupported or unrecognized audio format")
    if audio.tags is None:
        audio.add_tags()

    changed = False
    for key, value in updates.items():
        value = (value or "").strip()
        if value:
            if audio.tags.get(key) != [value]:
                audio.tags[key] = [value]
                changed = True
        elif clear_blank and key in audio.tags:
            del audio.tags[key]
            changed = True

    if changed:
        audio.save()
    return changed
