"""Playerton - entry point. Run this file (or the packaged Playerton.exe,
which is built from it) to start the player.

The GUI + logic lives in app.py (the App class). Supporting modules:
  - styles.py         ALL appearance: color themes, fonts, and the CSS-like
                      STYLESHEET mapping every widget style to theme colors
  - constants.py      shared constants (file types, tag fields, columns)
  - audio_engine.py   audio output (AudioOutput): pygame normally, or the
                      miniaudio + numpy 10-band equalizer engine
  - audio_tags.py     tag reading/writing + album art extraction (mutagen)
  - image_utils.py    background-photo fitting/fading, and generating a
                      color palette from an image
  - lyrics.py         finding lyrics (.lrc file, embedded tags, LRCLIB
                      online lookup) and parsing synced LRC lyrics
  - album_info.py     "Album Info" links: site searches + MusicBrainz lookup
  - media_keys.py     keyboard media keys as global hotkeys (Windows)
  - archive_utils.py  .zip album archive detection + filename parsing
  - playlist_cue.py   reading/writing playlists as .cue files
  - player_state.py   Player: plain playlist/queue/position state
  - state_cache.py    saving/loading the session between launches
  - logging_setup.py  debug logger (quiet in the packaged app)
  - build_app.py      packages the app into dist/Playerton/ (PyInstaller)
"""

import tkinter as tk

from app import App


def main():
    """Create the Tk root window, build the app in it, and run the UI
    event loop until the window is closed."""
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
