"""Player: plain state container for the playlist, library roots and
playback position."""


class Player:
    """App state: current playlist, library roots and playback position."""

    def __init__(self):
        self.playlist = []  # list of file paths
        self.library_roots = []  # list of folder paths
        self.current_index = None  # index into playlist of the active track
        self.queue = []  # paths explicitly queued to play next
        self.ignored = set()  # paths skipped during automatic next/previous
