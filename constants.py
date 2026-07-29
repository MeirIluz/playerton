"""Shared constants: supported file extensions/filetypes and common tag
field definitions used across the app.

All appearance/theming (colors, fonts, widget styles) lives in styles.py,
not here -- see that module to restyle the app or add a new color theme.
"""

MUSIC_EXTENSIONS = (
    ".mp3", ".flac", ".wav", ".aac", ".ogg", ".wma",
    ".m4a", ".aiff", ".alac", ".opus", ".ape",
)

MUSIC_FILETYPES = [
    ("Music files", tuple(f"*{ext}" for ext in MUSIC_EXTENSIONS)),
    ("Album archives", ("*.zip",)),
    ("All files", "*.*"),
]

IMAGE_FILETYPES = [
    ("Image files", ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp", "*.webp")),
    ("All files", "*.*"),
]

COMMON_TAG_FIELDS = [
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("albumartist", "Album Artist"),
    ("genre", "Genre"),
    ("date", "Year"),
    ("tracknumber", "Track #"),
    ("discnumber", "Disc #"),
    ("bpm", "BPM"),
]

MIXED_SENTINEL = "(mixed)"

# All metadata columns selectable for the playlist table (Artist/Title/...),
# via its right-click header menu. Order here is the fixed display order.
PLAYLIST_COLUMNS = [
    ("artist", "Artist"),
    ("title", "Title"),
    ("album", "Album"),
    ("duration", "Duration"),
    ("albumartist", "Album Artist"),
    ("genre", "Genre"),
    ("date", "Year"),
    ("tracknumber", "Track #"),
    ("discnumber", "Disc #"),
    ("bpm", "BPM"),
]

# Columns shown by default (matches the app's original fixed layout).
DEFAULT_PLAYLIST_COLUMNS = ["artist", "title", "album", "duration"]

PLAYLIST_COLUMN_WIDTHS = {
    "artist": 150,
    "title": 250,
    "album": 150,
    "duration": 80,
    "albumartist": 150,
    "genre": 100,
    "date": 70,
    "tracknumber": 70,
    "discnumber": 70,
    "bpm": 60,
}
