"""Tiny Player - entry point.

The GUI + logic lives in app.py (the App class). Supporting logic is split
across:
  - constants.py    shared constants (file types, tag fields)
  - logging_setup.py debug logger for playback navigation
  - audio_tags.py   tag reading/writing + album art extraction
  - image_utils.py  background-photo resize/opacity helpers
  - archive_utils.py .zip album archive filename helpers
  - player_state.py Player: plain playlist/queue/position state
"""

import tkinter as tk

from app import App


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
