"""The Tiny Player GUI: menu bar, transport toolbar, library tree,
playlist view, status bar, and the Now Playing bar (art, transport
controls, progress bar, and a right-side skip-buttons box with an optional
low-opacity background photo)."""

import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import Counter

import tkinter as tk
from tkinter import ttk, filedialog, simpledialog

from PIL import Image, ImageDraw, ImageFilter, ImageTk
import pygame

from constants import (
    MUSIC_EXTENSIONS, MUSIC_FILETYPES, IMAGE_FILETYPES,
    COMMON_TAG_FIELDS, NUMERIC_TAG_FIELDS, MIXED_SENTINEL,
    PLAYLIST_COLUMNS, DEFAULT_PLAYLIST_COLUMNS, PLAYLIST_COLUMN_WIDTHS,
)
from logging_setup import logger
from audio_tags import (
    format_duration, read_track_tags, get_track_duration,
    make_placeholder_art_pil, get_track_art_pil, read_full_metadata,
    read_common_tags, read_all_track_tags, apply_common_tags,
)
from image_utils import fit_image_cover, apply_low_opacity, extract_palette_from_image
from archive_utils import looks_like_archive, sanitize_filename, parse_album_zip_name
from player_state import Player
from state_cache import load_cache, save_cache
from styles import STYLESHEET, THEMES, resolve_style
from playlist_cue import read_cue_playlist, write_cue_playlist, unique_cue_path, PLAYLISTS_DIR
from excel_log import append_folder_log
from visualizer import analyze_track_spectrum


class App:
    def __init__(self, root):
        self.root = root
        self.player = Player()
        self.track_tags = {}  # path -> {"artist", "title", "album", "duration"}
        self.current_filter = None  # ("album" | "artist", value) or None
        self.search_query = ""  # lowercased search box text; "" = no search filter

        # Shared validation for the Track #/Disc #/Year/BPM tag entry
        # boxes (Properties/Bulk Edit dialogs): only ever let digits
        # through as the user types, formatting the value automatically.
        self._numeric_validate_cmd = (
            self.root.register(self._validate_numeric_input), "%d", "%P")

        try:
            pygame.mixer.init()
            self.audio_ready = True
        except Exception:
            self.audio_ready = False

        self.is_playing = False
        self.is_paused = False
        self.current_duration = 0
        self.elapsed_before_pause = 0.0
        self.play_started_monotonic = None
        self.progress_job = None
        self.shuffle_enabled = False
        self.repeat_enabled = False
        # Each track within the current album/playlist scope is assigned a
        # number (its position in _current_scope_paths()). played_numbers is
        # the ordered history of numbers played since scope was last reset;
        # history_pos is our position within it (lets Previous/Next replay
        # through it). Picking a *new* number (moving past the end of this
        # history) skips any number already in played_numbers, unless Repeat
        # is on -- this is what keeps sequential AND shuffle playback from
        # repeating a song until the whole album/playlist has played once.
        self.played_numbers = []
        self.history_pos = -1

        # Library tree folder(s) currently highlighted/expanded to show
        # where the active "Go to Album"/"More by Same Artist" filter's
        # tracks live; cleared/reapplied each time the filter changes.
        self._library_highlighted_items = []

        # Per-widget scheduled `.after()` fade job ids, keyed by widget,
        # for every smooth color-fade animation in the app (status bar,
        # "Currently viewing folder" label, now-playing row highlight,
        # library filter-match highlight, ...) -- see _animate_color_fade.
        self._fade_jobs = {}
        # The current cover art shown in the Now Playing bar, as a raw
        # PIL Image (kept alongside the PhotoImage so the next track
        # change can crossfade FROM it) -- see _play_track/_crossfade_art.
        self._current_art_pil = None
        self._art_crossfade_job = None

        # "Spinning disk" effect for the Now Playing album art (a CD/
        # vinyl-style circular crop of the current cover art that rotates
        # while a track is actively playing, and freezes when paused/
        # stopped) -- see _set_disk_base/_start_disk_spin. Toggled via
        # View > Spin Album Art (self.disk_spin_enabled/disk_spin_var are
        # set up further below, once the cache has been loaded).
        self._disk_base_image = None  # native-res, angle-0 disk (PIL Image)
        self._disk_hires_image = None  # supersampled, angle-0 disk (for spin)
        self._disk_angle = 0
        self._disk_spin_job = None

        # "Visualizer" popup window (see open_visualizer/_tick_visualizer):
        # a per-track frequency-bar animation, analyzed ONCE up front in
        # a background thread (see visualizer.py's module docstring) and
        # then indexed by the track's current playback position on every
        # animation tick. `_visualizer_window`/`_visualizer_canvas` are
        # None whenever the popup isn't open. `_visualizer_track_path`/
        # `_visualizer_frames`/`_visualizer_fps` describe whichever
        # track the currently-held analysis result is FOR (compared
        # against the actually-playing path every tick, so a track
        # change naturally triggers re-analysis without needing any
        # hook in _play_track/on_stop/etc.). `_visualizer_analyzing_path`
        # is set while a background analysis is in flight, to avoid
        # kicking off a duplicate thread for the same track.
        self._visualizer_window = None
        self._visualizer_canvas = None
        self._visualizer_bar_ids = []
        self._visualizer_track_path = None
        self._visualizer_frames = None
        self._visualizer_fps = 20
        self._visualizer_analyzing_path = None
        self._visualizer_queue = queue.Queue()
        self._visualizer_tick_job = None
        self._VISUALIZER_NUM_BARS = 32

        # Background library-scanning state (see _start_library_scan):
        # a thread-safe queue that scan worker thread(s) push results
        # onto, drained on the main/UI thread via root.after so that
        # opening/restoring a folder with a lot of tracks doesn't freeze
        # the window -- tag reads (the slow part) happen off-thread.
        # `_library_scans` maps a scan id -> {"path", "total", "done",
        # "announce"} for that scan's own progress/completion message;
        # `_library_scan_active` is how many scans are still running.
        self._library_scan_queue = queue.Queue()
        self._library_scans = {}
        self._library_scan_next_id = 1
        self._library_scan_active = 0
        self._library_scan_drain_job = None

        # Index being dragged in the queue panel, for click-and-drag
        # reordering (session-only UI state).
        self._queue_drag_index = None

        # Cue-sheet-backed playlists (see playlist_cue.py), shown under
        # the "Playlists" node in the library tree. Keyed by the cue
        # file's absolute path; each value is
        # {"name", "tracks": [path, ...], "node_id": tree item id,
        #  "parent": cue_path of its "mother" playlist, or None}.
        # "parent" implements one-way playlist inheritance (see
        # set_playlist_parent/add_tracks_to_playlist): any track added
        # to a playlist is also propagated up to its parent, and its
        # parent's parent, and so on -- never the other direction.
        self.playlists = {}
        # Maps a playlist's per-track tree item id -> (cue_path, real_path),
        # since those leaf items use a synthetic iid (a real track path can
        # already be in use as another tree item's iid elsewhere).
        self._playlist_track_info = {}
        # Click-and-drag-to-a-playlist state (session-only UI state): the
        # real track paths currently being dragged, and the small floating
        # indicator window shown while dragging.
        self._drag_paths = None
        self._drag_indicator = None

        # Restore last session's settings/state (theme, library folders,
        # playlist, background images, last-playing track), if any.
        self.cache = load_cache()

        # Dark mode is on by default; "dark_blue" is a selectable variant.
        saved_theme = self.cache.get("theme_name")
        self.theme_name = saved_theme if saved_theme in THEMES else "dark"
        self.theme_var = tk.StringVar(value=self.theme_name)
        self.palette = THEMES[self.theme_name]

        # Whether the Now Playing album art spins like a CD while a
        # track plays (View > Spin Album Art); on by default.
        self.disk_spin_enabled = bool(
            self.cache.get("disk_spin_enabled", True))
        self.disk_spin_var = tk.BooleanVar(value=self.disk_spin_enabled)

        # "Browsing mode" (View > Browsing Mode): when on, double-
        # clicking a track/folder/queue row no longer starts playback --
        # it only ever starts via an explicit "Play"/"Play Now" from the
        # right-click menu. Lets you freely browse the library/playlist
        # and do other actions (queue, tag edits, drag-to-playlist, ...)
        # while something is already playing, without a stray
        # double-click accidentally interrupting it. Off by default.
        self.browsing_mode = bool(self.cache.get("browsing_mode", False))
        self.browsing_mode_var = tk.BooleanVar(value=self.browsing_mode)

        # "Album Art Theme" (View > Album Art Theme): when on, the
        # entire app's color palette is derived from the currently
        # playing track's cover art (see image_utils.extract_palette_
        # from_image / _update_dynamic_theme_from_art) instead of a
        # fixed theme -- recomputed every time the Now Playing art
        # settles on a new track. `_dynamic_palette` is the currently
        # active derived palette (None until a track with art has
        # played since this was turned on); picking a theme from the
        # View menu's theme list turns this back off (see set_theme).
        self.dynamic_theme_enabled = bool(
            self.cache.get("dynamic_theme_enabled", False))
        self.dynamic_theme_var = tk.BooleanVar(
            value=self.dynamic_theme_enabled)
        self._dynamic_palette = None

        # Path to the Excel (.xlsx) "library log" workbook that newly
        # chosen folders get appended to (see _log_scan_to_excel/
        # excel_log.py) -- None until the user has picked one, either
        # proactively (File > Set Library Log File...) or when first
        # prompted the first time a folder is actually added. Once
        # declined for this session (_library_log_prompt_declined),
        # further folder-adds skip the prompt/logging silently rather
        # than asking every single time.
        self.library_log_path = self.cache.get("library_log_path")
        self._library_log_prompt_declined = False

        # Which playlist table columns (Artist/Title/Album/... /BPM) are
        # currently shown; toggled via the table's right-click header menu.
        saved_columns = self.cache.get("playlist_columns")
        if isinstance(saved_columns, list) and saved_columns:
            visible_set = set(saved_columns)
        else:
            visible_set = set(DEFAULT_PLAYLIST_COLUMNS)
        self.playlist_column_visible = {
            key: (key in visible_set) for key, _label in PLAYLIST_COLUMNS}

        # Current playlist table sort column/direction, set by double-
        # clicking a column header. This is session-only (deliberately
        # NOT saved to the cache/restored between launches).
        self.playlist_sort_key = None
        self.playlist_sort_reverse = False

        self.root.title("Tiny Player")
        try:
            self.root.attributes("-zoomed", True)
        except tk.TclError:
            self.root.geometry(
                f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_menu()
        self._build_toolbar()
        self._build_search_bar()
        self._build_status_bar()
        self._build_now_playing_bar()
        self._build_body()
        self._apply_theme()
        self._restore_from_cache()

        # Escape clears whichever tree currently has a multi-selection
        # (playlist table, library tree, or queue panel) -- a quick way
        # to back out of a multi-track selection without clicking away.
        self.root.bind_all("<Escape>", self._on_escape_clear_selection)

    # -- menu bar ---------------------------------------------------
    def _build_menu(self):
        menu_bar = tk.Menu(self.root)
        self.menu_bar = menu_bar
        self._top_menus = []

        file_menu = tk.Menu(menu_bar, tearoff=0)
        file_menu.add_command(label="Open File(s)...", command=self.open_files)
        file_menu.add_command(label="Open Folder...", command=self.open_folder)
        file_menu.add_command(
            label="Import Album Archive (.zip)...", command=self.open_archive)
        file_menu.add_separator()
        file_menu.add_command(
            label="New Playlist...", command=lambda: self.create_playlist())
        file_menu.add_command(
            label="Import Cue Playlist...", command=self.import_cue_playlist)
        file_menu.add_separator()
        file_menu.add_command(
            label="Set Library Log File...", command=self.choose_library_log_path)
        file_menu.add_command(
            label="Log Existing Library to Excel", command=self.log_existing_library)
        file_menu.add_separator()
        file_menu.add_command(label="Refresh App", command=self._relaunch_app)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menu_bar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menu_bar, tearoff=0)
        edit_menu.add_command(label="Remove Selected",
                              command=self.remove_selected)
        edit_menu.add_command(label="Clear Playlist",
                              command=self.clear_playlist)
        edit_menu.add_separator()
        edit_menu.add_command(label="Properties...",
                              command=lambda: self.open_properties())
        edit_menu.add_command(label="Bulk Edit Properties...",
                              command=self.open_bulk_properties)
        edit_menu.add_separator()
        edit_menu.add_command(label="Show Full Playlist",
                              command=self.show_full_playlist)
        menu_bar.add_cascade(label="Edit", menu=edit_menu)

        view_menu = tk.Menu(menu_bar, tearoff=0)
        view_menu.add_command(label="Library")
        view_menu.add_command(label="Playlist")
        view_menu.add_separator()
        view_menu.add_checkbutton(
            label="Spin Album Art", variable=self.disk_spin_var,
            command=self.toggle_disk_spin)
        view_menu.add_checkbutton(
            label="Browsing Mode (double-click won't play)",
            variable=self.browsing_mode_var,
            command=self.toggle_browsing_mode)
        view_menu.add_checkbutton(
            label="Album Art Theme (dynamic colors)",
            variable=self.dynamic_theme_var,
            command=self.toggle_dynamic_theme)
        view_menu.add_separator()
        # Built from styles.THEMES so adding a new theme there (see that
        # module's docstring) automatically gets a menu entry here too --
        # no code changes needed to pick it up.
        for theme_name in THEMES:
            view_menu.add_radiobutton(
                label=theme_name.replace("_", " ").title(), value=theme_name,
                variable=self.theme_var,
                command=lambda t=theme_name: self.set_theme(t))
        menu_bar.add_cascade(label="View", menu=view_menu)

        playback_menu = tk.Menu(menu_bar, tearoff=0)
        playback_menu.add_command(label="Play", command=self.on_play)
        playback_menu.add_command(label="Pause", command=self.on_pause)
        playback_menu.add_command(label="Stop", command=self.on_stop)
        playback_menu.add_command(label="Previous", command=self.on_previous)
        playback_menu.add_command(label="Next", command=self.on_next)
        playback_menu.add_separator()
        playback_menu.add_command(
            label="Visualizer...", command=self.open_visualizer)
        menu_bar.add_cascade(label="Playback", menu=playback_menu)

        library_menu = tk.Menu(menu_bar, tearoff=0)
        library_menu.add_command(
            label="Add Folder to Library...", command=self.open_folder)
        menu_bar.add_cascade(label="Library", menu=library_menu)

        help_menu = tk.Menu(menu_bar, tearoff=0)
        help_menu.add_command(label="About")
        menu_bar.add_cascade(label="Help", menu=help_menu)

        self._top_menus = [
            file_menu, edit_menu, view_menu, playback_menu, library_menu, help_menu,
        ]
        self.root.config(menu=menu_bar)

    # -- transport toolbar --------------------------------------------
    def _build_toolbar(self):
        toolbar = ttk.Frame(self.root, padding=4)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar, text="|<<", width=4, style="Transport.TButton",
                   command=self.on_previous).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Play", style="Transport.TButton",
                   command=self.on_play).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Pause", style="Transport.TButton",
                   command=self.on_pause).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Stop", style="Transport.TButton",
                   command=self.on_stop).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text=">>|", width=4, style="Transport.TButton",
                   command=self.on_next).pack(side=tk.LEFT, padx=2)

        # Background library-scan loading bar (see _start_library_scan/
        # _show_loading_bar): sits where a seek bar might otherwise go,
        # but is only ever packed/visible while a scan is actually
        # running -- built here (not packed immediately) so
        # _show_loading_bar can insert it in this exact spot (via
        # `before=self.volume_label`) whenever a scan starts.
        self._build_loading_bar(toolbar)

        self.volume_label = ttk.Label(
            toolbar, text="Vol", style="ToolbarLabel.TLabel")
        self.volume_label.pack(side=tk.LEFT, padx=(10, 2))
        self.volume_var = tk.DoubleVar(value=80)
        volume = ttk.Scale(toolbar, from_=0, to=100, variable=self.volume_var,
                           orient=tk.HORIZONTAL, length=100, command=self.on_volume_change)
        volume.pack(side=tk.LEFT, padx=2)

    # -- search bar (filters the library tree + playlist table) ----------
    def _build_search_bar(self):
        search_frame = ttk.Frame(self.root, padding=(4, 2))
        search_frame.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(search_frame, text="Search:", style="ToolbarLabel.TLabel").pack(
            side=tk.LEFT, padx=(0, 4))
        self.search_var = tk.StringVar(value="")
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            search_frame, text="Clear", style="PanelAction.TButton",
            command=lambda: self.search_var.set("")).pack(side=tk.LEFT, padx=(4, 0))
        self.search_var.trace_add("write", self._on_search_changed)

    def _on_search_changed(self, *_args):
        self.search_query = self.search_var.get().strip().lower()
        self._refresh_playlist_view()
        if not self.search_query:
            self._clear_library_highlight()
            self.status_var.set("Ready")
            return
        matched_paths = [
            p for p in self.player.playlist if self._matches_search(p)]
        self._highlight_library_folders(matched_paths)
        noun = "match" if len(matched_paths) == 1 else "matches"
        self.status_var.set(
            f"Search '{self.search_query}': {len(matched_paths)} {noun}")

    def _matches_search(self, path):
        if not self.search_query:
            return True
        tags = self.track_tags.get(path, {})
        haystack = " ".join([
            tags.get("artist", ""), tags.get("title", ""),
            tags.get("album", ""), os.path.basename(path),
        ]).lower()
        return self.search_query in haystack

    # -- background library-scan loading bar (hidden unless a scan is
    # actively running -- see _start_library_scan/_show_loading_bar) -----
    def _build_loading_bar(self, parent):
        self.loading_frame = ttk.Frame(parent, padding=(0, 0, 4, 0))
        # Deliberately NOT packed here -- _show_loading_bar/_hide_loading_bar
        # pack/unpack it on demand, so it only takes up space while a
        # library scan is actually in progress.
        self.loading_bar_var = tk.StringVar(value="")
        ttk.Label(
            self.loading_frame, textvariable=self.loading_bar_var,
            style="ToolbarLabel.TLabel", width=26, anchor=tk.W,
        ).pack(side=tk.LEFT, padx=(0, 6))
        self.loading_progress = ttk.Progressbar(
            self.loading_frame, orient=tk.HORIZONTAL, mode="determinate")
        self.loading_progress.pack(side=tk.LEFT, fill=tk.X, expand=True)

    # -- now playing bar --------------------------------------------------
    def _build_now_playing_bar(self):
        frame = ttk.Frame(self.root, relief=tk.RIDGE, borderwidth=1, padding=8)
        frame.pack(side=tk.BOTTOM, fill=tk.X)

        self._current_art_pil = make_placeholder_art_pil()
        self._set_disk_base(self._current_art_pil)
        self.now_playing_art_image = ImageTk.PhotoImage(self._current_art_pil)
        self.art_label = tk.Label(frame, image=self.now_playing_art_image)
        self.art_label.pack(side=tk.LEFT, padx=(0, 10))
        self._show_static_art_frame()

        self._build_right_box(frame)

        info_frame = ttk.Frame(frame)
        info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.now_title_var = tk.StringVar(value="No track playing")
        self.now_artist_var = tk.StringVar(value="")
        self.now_title_label = ttk.Label(
            info_frame, textvariable=self.now_title_var,
            style="NowPlayingTitle.TLabel")
        self.now_title_label.pack(anchor=tk.W)
        self.now_artist_label = ttk.Label(
            info_frame, textvariable=self.now_artist_var,
            style="NowPlayingArtist.TLabel")
        self.now_artist_label.pack(anchor=tk.W)

        controls_row = ttk.Frame(info_frame)
        controls_row.pack(fill=tk.X, pady=(4, 0))

        self.play_pause_button = tk.Button(
            controls_row, text="Play", width=7, command=self.on_play_pause_toggle)
        self.play_pause_button.pack(side=tk.LEFT, padx=(0, 4))

        self.stop_button = tk.Button(controls_row, text="Stop", width=6,
                                     command=self.on_stop)
        self.stop_button.pack(side=tk.LEFT, padx=4)

        self.add_queue_button = tk.Button(
            controls_row, text="Add to Queue", width=12,
            command=self.on_add_selection_to_queue)
        self.add_queue_button.pack(side=tk.LEFT, padx=4)

        self.shuffle_button = tk.Button(
            controls_row, text="Shuffle", width=8, relief=tk.RAISED,
            command=self.toggle_shuffle)
        self.shuffle_button.pack(side=tk.LEFT, padx=4)

        self.repeat_button = tk.Button(
            controls_row, text="Repeat", width=8, relief=tk.RAISED,
            command=self.toggle_repeat)
        self.repeat_button.pack(side=tk.LEFT, padx=4)

        self.visualizer_button = tk.Button(
            controls_row, text="Visualizer", width=10, relief=tk.RAISED,
            command=self.open_visualizer)
        self.visualizer_button.pack(side=tk.LEFT, padx=4)

        progress_row = ttk.Frame(info_frame)
        progress_row.pack(fill=tk.X, pady=(6, 0))

        self.elapsed_var = tk.StringVar(value="0:00")
        ttk.Label(progress_row, textvariable=self.elapsed_var, width=6,
                  style="ProgressTime.TLabel").pack(side=tk.LEFT)

        self.now_playing_progress = ttk.Progressbar(
            progress_row, orient=tk.HORIZONTAL, mode="determinate")
        self.now_playing_progress.pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.now_playing_progress.bind(
            "<Button-1>", self._on_progress_left_click)
        self.now_playing_progress.bind(
            "<Button-3>", self._on_progress_right_click)

        self.duration_var = tk.StringVar(value="0:00")
        ttk.Label(progress_row, textvariable=self.duration_var, width=6,
                  style="ProgressTime.TLabel").pack(side=tk.LEFT)

    # -- right-side skip-buttons box (+ optional background photo) --------
    def _build_right_box(self, parent):
        """A box on the right side of the Now Playing bar containing its own
        Previous/Next buttons (mirroring the toolbar's |<< / >>| buttons),
        with an optional user-uploaded background photo shown at low
        opacity behind those buttons. The "Set Background..." button sits
        below that box (not part of the image itself)."""
        self.right_box_width = 170
        self.right_box_height = 54

        container = ttk.Frame(parent)
        container.pack(side=tk.RIGHT, padx=(10, 0))

        style = ttk.Style()
        bg_color = style.lookup("TFrame", "background") or "#f0f0f0"
        self.right_box_base_color = self._rgb_of(bg_color)

        canvas = tk.Canvas(container, width=self.right_box_width,
                           height=self.right_box_height,
                           highlightthickness=0, bd=0, bg=bg_color)
        canvas.pack(side=tk.TOP)
        canvas.bind("<Button-3>", self._on_right_box_right_click)
        self.right_box_canvas = canvas
        self.right_box_bg_item = None
        self.right_box_bg_photo = None
        self.right_box_bg_source_image = None
        self.right_box_bg_path = None

        nav_row = ttk.Frame(canvas)
        ttk.Button(nav_row, text="|<<", width=4, style="NavBox.TButton",
                   command=self.on_previous).pack(side=tk.LEFT, padx=2)
        ttk.Button(nav_row, text=">>|", width=4, style="NavBox.TButton",
                   command=self.on_next).pack(side=tk.LEFT, padx=2)
        canvas.create_window(self.right_box_width // 2,
                             self.right_box_height // 2,
                             anchor=tk.CENTER, window=nav_row)

        ttk.Button(container, text="Set Background...", style="PanelAction.TButton",
                   command=self._choose_right_box_background).pack(
            side=tk.TOP, fill=tk.X, pady=(4, 0))

    def _rgb_of(self, color):
        r, g, b = self.root.winfo_rgb(color)
        return (r >> 8, g >> 8, b >> 8)

    def _grayscale_color(self, color):
        """Desaturate `color` to a mid-gray of the same perceived
        brightness -- used for the "ignored" track marker so it looks
        grayed-out regardless of the active theme's colors."""
        r, g, b = self._rgb_of(color)
        gray = round(0.299 * r + 0.587 * g + 0.114 * b)
        return "#%02x%02x%02x" % (gray, gray, gray)

    def _grayscale_background(self, color, amount=0.3):
        """Tint `color` (a background) toward a FIXED mid-gray (not one
        matched to `color`'s own brightness -- that would be a no-op for
        themes whose background is already near-neutral, e.g. the dark
        theme's #1e1e1e), for a visibly grayed-out row background on
        ignored tracks, on top of the dimmed text from _grayscale_color."""
        r, g, b = self._rgb_of(color)
        blended = self._blend_rgb((r, g, b), (128, 128, 128), amount)
        return "#%02x%02x%02x" % blended

    @staticmethod
    def _blend_rgb(start_rgb, end_rgb, ratio):
        return tuple(
            round(start + (end - start) * ratio)
            for start, end in zip(start_rgb, end_rgb))

    # -- generic smooth color-fade animation helper -----------------------
    # Powers every fade effect in the app (status bar messages, the
    # "Currently viewing folder" label, the now-playing row highlight,
    # the library filter-match highlight, ...): animates a widget's color
    # from `start_color` to `end_color` over `total_steps` steps, then
    # calls `on_complete` (if given). Any fade already running for the
    # same `key` is cancelled first, so rapid updates always restart
    # cleanly from full color instead of stacking/racing.
    def _animate_color_fade(self, key, apply_color, start_color, end_color,
                            delay_ms=0, total_steps=14, step_ms=30, on_complete=None):
        existing = self._fade_jobs.pop(key, None)
        if existing is not None:
            self.root.after_cancel(existing)
        start_rgb = self._rgb_of(start_color)
        end_rgb = self._rgb_of(end_color)
        apply_color(start_color)
        job = self.root.after(
            delay_ms, self._step_color_fade, key, apply_color,
            start_rgb, end_rgb, 0, total_steps, step_ms, on_complete)
        self._fade_jobs[key] = job

    def _step_color_fade(self, key, apply_color, start_rgb, end_rgb, step,
                         total_steps, step_ms, on_complete):
        if step >= total_steps:
            self._fade_jobs.pop(key, None)
            if on_complete:
                on_complete()
            return
        ratio = (step + 1) / total_steps
        blended = self._blend_rgb(start_rgb, end_rgb, ratio)
        try:
            apply_color("#%02x%02x%02x" % blended)
        except tk.TclError:
            self._fade_jobs.pop(key, None)
            return
        job = self.root.after(
            step_ms, self._step_color_fade, key, apply_color,
            start_rgb, end_rgb, step + 1, total_steps, step_ms, on_complete)
        self._fade_jobs[key] = job

    def _show_viewing_folder_label(self, name):
        """Show "Currently viewing folder: <name>" above the playlist
        table, then automatically fade it out a couple seconds later
        (fading the text color to the background color rather than
        actual widget transparency, since plain ttk labels don't support
        per-widget alpha)."""
        self.viewing_folder_var.set(f"Currently viewing folder: {name}")
        self._animate_color_fade(
            self.viewing_folder_label,
            lambda color: self.viewing_folder_label.configure(
                foreground=color),
            self.palette["fg"], self.palette["bg"],
            delay_ms=2000,
            on_complete=lambda: self.viewing_folder_var.set(""))

    def _on_right_box_right_click(self, event):
        menu = tk.Menu(self.root, tearoff=0, **self._menu_colors())
        menu.add_command(label="Set Background Image...",
                         command=self._choose_right_box_background)
        menu.add_command(
            label="Clear Background",
            command=self._clear_right_box_background,
            state=tk.NORMAL if self.right_box_bg_item is not None else tk.DISABLED)
        self._popup_menu(menu, event)

    def _choose_right_box_background(self):
        path = filedialog.askopenfilename(
            title="Choose a background image", filetypes=IMAGE_FILETYPES)
        if not path:
            return
        try:
            image = Image.open(path)
        except Exception as exc:
            self.status_var.set(f"Could not load image: {exc}")
            return
        self.right_box_bg_source_image = image
        self.right_box_bg_path = path
        self._set_right_box_background(image)
        self.status_var.set(f"Set background image: {os.path.basename(path)}")

    def _clear_right_box_background(self):
        self.right_box_bg_source_image = None
        self.right_box_bg_path = None
        if self.right_box_bg_item is not None:
            self.right_box_canvas.delete(self.right_box_bg_item)
            self.right_box_bg_item = None
            self.right_box_bg_photo = None
        self.status_var.set("Cleared background image")

    def _set_right_box_background(self, image):
        fitted = fit_image_cover(
            image, (self.right_box_width, self.right_box_height))
        faded = apply_low_opacity(
            fitted, opacity=0.25, base_color=self.right_box_base_color)
        self.right_box_bg_photo = ImageTk.PhotoImage(faded)
        if self.right_box_bg_item is None:
            self.right_box_bg_item = self.right_box_canvas.create_image(
                0, 0, anchor=tk.NW, image=self.right_box_bg_photo)
            # create_window'd widgets always paint above plain canvas items
            # in Tk regardless of stacking order, but push it to the back
            # explicitly anyway for clarity/future-proofing.
            self.right_box_canvas.tag_lower(self.right_box_bg_item)
        else:
            self.right_box_canvas.itemconfig(
                self.right_box_bg_item, image=self.right_box_bg_photo)

    # -- dark mode / theming ---------------------------------------------
    def _menu_colors(self):
        """Keyword args to apply the current palette to a tk.Menu."""
        palette = self.palette
        return dict(
            bg=palette["bg"], fg=palette["fg"],
            activebackground=palette["select_bg"],
            activeforeground=palette["select_fg"],
        )

    def set_theme(self, theme_name):
        self.theme_name = theme_name
        self.theme_var.set(theme_name)
        if self.dynamic_theme_enabled:
            # Explicitly picking a fixed theme overrides/cancels "Album
            # Art Theme" -- otherwise the choice would appear to do
            # nothing (dynamic mode would just keep overriding it on the
            # next track change).
            self.dynamic_theme_enabled = False
            self.dynamic_theme_var.set(False)
            self._dynamic_palette = None
        self._apply_theme()
        self.status_var.set(f"Theme: {theme_name.replace('_', ' ').title()}")

    def toggle_dynamic_theme(self):
        """Handler for the View > "Album Art Theme" checkbutton: when
        on, the app's whole color palette is derived from the currently
        playing track's cover art instead of a fixed theme (see
        _update_dynamic_theme_from_art), recomputed on every track
        change. Turning it off reverts to the previously selected fixed
        theme."""
        self.dynamic_theme_enabled = self.dynamic_theme_var.get()
        if self.dynamic_theme_enabled:
            if self._current_art_pil is not None:
                self._update_dynamic_theme_from_art(self._current_art_pil)
            else:
                self.status_var.set(
                    "Album Art Theme: On (will apply once a track with cover art plays)")
        else:
            self._dynamic_palette = None
            self._apply_theme()
            self.status_var.set("Album Art Theme: Off")

    def _update_dynamic_theme_from_art(self, art_pil):
        """Recompute and apply the "Album Art Theme" palette from
        `art_pil` (the Now Playing cover art that just "settled" on a
        new track) -- a no-op if the feature is currently turned off."""
        if not self.dynamic_theme_enabled:
            return
        try:
            palette = extract_palette_from_image(art_pil)
        except Exception:
            return
        self._dynamic_palette = palette
        self._apply_theme(palette)
        self.status_var.set("Album Art Theme: updated from cover art")

    def _apply_theme(self, palette=None):
        """Apply `palette` (or, if not given, whichever's currently
        active -- the "Album Art Theme" dynamic palette if that's turned
        on, otherwise the selected fixed theme's) to every widget,
        driven by the shared CSS-like STYLESHEET (see styles.py): ttk
        selectors get `ttk.Style().configure(...)` (picked up live by
        every ttk widget using that style); "#name" selectors
        reconfigure one specific plain tk widget directly (menus,
        canvases, and the playlist background photo are handled
        separately below since they need logic beyond "set these
        properties")."""
        if palette is None:
            if self.dynamic_theme_enabled and self._dynamic_palette is not None:
                palette = self._dynamic_palette
            else:
                palette = THEMES[self.theme_name]
        self.palette = palette

        # Cancel any in-flight fade animation (now-playing title/artist,
        # status bar, "Currently viewing folder" label, ...) BEFORE
        # resetting their colors below -- each fade's step closure
        # captured its start/end colors from the OLD theme, so leaving
        # one running would keep repainting a stale in-between color
        # over our reset for a few more steps (i.e. the exact "stuck on
        # the old theme's color" bug this fixes).
        for job in self._fade_jobs.values():
            self.root.after_cancel(job)
        self._fade_jobs.clear()

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            ".", background=palette["bg"], foreground=palette["fg"])

        for selector, props in STYLESHEET.items():
            resolved = resolve_style(props, palette)
            if selector.startswith("#"):
                widget = getattr(self, selector[1:], None)
                if widget is not None:
                    widget.configure(**resolved)
            else:
                style.configure(selector, **resolved)

        style.map("TButton", background=[("active", palette["select_bg"])],
                  foreground=[("active", palette["select_fg"])])
        style.map(
            "Treeview",
            background=[("selected", palette["select_bg"])],
            foreground=[("selected", palette["select_fg"])])

        # Contrasting highlight for the library folder matching the
        # current "Go to Album"/"More by Same Artist" filter (tag colors
        # live on the widget instance, not the ttk style, so they need to
        # be reapplied whenever the theme changes).
        if hasattr(self, "library_tree"):
            self.library_tree.tag_configure(
                "filter_match", background=palette["highlight_bg"],
                foreground=palette["highlight_fg"])
            self.library_tree.tag_configure(
                "ignored", foreground=self._grayscale_color(palette["fg"]),
                background=self._grayscale_background(palette["bg"]))

        # Same contrasting highlight for the currently-playing track's row
        # in the playlist table, when it's visible in the current view.
        # "in_queue" gets its own subtler color so a queued-but-not-yet-
        # playing track is still distinguishable from the actively
        # playing one. "ignored" grays out a track skipped by automatic
        # next/previous (see context_toggle_ignore).
        if hasattr(self, "playlist_tree"):
            self.playlist_tree.tag_configure(
                "now_playing", background=palette["highlight_bg"],
                foreground=palette["highlight_fg"])
            self.playlist_tree.tag_configure(
                "in_queue", background=palette["queue_bg"],
                foreground=palette["queue_fg"])
            self.playlist_tree.tag_configure(
                "ignored", foreground=self._grayscale_color(palette["field_fg"]),
                background=self._grayscale_background(palette["field_bg"]))

        # These labels get their `foreground` set directly (bypassing the
        # ttk style) by the fade-in/fade-out animations above, which
        # otherwise leaves a stale color override from the PREVIOUS theme
        # in place after switching themes (a plain `style.configure(...)`
        # can't override a per-widget option). Reset them to the new
        # theme's real color explicitly.
        if hasattr(self, "now_title_label"):
            self.now_title_label.configure(foreground=palette["fg"])
        if hasattr(self, "now_artist_label"):
            self.now_artist_label.configure(foreground=palette["fg"])
        if hasattr(self, "status_bar_label"):
            self.status_bar_label.configure(foreground=palette["fg"])
        if hasattr(self, "viewing_folder_label"):
            self.viewing_folder_label.configure(foreground=palette["fg"])

        self.root.configure(bg=palette["bg"])

        menu_colors = self._menu_colors()
        if hasattr(self, "menu_bar"):
            self.menu_bar.configure(**menu_colors)
        for menu in getattr(self, "_top_menus", []):
            menu.configure(**menu_colors)

        if hasattr(self, "right_box_canvas"):
            self.right_box_canvas.configure(bg=palette["bg"])
            self.right_box_base_color = self._rgb_of(palette["bg"])
            if self.right_box_bg_source_image is not None:
                self._set_right_box_background(self.right_box_bg_source_image)

        if self._visualizer_window is not None and self._visualizer_window.winfo_exists():
            self._visualizer_window.configure(bg=palette["field_bg"])
            self._visualizer_canvas.configure(bg=palette["field_bg"])
            # Bar item ids are recreated (not just recolored) on the next
            # tick if their count doesn't match -- forcing a "all"-clear
            # here is simplest to guarantee the message/bar colors below
            # never look stale after a theme switch.
            self._visualizer_canvas.delete("all")
            self._visualizer_bar_ids = []

        if getattr(self, "_current_art_pil", None) is not None:
            # Rebuild the Now Playing "spinning disk" so its corner-fill
            # color (and rim/hole colors) match the new theme, instead of
            # keeping the old theme's background baked into the image
            # (kept up to date even while the effect is toggled off, so
            # it's ready to display correctly as soon as it's turned
            # back on).
            self._set_disk_base(self._current_art_pil)
            if self._disk_spin_job is None:
                self._show_static_art_frame()

        if getattr(self, "playlist_bg_source_image", None) is not None:
            # Force Tk to finish recomputing geometry from the style
            # changes above before measuring the treeview -- querying
            # winfo_width/height immediately after a batch of style.configure
            # calls can otherwise return a stale/transitional size, which
            # would bake the background photo in at the wrong (tiny) size.
            self.root.update_idletasks()
            self._apply_playlist_background()

    # -- library (left) + playlist (center) ----------------------------
    def _build_body(self):
        body = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Left: media library ("file explorer") on top, queue panel below
        library_frame = ttk.Frame(body, width=250)
        library_pane = ttk.PanedWindow(library_frame, orient=tk.VERTICAL)
        library_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        library_tree_frame = ttk.Frame(library_pane)
        self.library_tree = ttk.Treeview(
            library_tree_frame, show="tree", style="Library.Treeview")
        lib_scroll = ttk.Scrollbar(
            library_tree_frame, orient=tk.VERTICAL, command=self.library_tree.yview)
        self.library_tree.configure(yscrollcommand=lib_scroll.set)
        lib_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.library_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        library_pane.add(library_tree_frame, weight=2)

        # A permanent "Playlists" node (always first/top) holding every
        # cue-sheet-backed playlist as a child folder-like node, each
        # containing its tracks -- see create_playlist()/import_cue_playlist().
        self._playlists_root_id = "__playlists_root__"
        self.library_tree.insert(
            "", 0, iid=self._playlists_root_id, text="Playlists", open=True)

        queue_frame = ttk.Frame(library_pane)
        self._build_queue_panel(queue_frame)
        library_pane.add(queue_frame, weight=1)

        body.add(library_frame, weight=1)

        self.library_tree.bind("<Double-1>", self._on_tree_double_click)
        self.library_tree.bind("<Button-3>", self._on_tree_right_click)

        # Center: playlist
        playlist_frame = ttk.Frame(body)

        self.playlist_bg_source_image = None
        self.playlist_bg_photo = None
        self._playlist_bg_style_installed = False
        self.playlist_bg_path = None

        playlist_header = ttk.Frame(playlist_frame)
        playlist_header.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(playlist_header, text="Set Background...", style="PanelAction.TButton",
                   command=self._choose_playlist_background).pack(
            side=tk.RIGHT, padx=2, pady=2)
        ttk.Button(playlist_header, text="Clear Background", style="PanelAction.TButton",
                   command=self._clear_playlist_background).pack(
            side=tk.RIGHT, padx=2, pady=2)

        # Transient "Currently viewing folder: ..." label, shown briefly
        # above the playlist table whenever a folder/playlist is opened
        # from the library tree, then automatically faded out -- see
        # _show_viewing_folder_label.
        self.viewing_folder_var = tk.StringVar(value="")
        self.viewing_folder_label = ttk.Label(
            playlist_frame, textvariable=self.viewing_folder_var,
            style="ViewingFolder.TLabel")
        self.viewing_folder_label.pack(side=tk.TOP, fill=tk.X)

        playlist_body = ttk.Frame(playlist_frame)
        playlist_body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.playlist_tree = ttk.Treeview(
            playlist_body, columns=(), show="headings")
        self._apply_playlist_columns()
        pl_scroll = ttk.Scrollbar(
            playlist_body, orient=tk.VERTICAL, command=self.playlist_tree.yview)
        self.playlist_tree.configure(yscrollcommand=pl_scroll.set)
        pl_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.playlist_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body.add(playlist_frame, weight=3)

        self.playlist_tree.bind("<Double-1>", self._on_tree_double_click)
        self.playlist_tree.bind("<Button-3>", self._on_tree_right_click)
        self.playlist_tree.bind(
            "<Configure>", self._on_playlist_tree_configure)
        self.playlist_tree.bind(
            "<Button-1>", self._on_playlist_heading_click, add="+")

        # Drag-and-drop tracks onto a playlist node in the library tree,
        # from either the playlist table or the library tree itself.
        for tree in (self.playlist_tree, self.library_tree):
            tree.bind("<ButtonPress-1>", self._on_drag_start, add="+")
            tree.bind("<B1-Motion>", self._on_drag_motion, add="+")
            tree.bind("<ButtonRelease-1>", self._on_drag_release, add="+")

    # -- queue panel (bottom half of the library/"file explorer" side) ---
    def _build_queue_panel(self, parent):
        header = ttk.Frame(parent)
        header.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(header, text="Queue", style="QueueHeader.TLabel").pack(
            side=tk.LEFT, padx=4, pady=2)
        ttk.Button(header, text="Clear", style="PanelAction.TButton",
                   command=self._clear_queue).pack(
            side=tk.RIGHT, padx=2, pady=2)

        tree_frame = ttk.Frame(parent)
        tree_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.queue_tree = ttk.Treeview(
            tree_frame, columns=("title", "artist"), show="headings", height=6,
            style="Queue.Treeview")
        self.queue_tree.heading("title", text="Title")
        self.queue_tree.heading("artist", text="Artist")
        self.queue_tree.column("title", width=140, anchor=tk.W)
        self.queue_tree.column("artist", width=90, anchor=tk.W)
        q_scroll = ttk.Scrollbar(
            tree_frame, orient=tk.VERTICAL, command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=q_scroll.set)
        self.queue_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        q_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        button_row = ttk.Frame(parent)
        button_row.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(button_row, text="Up", style="PanelAction.TButton",
                   command=lambda: self._move_queue_selection(-1)).pack(
            side=tk.LEFT, padx=2, pady=2)
        ttk.Button(button_row, text="Down", style="PanelAction.TButton",
                   command=lambda: self._move_queue_selection(1)).pack(
            side=tk.LEFT, padx=2, pady=2)
        ttk.Button(button_row, text="Remove", style="PanelAction.TButton",
                   command=self._remove_queue_selection).pack(
            side=tk.LEFT, padx=2, pady=2)

        self.queue_tree.bind("<Double-1>", self._on_queue_double_click)
        self.queue_tree.bind("<Button-3>", self._on_queue_right_click)
        self.queue_tree.bind("<ButtonPress-1>", self._on_queue_drag_start)
        self.queue_tree.bind("<B1-Motion>", self._on_queue_drag_motion)

        self._refresh_queue_view()

    def _refresh_queue_view(self):
        """Rebuild the queue panel's rows from `self.player.queue`. Row
        iids are just the track's position (as a string) -- simple and
        sufficient since the whole tree is always fully rebuilt on any
        queue change, and duplicate tracks in the queue are otherwise not
        distinguishable by path alone. Also updates the "in_queue" marker
        on any matching rows currently shown in the playlist table."""
        self.queue_tree.delete(*self.queue_tree.get_children())
        for index, path in enumerate(self.player.queue):
            tags = self.track_tags.get(path, {})
            title = tags.get("title") or os.path.basename(path)
            artist = tags.get("artist", "")
            self.queue_tree.insert(
                "", tk.END, iid=str(index), values=(title, artist))
        self._update_queue_markers()

    def _update_queue_markers(self):
        """Add/remove the "in_queue" tag on the playlist table's rows to
        match the current contents of `self.player.queue`, without doing a
        full `_refresh_playlist_view()` (which would also re-sort/re-filter
        and be overkill for just a queue change). Preserves "now_playing"/
        "ignored" if either was already set on a row."""
        if not hasattr(self, "playlist_tree"):
            return
        queued_paths = set(self.player.queue)
        for item in self.playlist_tree.get_children():
            current_tags = self.playlist_tree.item(item, "tags")
            new_tags = []
            if "now_playing" in current_tags:
                new_tags.append("now_playing")
            if item in queued_paths:
                new_tags.append("in_queue")
            if "ignored" in current_tags:
                new_tags.append("ignored")
            self.playlist_tree.item(item, tags=tuple(new_tags))

    def _selected_queue_index(self):
        selection = self.queue_tree.selection()
        if not selection:
            return None
        try:
            return int(selection[0])
        except ValueError:
            return None

    def _move_queue_selection(self, delta):
        index = self._selected_queue_index()
        if index is None:
            return
        new_index = index + delta
        if not (0 <= new_index < len(self.player.queue)):
            return
        queue = self.player.queue
        queue[index], queue[new_index] = queue[new_index], queue[index]
        self._refresh_queue_view()
        self.queue_tree.selection_set(str(new_index))
        self.queue_tree.focus(str(new_index))

    def _remove_queue_selection(self):
        index = self._selected_queue_index()
        if index is None:
            return
        del self.player.queue[index]
        self._refresh_queue_view()

    def _clear_queue(self):
        self.player.queue.clear()
        self._refresh_queue_view()
        self.status_var.set("Cleared queue")

    def _play_queue_index(self, index):
        """Jump straight to playing the queued track at `index`, removing
        it from the queue (the rest of the queue keeps its order)."""
        if not (0 <= index < len(self.player.queue)):
            return
        path = self.player.queue.pop(index)
        self._refresh_queue_view()
        self._play_track(path, from_queue=True)

    def _on_queue_double_click(self, event):
        row = self.queue_tree.identify_row(event.y)
        if not row:
            return
        if self.browsing_mode:
            self.status_var.set(
                "Browsing Mode is on -- right-click > Play Now to play this track")
            return
        self._play_queue_index(int(row))

    def _on_queue_right_click(self, event):
        row = self.queue_tree.identify_row(event.y)
        if not row:
            return
        if row not in self.queue_tree.selection():
            self.queue_tree.selection_set(row)
        index = int(row)
        menu = tk.Menu(self.root, tearoff=0, **self._menu_colors())
        menu.add_command(label="Play Now",
                         command=lambda: self._play_queue_index(index))
        menu.add_command(label="Move Up",
                         command=lambda: self._move_queue_selection(-1))
        menu.add_command(label="Move Down",
                         command=lambda: self._move_queue_selection(1))
        menu.add_separator()
        menu.add_command(label="Remove from Queue",
                         command=self._remove_queue_selection)
        menu.add_command(
            label="Clear Selection",
            command=self._clear_queue_selection,
            state=tk.NORMAL if len(self.queue_tree.selection()) > 1 else tk.DISABLED)
        menu.add_command(label="Clear Queue", command=self._clear_queue)
        self._popup_menu(menu, event)

    def _clear_queue_selection(self):
        """Deselect whatever's currently multi-selected in the queue
        panel, without touching the queue's actual contents (Escape does
        the same thing for this and the other trees -- this is the
        discoverable menu equivalent for the queue panel specifically)."""
        selection = self.queue_tree.selection()
        if selection:
            self.queue_tree.selection_remove(*selection)

    def _on_queue_drag_start(self, event):
        row = self.queue_tree.identify_row(event.y)
        self._queue_drag_index = int(row) if row else None

    def _on_queue_drag_motion(self, event):
        if self._queue_drag_index is None:
            return
        row = self.queue_tree.identify_row(event.y)
        if not row:
            return
        target_index = int(row)
        if target_index == self._queue_drag_index:
            return
        queue = self.player.queue
        item = queue.pop(self._queue_drag_index)
        queue.insert(target_index, item)
        self._queue_drag_index = target_index
        self._refresh_queue_view()
        self.queue_tree.selection_set(str(target_index))

    # -- playlist column selection (right-click the table header) --------
    def _apply_playlist_columns(self):
        """(Re)configure the playlist Treeview's columns to match
        `self.playlist_column_visible`, then rebuild its rows so the
        values line up with the new column set."""
        visible_keys = tuple(
            key for key, _label in PLAYLIST_COLUMNS
            if self.playlist_column_visible.get(key))
        self.playlist_tree["columns"] = visible_keys
        for key, label in PLAYLIST_COLUMNS:
            if key in visible_keys:
                self.playlist_tree.heading(key, text=label)
                self.playlist_tree.column(
                    key, width=PLAYLIST_COLUMN_WIDTHS.get(key, 120), anchor=tk.W)
        self._refresh_playlist_view()

    def _show_playlist_column_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0, **self._menu_colors())
        for key, label in PLAYLIST_COLUMNS:
            var = tk.BooleanVar(
                value=self.playlist_column_visible.get(key, False))
            menu.add_checkbutton(
                label=label, variable=var,
                command=lambda k=key, v=var: self._toggle_playlist_column(k, v))
        self._popup_menu(menu, event)

    def _toggle_playlist_column(self, key, var):
        new_value = var.get()
        if not new_value:
            currently_visible = [
                k for k, visible in self.playlist_column_visible.items() if visible]
            if currently_visible == [key]:
                # Keep at least one column visible at all times.
                var.set(True)
                self.status_var.set("At least one column must stay visible")
                return
        self.playlist_column_visible[key] = new_value
        self._apply_playlist_columns()

    # -- playlist background photo (Artist/Title/Album/Duration table) ---
    def _choose_playlist_background(self):
        path = filedialog.askopenfilename(
            title="Choose a background image", filetypes=IMAGE_FILETYPES)
        if not path:
            return
        try:
            image = Image.open(path)
        except Exception as exc:
            self.status_var.set(f"Could not load image: {exc}")
            return
        self.playlist_bg_source_image = image
        self.playlist_bg_path = path
        self._apply_playlist_background()
        self.status_var.set(
            f"Set playlist background image: {os.path.basename(path)}")

    def _clear_playlist_background(self):
        self.playlist_bg_source_image = None
        self.playlist_bg_path = None
        if self._playlist_bg_style_installed:
            self.playlist_tree.configure(style="Treeview")
        self.status_var.set("Cleared playlist background image")

    def _on_playlist_tree_configure(self, _event):
        if self.playlist_bg_source_image is not None:
            self._apply_playlist_background()

    def _apply_playlist_background(self):
        # Guard against measuring a stale/transitional size (e.g. right
        # after a batch of ttk.Style changes, before Tk has finished
        # recomputing geometry) -- that would bake the photo in at the
        # wrong (tiny) size until the next real resize.
        self.root.update_idletasks()
        width = self.playlist_tree.winfo_width()
        height = self.playlist_tree.winfo_height()
        if width <= 1 or height <= 1:
            return
        style = ttk.Style()
        base_color = style.lookup("Treeview", "fieldbackground") or "white"
        base_rgb = self._rgb_of(base_color)
        # "Cover" fit: scale the image up to fill the entire table with no
        # gaps, cropping any excess from the center.
        fitted = fit_image_cover(
            self.playlist_bg_source_image, (width, height))
        faded = apply_low_opacity(
            fitted, opacity=0.2, base_color=base_rgb)

        # Note: ImageTk.PhotoImage.paste() does NOT resize the underlying
        # Tk image, so reusing the same PhotoImage object across a size
        # change (e.g. the treeview resizing, or a theme switch nudging
        # its layout) would leave the background baked in at the old,
        # smaller size ("shrinking" to a corner). To avoid that, always
        # build a fresh PhotoImage and register it under a new, uniquely
        # named ttk element instead of mutating the old one in place.
        self._playlist_bg_element_counter = getattr(
            self, "_playlist_bg_element_counter", 0) + 1
        element_name = f"Playlist.field{self._playlist_bg_element_counter}"
        self.playlist_bg_photo = ImageTk.PhotoImage(faded)

        style.element_create(
            element_name, "image", self.playlist_bg_photo, sticky="nsew")
        style.layout("Playlist.Treeview", [
            (element_name, {"sticky": "nsew", "children": [
                ("Treeview.padding", {"sticky": "nsew", "children": [
                    ("Treeview.treearea", {"sticky": "nsew"}),
                ]}),
            ]}),
        ])
        self.playlist_tree.configure(style="Playlist.Treeview")
        self._playlist_bg_style_installed = True

    # -- status bar -----------------------------------------------------
    def _build_status_bar(self):
        self.status_var = tk.StringVar(value="Ready")
        self.status_bar_label = ttk.Label(
            self.root, textvariable=self.status_var, anchor=tk.W,
            style="StatusBar.TLabel")
        self.status_bar_label.pack(side=tk.BOTTOM, fill=tk.X)
        # Every status_var.set(...) call anywhere in the app automatically
        # gets this fade-to-background animation, so no individual call
        # site needs to know about it.
        self.status_var.trace_add("write", self._on_status_changed)

    def _on_status_changed(self, *_args):
        self._animate_color_fade(
            self.status_bar_label,
            lambda color: self.status_bar_label.configure(foreground=color),
            self.palette["fg"], self.palette["bg"],
            delay_ms=2500, total_steps=20, step_ms=40)

    # -- file/folder actions ---------------------------------------------
    def open_files(self):
        paths = filedialog.askopenfilenames(
            title="Select music file(s) or album archive(s)", filetypes=MUSIC_FILETYPES)
        added = 0
        for path in paths:
            if looks_like_archive(path):
                self.import_album_archive(path)
            else:
                self._add_track(path)
                added += 1
        if added:
            self.status_var.set(f"Added {added} file(s)")

    def open_archive(self):
        paths = filedialog.askopenfilenames(
            title="Select album archive(s)",
            filetypes=[("Zip archives", "*.zip"), ("All files", "*.*")],
        )
        for path in paths:
            self.import_album_archive(path)

    def open_folder(self):
        path = filedialog.askdirectory(title="Select folder")
        if not path:
            return
        self.player.library_roots.append(path)
        self.library_tree.insert(
            "", tk.END, iid=path, text=os.path.basename(path) or path, open=True)
        self._start_library_scan(path)

    # -- background library scanning (folders + tag reads off the UI
    # thread, so opening/restoring a large library doesn't freeze the
    # window) -----------------------------------------------------------
    def _start_library_scan(self, dirpath, announce=True, log=True):
        """Kick off a background thread that walks `dirpath` and reads
        every track's tags, feeding results back to the main thread via
        `self._library_scan_queue` (drained by `_drain_library_scan_queue`,
        polled with `root.after`). `dirpath` itself must already exist as
        a node in the library tree (its iid IS its path, per the
        existing convention) -- only its descendants are added here.
        `log=True` records this folder's tracks (Album/Artist/Year) to
        the Excel library log once the scan finishes (see
        _log_scan_to_excel) -- pass `log=False` for scans that AREN'T a
        genuinely new folder the user just chose (e.g. re-scanning
        already-known roots on launch), so the log doesn't get a
        duplicate entry for the same folder every single session."""
        scan_id = self._library_scan_next_id
        self._library_scan_next_id += 1
        self._library_scans[scan_id] = {
            "path": dirpath, "total": 0, "done": 0, "announce": announce,
            "log": log, "log_entries": [],
        }
        self._library_scan_active += 1
        self._show_loading_bar()

        thread = threading.Thread(
            target=self._library_scan_worker, args=(scan_id, dirpath),
            daemon=True)
        thread.start()

        if self._library_scan_drain_job is None:
            self._library_scan_drain_job = self.root.after(
                15, self._drain_library_scan_queue)

    def _library_scan_worker(self, scan_id, dirpath):
        """Runs in a background thread -- MUST NOT touch any Tk widget or
        `self.player`/`self.track_tags` directly (not thread-safe); only
        ever communicates back via the thread-safe `self._library_scan_queue`."""
        put = self._library_scan_queue.put

        total = 0
        for _root, _dirs, files in os.walk(dirpath):
            total += sum(1 for f in files if f.lower().endswith(MUSIC_EXTENSIONS))
        put(("total", scan_id, total))

        def walk(parent_dir):
            try:
                entries = sorted(
                    os.scandir(parent_dir), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError:
                return
            for entry in entries:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if is_dir:
                    put(("dir", scan_id, parent_dir, entry.path, entry.name))
                    walk(entry.path)
                elif entry.name.lower().endswith(MUSIC_EXTENSIONS):
                    tags = read_all_track_tags(entry.path)
                    put(("file", scan_id, parent_dir, entry.path, tags))

        walk(dirpath)
        put(("scan_done", scan_id))

    def _drain_library_scan_queue(self):
        """Runs on the main/UI thread (scheduled via `root.after`): applies
        a batch of results pushed by any active `_library_scan_worker`
        thread(s) to the library tree/playlist/loading bar, then
        reschedules itself until every scan has finished AND the queue is
        empty.

        The per-tick batch size is capped fairly low (rather than
        draining everything available in one shot) ON PURPOSE: the
        background scan thread(s) can easily outrun the few hundred
        milliseconds it takes just to build the rest of the UI at
        startup, so by the time the window draws its first real frame
        the whole scan may already be sitting fully-queued/finished --
        without a cap, that first drain call would swallow the entire
        queue AND hide the loading bar in that same tick, so the bar
        would never actually become visible to the user even though the
        scan genuinely did happen in the background. Capping the batch
        forces several `root.after` ticks (i.e. several rendered frames)
        no matter how fast the scan itself was, so the loading bar/
        progress is actually seen."""
        processed = 0
        try:
            while processed < 40:
                item = self._library_scan_queue.get_nowait()
                processed += 1
                kind = item[0]
                if kind == "file":
                    _, scan_id, parent_id, entry_path, tags = item
                    if not self.library_tree.exists(entry_path):
                        self.library_tree.insert(
                            parent_id, tk.END, iid=entry_path,
                            text=os.path.basename(entry_path), open=False)
                    self._register_scanned_track(entry_path, tags)
                    self._apply_library_ignored_mark(entry_path)
                    scan = self._library_scans.get(scan_id)
                    if scan is not None:
                        scan["done"] += 1
                        if scan["log"]:
                            scan["log_entries"].append((
                                tags.get("album", ""), tags.get("artist", ""),
                                tags.get("date", "")))
                elif kind == "dir":
                    _, _scan_id, parent_id, entry_path, entry_name = item
                    if not self.library_tree.exists(entry_path):
                        self.library_tree.insert(
                            parent_id, tk.END, iid=entry_path,
                            text=entry_name, open=False)
                elif kind == "total":
                    _, scan_id, total = item
                    scan = self._library_scans.get(scan_id)
                    if scan is not None:
                        scan["total"] = total
                elif kind == "scan_done":
                    _, scan_id = item
                    self._library_scan_active -= 1
                    scan = self._library_scans.pop(scan_id, None)
                    if scan is not None:
                        if scan["announce"]:
                            noun = "file" if scan["done"] == 1 else "files"
                            self.status_var.set(
                                f"Added {scan['done']} {noun} from {scan['path']}")
                        if scan["log"] and scan["log_entries"]:
                            self._log_scan_to_excel(
                                scan["path"], scan["log_entries"])
        except queue.Empty:
            pass

        self._update_loading_bar()

        if self._library_scan_active > 0 or not self._library_scan_queue.empty():
            self._library_scan_drain_job = self.root.after(
                15, self._drain_library_scan_queue)
        else:
            self._library_scan_drain_job = None
            self._hide_loading_bar()

    def _update_loading_bar(self):
        if not hasattr(self, "loading_progress"):
            return
        total = sum(s["total"] for s in self._library_scans.values())
        done = sum(s["done"] for s in self._library_scans.values())
        total = max(total, done, 1)
        self.loading_progress.configure(maximum=total)
        self.loading_progress["value"] = done
        self.loading_bar_var.set(f"Loading library... {done}/{total}")

    def _show_loading_bar(self):
        if not hasattr(self, "loading_frame"):
            return
        if not self.loading_frame.winfo_ismapped():
            self.loading_frame.pack(
                side=tk.LEFT, fill=tk.X, expand=True, padx=10,
                before=self.volume_label)
        self._update_loading_bar()

    def _hide_loading_bar(self):
        if not hasattr(self, "loading_frame"):
            return
        self.loading_frame.pack_forget()

    # -- Excel "library log" (records newly chosen folders' tracks) ------
    def choose_library_log_path(self):
        """File menu action: (re)choose where the "chosen folders" Excel
        log is saved. Lets the user set one up proactively, or move/
        replace an existing one, rather than only ever being asked the
        first time a folder happens to get added."""
        path = filedialog.asksaveasfilename(
            title="Choose Library Log File", defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx"), ("All files", "*.*")],
            initialfile=os.path.basename(self.library_log_path)
            if self.library_log_path else "library_log.xlsx")
        if not path:
            return
        self.library_log_path = path
        self._library_log_prompt_declined = False
        self.status_var.set(f"Library log will be saved to: {path}")

    def _ensure_library_log_path(self):
        """Return the path to log "chosen folder" entries to, prompting
        the user to choose one THE FIRST TIME it's needed and reusing it
        afterward (see excel_log.py's module docstring for the file
        format). Returns None if no path is set and the user
        cancels/declines the prompt -- logging is then skipped silently
        for that scan (a repeated prompt on every single folder-add
        would get old fast; use File > Set Library Log File... to set
        one up later if they change their mind this session)."""
        if self.library_log_path:
            return self.library_log_path
        if self._library_log_prompt_declined:
            return None
        path = filedialog.asksaveasfilename(
            title="Choose where to save the library log (Excel file)",
            defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx"), ("All files", "*.*")],
            initialfile="library_log.xlsx")
        if not path:
            self._library_log_prompt_declined = True
            return None
        self.library_log_path = path
        return path

    def _log_scan_to_excel(self, folder_path, entries):
        """Append `entries` (a list of (album, artist, year) tuples, one
        per track found) for `folder_path` to the Excel library log,
        prompting for its location the first time it's needed (see
        _ensure_library_log_path). No-ops silently if the user hasn't
        set one up / declines when asked."""
        log_path = self._ensure_library_log_path()
        if not log_path:
            return
        try:
            append_folder_log(log_path, folder_path, entries)
        except Exception as exc:
            self.status_var.set(f"Could not write library log: {exc}")

    def log_existing_library(self):
        """File menu action: log every ALREADY-ADDED library folder's
        tracks to the Excel library log in one go -- for folders that
        were added before this feature existed (or before a log path
        was chosen), which otherwise never get an entry since only
        NEWLY added folders are logged automatically."""
        deduped_roots = self._deduped_library_roots()
        if not deduped_roots:
            self.status_var.set("No library folders to log")
            return

        log_path = self._ensure_library_log_path()
        if not log_path:
            self.status_var.set("Cancelled: no library log file chosen")
            return

        # Group every currently-loaded track under whichever of its
        # (deduped, outermost) library roots it actually lives under, so
        # a track isn't logged twice if one root is nested inside
        # another and both ended up in library_roots.
        roots_by_length = sorted(deduped_roots, key=len, reverse=True)
        entries_by_root = {root: [] for root in deduped_roots}
        for path in self.player.playlist:
            for root_dir in roots_by_length:
                root_with_sep = root_dir.rstrip(os.sep) + os.sep
                if path == root_dir or path.startswith(root_with_sep):
                    tags = self.track_tags.get(path, {})
                    entries_by_root[root_dir].append(
                        (tags.get("album", ""), tags.get("artist", ""),
                         tags.get("date", "")))
                    break

        logged_folders = 0
        logged_tracks = 0
        for root_dir, entries in entries_by_root.items():
            if not entries:
                continue
            try:
                append_folder_log(log_path, root_dir, entries)
            except Exception as exc:
                self.status_var.set(f"Could not write library log: {exc}")
                return
            logged_folders += 1
            logged_tracks += len(entries)

        if logged_folders:
            self.status_var.set(
                f"Logged {logged_tracks} track(s) from {logged_folders} "
                f"existing folder(s) to the library log")
        else:
            self.status_var.set(
                "No tracks found in existing library folders to log")

    def import_album_archive(self, zip_path):
        """Unzip an album archive and file its tracks under
        '<source folder>/<Artist>/<Year> - <Album>/', merging into any
        existing artist folder that already exists at that location.
        """
        if not zipfile.is_zipfile(zip_path):
            self.status_var.set(
                f"'{os.path.basename(zip_path)}' is not a valid (or fully downloaded) zip archive")
            return

        dest_root = os.path.dirname(zip_path) or "."
        temp_dir = tempfile.mkdtemp(prefix="album_import_")
        try:
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    archive.extractall(temp_dir)
            except Exception as exc:
                self.status_var.set(
                    f"Failed to extract {os.path.basename(zip_path)}: {exc}")
                return

            audio_files = []
            for dirpath, _dirnames, filenames in os.walk(temp_dir):
                for name in filenames:
                    if name.lower().endswith(MUSIC_EXTENSIONS):
                        audio_files.append(os.path.join(dirpath, name))

            if not audio_files:
                self.status_var.set(
                    f"No music files found inside {os.path.basename(zip_path)}")
                return

            filename_album, filename_artist = parse_album_zip_name(zip_path)
            artist, album, year = self._detect_album_info(
                audio_files, filename_artist, filename_album)

            artist_dir = os.path.join(dest_root, sanitize_filename(artist))
            # merges if it already exists
            os.makedirs(artist_dir, exist_ok=True)

            album_folder_name = sanitize_filename(
                f"{year} - {album}" if year else album)
            album_dir = os.path.join(artist_dir, album_folder_name)
            os.makedirs(album_dir, exist_ok=True)

            moved = 0
            for src in audio_files:
                dest = self._unique_destination(
                    os.path.join(album_dir, os.path.basename(src)))
                shutil.move(src, dest)
                moved += 1

            self._add_library_folder(artist_dir, announce=False)
            self.status_var.set(
                f"Imported {moved} track(s) into {os.path.relpath(album_dir, dest_root)}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _detect_album_info(self, audio_files, filename_artist, filename_album):
        artists, albums, years = Counter(), Counter(), Counter()
        for path in audio_files:
            tags = read_common_tags(path)
            if tags.get("artist"):
                artists[tags["artist"]] += 1
            if tags.get("album"):
                albums[tags["album"]] += 1
            match = re.search(r"\d{4}", tags.get("date", "") or "")
            if match:
                years[match.group(0)] += 1

        artist = artists.most_common(1)[0][0] if artists else (
            filename_artist or "Unknown Artist")
        album = albums.most_common(1)[0][0] if albums else (
            filename_album or "Unknown Album")
        year = years.most_common(1)[0][0] if years else ""
        return artist, album, year

    def _unique_destination(self, dest_path):
        if not os.path.exists(dest_path):
            return dest_path
        root, ext = os.path.splitext(dest_path)
        counter = 1
        candidate = f"{root} ({counter}){ext}"
        while os.path.exists(candidate):
            counter += 1
            candidate = f"{root} ({counter}){ext}"
        return candidate

    def _add_library_folder(self, dirpath, announce=True, log=True):
        """Add or refresh `dirpath` as a top-level node in the library tree
        and merge any newly found tracks into the playlist (scanned in
        the background -- see _start_library_scan). `log=False` skips
        recording this folder to the Excel library log (used when
        re-adding an already-known folder on launch -- see
        _restore_from_cache)."""
        is_new = dirpath not in self.player.library_roots
        if self.library_tree.exists(dirpath):
            # Already present in the tree (either as a previously-added
            # root, or as a folder discovered while populating another
            # root it happens to be nested inside) -- just refresh its
            # contents instead of trying to insert a duplicate node.
            self.library_tree.delete(*self.library_tree.get_children(dirpath))
        else:
            self.library_tree.insert(
                "", tk.END, iid=dirpath, text=os.path.basename(dirpath) or dirpath, open=True)

        if is_new:
            self.player.library_roots.append(dirpath)

        self._start_library_scan(dirpath, announce=announce, log=log)

    def _add_track(self, path):
        if path in self.player.playlist:
            return
        self._register_scanned_track(path, self._read_all_track_tags(path))

    def _register_scanned_track(self, path, tags):
        """Like _add_track, but takes already-computed `tags` instead of
        reading them from disk itself -- used by the background library
        scanner (see _library_scan_worker/_drain_library_scan_queue),
        which reads each file's tags off the main thread so scanning a
        large library doesn't block the UI. No-ops if `path` is already
        in the playlist (mirrors _add_track's existing behavior of never
        overwriting an already-loaded track's tags)."""
        if path in self.player.playlist:
            return
        self.player.playlist.append(path)
        self.track_tags[path] = tags
        if self._track_passes_filter(path):
            if self.playlist_sort_key:
                # A column sort is active: re-sort so the new track lands
                # in the right spot instead of just appending at the end.
                self._refresh_playlist_view()
            else:
                self.playlist_tree.insert(
                    "", tk.END, iid=path, values=self._row_values(path),
                    tags=self._row_tags(path))

    def _read_all_track_tags(self, path):
        """Read every metadata field the playlist table can show for
        `path` (artist/title/album/duration plus the optional columns:
        album artist, genre, year, track #, disc #, BPM), in a single
        file open/parse (see audio_tags.read_all_track_tags)."""
        return read_all_track_tags(path)

    def _row_values(self, path):
        tags = self.track_tags.get(path, {})
        values = []
        for key in self.playlist_tree["columns"]:
            value = tags.get(key, "")
            if key == "title" and path in self.player.ignored:
                value = f"\u2717 {value}" if value else "\u2717"
            values.append(value)
        return tuple(values)

    def _row_tags(self, path):
        """The full set of display tags for `path`'s playlist row: which
        of "now_playing"/"in_queue"/"ignored" currently apply. Listed in
        this priority order (now_playing first) since ttk resolves a
        conflicting style option -- e.g. foreground -- from whichever tag
        appears FIRST in the row's tags tuple."""
        tags = []
        if path == self._current_playing_path():
            tags.append("now_playing")
        if path in self.player.queue:
            tags.append("in_queue")
        if path in self.player.ignored:
            tags.append("ignored")
        return tuple(tags)

    def _track_passes_filter(self, path):
        if not self._matches_search(path):
            return False
        if self.current_filter is None:
            return True
        kind, value = self.current_filter
        if kind == "paths":
            return path in value
        tags = self.track_tags.get(path, {})
        return tags.get(kind, "").strip().lower() == value.strip().lower()

    def _refresh_playlist_view(self):
        self.playlist_tree.delete(*self.playlist_tree.get_children())
        paths = [
            p for p in self.player.playlist if self._track_passes_filter(p)]
        for path in self._sorted_paths(paths):
            self.playlist_tree.insert(
                "", tk.END, iid=path, values=self._row_values(path),
                tags=self._row_tags(path))

    def _current_playing_path(self):
        index = self.player.current_index
        if index is not None and 0 <= index < len(self.player.playlist):
            return self.player.playlist[index]
        return None

    def _highlight_now_playing_row(self, path):
        """Mark `path`'s row in the playlist table with a contrasting
        highlight if it's currently visible in the table (i.e. not
        filtered out by the active view/filter). Any other row is cleared
        of the "now_playing" tag first (since only one track can be "now
        playing" at a time), while preserving its "in_queue"/"ignored"
        tags if it has them. "now_playing" is listed first so it takes
        priority over the others for styling if a row has more than one.
        The highlight's background fades smoothly in rather than
        appearing instantly."""
        for item in self.playlist_tree.get_children():
            current_tags = self.playlist_tree.item(item, "tags")
            new_tags = []
            if item == path:
                new_tags.append("now_playing")
            if "in_queue" in current_tags:
                new_tags.append("in_queue")
            if "ignored" in current_tags:
                new_tags.append("ignored")
            self.playlist_tree.item(item, tags=tuple(new_tags))
        if path is not None and self.playlist_tree.exists(path):
            self.playlist_tree.tag_configure(
                "now_playing", foreground=self.palette["highlight_fg"])
            self._animate_color_fade(
                "now_playing_tag",
                lambda color: self.playlist_tree.tag_configure(
                    "now_playing", background=color),
                self.palette["field_bg"], self.palette["highlight_bg"],
                total_steps=8, step_ms=25)

    def _sorted_paths(self, paths):
        """Apply the current column sort (set by double-clicking a column
        header) to `paths` for display. This ordering is session-only and
        never written to the cache. Sorting by Album additionally orders
        each album's songs by track number (always ascending), regardless
        of which direction the album grouping itself is sorted in."""
        key = self.playlist_sort_key
        if not key:
            return paths
        reverse = self.playlist_sort_reverse

        if key == "album":
            by_track = sorted(
                paths,
                key=lambda p: self._sort_value_numeric(
                    self.track_tags.get(p, {}).get("tracknumber", "")))
            return sorted(
                by_track,
                key=lambda p: self._sort_value_text(
                    self.track_tags.get(p, {}).get("album", "")),
                reverse=reverse)

        if key in ("bpm", "tracknumber", "discnumber", "date"):
            return sorted(
                paths,
                key=lambda p: self._sort_value_numeric(
                    self.track_tags.get(p, {}).get(key, "")),
                reverse=reverse)

        if key == "duration":
            return sorted(
                paths,
                key=lambda p: self._sort_value_duration(
                    self.track_tags.get(p, {}).get(key, "")),
                reverse=reverse)

        return sorted(
            paths,
            key=lambda p: self._sort_value_text(
                self.track_tags.get(p, {}).get(key, "")),
            reverse=reverse)

    @staticmethod
    def _sort_value_text(value):
        return (value or "").strip().lower()

    @staticmethod
    def _sort_value_numeric(value):
        """Extract a leading number from `value` for sorting (handles
        e.g. track numbers written as "3/12"). Missing/unparseable
        values sort to the end."""
        match = re.search(r"-?\d+(\.\d+)?", value or "")
        if not match:
            return float("inf")
        try:
            return float(match.group(0))
        except ValueError:
            return float("inf")

    @staticmethod
    def _sort_value_duration(value):
        """Parse a formatted duration ("M:SS" or "H:MM:SS") back into
        seconds for sorting. Missing/unparseable values sort to the end."""
        if not value:
            return float("inf")
        try:
            parts = [int(p) for p in value.split(":")]
        except ValueError:
            return float("inf")
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds

    def show_full_playlist(self):
        self.current_filter = None
        self._refresh_playlist_view()
        self._clear_library_highlight()
        self.status_var.set("Showing full playlist")

    def _filter_by(self, kind, path):
        value = self.track_tags.get(path, {}).get(kind, "")
        if not value:
            self.status_var.set(f"No {kind} tag found for this track")
            return
        self.current_filter = (kind, value)
        self._refresh_playlist_view()
        matched_paths = [
            p for p in self.player.playlist if self._track_passes_filter(p)]
        self._highlight_library_folders(matched_paths)
        self.status_var.set(f"Filtered by {kind}: {value}")

    def _clear_library_highlight(self):
        for item in self._library_highlighted_items:
            if self.library_tree.exists(item):
                self.library_tree.item(item, tags=())
        self._library_highlighted_items = []

    def _highlight_library_folders(self, paths):
        """Expand and mark (with a contrasting color) the library tree
        folder(s) containing `paths`, so it's obvious at a glance where
        the current album/artist filter's tracks live on disk. The
        highlight's background fades smoothly in rather than appearing
        instantly."""
        self._clear_library_highlight()

        folders = []
        seen = set()
        for path in paths:
            folder = os.path.dirname(path)
            if folder not in seen and self.library_tree.exists(folder):
                seen.add(folder)
                folders.append(folder)

        for folder in folders:
            self.library_tree.item(folder, open=True, tags=("filter_match",))
            self._library_highlighted_items.append(folder)
            # Expand every ancestor too, so the highlighted folder is
            # actually visible instead of being collapsed out of sight.
            ancestor = self.library_tree.parent(folder)
            while ancestor:
                self.library_tree.item(ancestor, open=True)
                ancestor = self.library_tree.parent(ancestor)

        if folders:
            self.library_tree.tag_configure(
                "filter_match", foreground=self.palette["highlight_fg"])
            self._animate_color_fade(
                "filter_match_tag",
                lambda color: self.library_tree.tag_configure(
                    "filter_match", background=color),
                self.palette["field_bg"], self.palette["highlight_bg"],
                total_steps=8, step_ms=25)
            self.library_tree.see(folders[0])

    def remove_selected(self):
        for path in self.playlist_tree.selection():
            self._remove_track(path)

    def _remove_track(self, path, remove_from_library=True):
        if self.playlist_tree.exists(path):
            self.playlist_tree.delete(path)
        if remove_from_library and self.library_tree.exists(path):
            self.library_tree.delete(path)
        if path in self.player.playlist:
            self.player.playlist.remove(path)
        self.track_tags.pop(path, None)
        self.player.ignored.discard(path)
        self._apply_library_ignored_mark(path)
        self.player.queue = [p for p in self.player.queue if p != path]
        self._refresh_queue_view()

    def clear_playlist(self):
        # Only empties the playlist table/queue -- the library tree (your
        # imported folders) is left untouched, so tracks are still there
        # to re-add (e.g. via double-click) without re-importing folders.
        for path in list(self.player.playlist):
            self._remove_track(path, remove_from_library=False)
        self.status_var.set("Cleared playlist (library folders kept)")

    # -- shared context menu / double-click (playlist + library tree) ----
    def _collect_audio_paths(self, tree, item_id):
        """Resolve a tree item to the list of underlying track paths it
        represents: itself if it's a track (leaf), or all descendant tracks
        if it's a folder/album/playlist node. A library-tree leaf whose
        track was previously removed from the playlist (e.g. via "Clear
        Playlist", which keeps the library tree intact) is transparently
        re-added so it's playable again without re-importing the whole
        folder -- the same applies to a playlist's tracks."""
        if not tree.exists(item_id):
            return []
        children = tree.get_children(item_id)
        if not children:
            playlist_info = self._playlist_track_info.get(item_id)
            if playlist_info is not None:
                _cue_path, real_path = playlist_info
                if real_path not in self.player.playlist and os.path.isfile(real_path):
                    self._add_track(real_path)
                return [real_path] if real_path in self.player.playlist else []
            if item_id in self.player.playlist:
                return [item_id]
            if (tree is self.library_tree and os.path.isfile(item_id)
                    and item_id.lower().endswith(MUSIC_EXTENSIONS)):
                self._add_track(item_id)
                return [item_id]
            return []
        paths = []
        for child in children:
            paths.extend(self._collect_audio_paths(tree, child))
        return paths

    def _on_tree_double_click(self, event):
        tree = event.widget
        if (tree is self.playlist_tree
                and tree.identify_region(event.x, event.y) == "heading"):
            # Heading clicks are handled on a single click now (see
            # _on_playlist_heading_click); nothing extra to do here.
            return
        item = tree.identify_row(event.y)
        if not item:
            return
        folder_name = self._folder_like_label(tree, item)
        if self.browsing_mode:
            # Browsing Mode: double-click never starts playback -- just
            # view a folder's tracklist (or, for a single track, do
            # nothing beyond the normal single-click selection). Use
            # the right-click menu's "Play"/"Play Now" to actually start
            # something while browsing.
            paths = self._collect_audio_paths(tree, item)
            if folder_name:
                self._view_folder(paths, folder_name)
            else:
                self.status_var.set(
                    "Browsing Mode is on -- right-click > Play to play this track")
            return
        if folder_name:
            self._show_viewing_folder_label(folder_name)
        self._play_paths(self._collect_audio_paths(tree, item))

    def _folder_like_label(self, tree, item):
        """The display name to show in the "Currently viewing folder: ..."
        label for `item`, if it's a folder-like node worth announcing
        (a real library folder, a playlist, or the "Playlists" root) --
        i.e. anything with children -- otherwise None (a plain track)."""
        if tree is not self.library_tree or not tree.get_children(item):
            return None
        if item == self._playlists_root_id:
            return tree.item(item, "text") or "Playlists"
        playlist_info = next(
            (info for info in self.playlists.values()
             if info["node_id"] == item), None)
        if playlist_info is not None:
            return playlist_info["name"]
        if os.path.isdir(item):
            return tree.item(item, "text") or item
        return None

    def _on_playlist_heading_click(self, event):
        """A single left-click on a playlist column header sorts by that
        column (was previously a double-click)."""
        if self.playlist_tree.identify_region(event.x, event.y) != "heading":
            return
        column_id = self.playlist_tree.identify_column(event.x)
        if not column_id:
            return
        try:
            index = int(column_id.replace("#", "")) - 1
        except ValueError:
            return
        columns = self.playlist_tree["columns"]
        if not (0 <= index < len(columns)):
            return
        self._sort_playlist_by(columns[index])

    def _sort_playlist_by(self, key):
        if self.playlist_sort_key == key:
            self.playlist_sort_reverse = not self.playlist_sort_reverse
        else:
            self.playlist_sort_key = key
            self.playlist_sort_reverse = False
        self._refresh_playlist_view()
        label = dict(PLAYLIST_COLUMNS).get(key, key)
        direction = "descending" if self.playlist_sort_reverse else "ascending"
        self.status_var.set(f"Sorted by {label} ({direction})")

    def _on_tree_right_click(self, event):
        tree = event.widget
        if tree is self.playlist_tree and tree.identify_region(event.x, event.y) == "heading":
            self._show_playlist_column_menu(event)
            return
        item = tree.identify_row(event.y)
        if not item:
            return
        if item not in tree.selection():
            tree.selection_set(item)
        if tree is self.library_tree:
            self._show_library_context_menu(event, item)
        else:
            self._show_context_menu(event, tree, item)

    def _on_escape_clear_selection(self, _event=None):
        """Escape key: clear a multi-track (or single-track) selection in
        whichever of the playlist table/library tree/queue panel
        currently has one, so a batch action (queue/remove/bulk edit/...)
        can be backed out of without clicking away first."""
        for tree in (self.playlist_tree, self.library_tree, self.queue_tree):
            if tree.selection():
                tree.selection_remove(*tree.selection())

    def _play_paths(self, paths):
        """Play the first path immediately. If more than one path is given
        (e.g. double-clicking an album/folder), scope the playlist view to
        exactly this set of tracks -- the same way the "View" action does
        -- so Next/Previous naturally continues through the rest of them
        in order, WITHOUT touching the real queue. The queue is a separate,
        user-driven thing (via "Add to Queue"/the queue panel) and should
        only ever show tracks actually queued, not whatever plays next."""
        if not paths:
            return
        if len(paths) > 1:
            self.current_filter = ("paths", frozenset(paths))
            self._refresh_playlist_view()
        self._play_track(paths[0])

    def _show_context_menu(self, event, tree, item):
        primary_paths = self._collect_audio_paths(tree, item)

        selected_paths = []
        for sel in tree.selection():
            for path in self._collect_audio_paths(tree, sel):
                if path not in selected_paths:
                    selected_paths.append(path)

        menu = self._build_context_menu(primary_paths, selected_paths)
        self._popup_menu(menu, event)

    def _show_library_context_menu(self, event, item):
        """Right-click menu for the library (left) tree. Adds a "View"
        entry (shows this folder/track's songs in the playlist table on
        the right, exactly like playing it would, but WITHOUT starting
        playback), and then one of:
          - folders: "View in File Manager" + "Remove Folder" (drops it
            and everything below it from the library tree/playlist --
            doesn't touch anything on disk).
          - a playlist's own node: "Rename Playlist..." + "Remove
            Playlist" (deletes its .cue file; doesn't touch the audio
            files it referenced).
          - a track that lives inside a playlist: "Remove from
            Playlist" (removes just that one track from the playlist).
          - a plain file: "View Containing Folder".
        ...on top of the shared Play/Queue/etc menu."""
        paths = self._collect_audio_paths(self.library_tree, item)
        menu = self._build_context_menu(
            paths, paths, include_filters=False, exclude_ignored_from_queue=True)

        folder_name = self._folder_like_label(self.library_tree, item)
        menu.insert_command(
            0, label="View", state=tk.NORMAL if paths else tk.DISABLED,
            command=lambda: self._view_folder(paths, folder_name))

        # A playlist ROOT node's iid is "playlist::<cue_path>"; look it up
        # via its stored node_id rather than treating `item` as a cue_path.
        owning_cue_path = next(
            (cp for cp, info in self.playlists.items()
             if info["node_id"] == item), None)
        track_owner = self._playlist_track_info.get(item)
        if owning_cue_path is not None:
            menu.insert_separator(1)
            menu.insert_command(
                2, label="Rename Playlist...",
                command=lambda: self.rename_playlist(owning_cue_path))
            menu.insert_command(
                3, label="Set Mother Playlist...",
                command=lambda: self.set_playlist_parent(owning_cue_path))
            next_index = 4
            if self.playlists.get(owning_cue_path, {}).get("parent"):
                menu.insert_command(
                    next_index, label="Clear Mother Playlist",
                    command=lambda: self._apply_playlist_parent(owning_cue_path, None))
                next_index += 1
            menu.insert_command(
                next_index, label="Remove Playlist",
                command=lambda: self.remove_playlist(owning_cue_path))
        elif track_owner is not None:
            owner_cue_path, _real_path = track_owner
            menu.insert_separator(1)
            menu.insert_command(
                2, label="Remove from Playlist",
                command=lambda: self.remove_track_from_playlist(owner_cue_path, item))
        elif os.path.isdir(item):
            menu.insert_command(
                1, label="View in File Manager",
                command=lambda: self._open_in_file_manager(item))
            menu.insert_separator(2)
            menu.insert_command(
                3, label="Remove Folder",
                command=lambda: self._remove_library_folder(item))
            menu.insert_separator(4)
        elif os.path.isfile(item):
            menu.insert_command(
                1, label="View Containing Folder",
                command=lambda: self._open_in_file_manager(os.path.dirname(item)))
            menu.insert_separator(2)
        else:
            menu.insert_separator(1)

        self._popup_menu(menu, event)

    def _remove_library_folder(self, dirpath):
        """Remove `dirpath` and every folder/track below it from the
        library tree and the playlist. Purely an in-app removal -- the
        files themselves are left untouched on disk."""
        if not self.library_tree.exists(dirpath):
            return
        paths = self._collect_audio_paths(self.library_tree, dirpath)
        for path in paths:
            # The library tree node for each track is removed below, all
            # at once, by deleting the folder's subtree -- no need to
            # delete each leaf individually here.
            self._remove_track(path, remove_from_library=False)

        prefix = dirpath.rstrip(os.sep) + os.sep
        self.player.library_roots = [
            r for r in self.player.library_roots
            if r != dirpath and not r.startswith(prefix)]

        self._library_highlighted_items = [
            item for item in self._library_highlighted_items
            if item != dirpath and not item.startswith(prefix)]

        self.library_tree.delete(dirpath)
        self.status_var.set(
            f"Removed folder: {os.path.basename(dirpath) or dirpath}")

    # -- cue-sheet-backed playlists ---------------------------------------
    def create_playlist(self, initial_tracks=None):
        """Prompt for a name and create a new (optionally pre-filled)
        playlist, backed by a new .cue file under PLAYLISTS_DIR. Starts
        with no "mother" playlist set (use set_playlist_parent after the
        fact to link it under one)."""
        name = simpledialog.askstring(
            "New Playlist", "Playlist name:", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        cue_path = unique_cue_path(name)
        write_cue_playlist(
            cue_path, name, initial_tracks or [], track_tags=self.track_tags)
        self._load_playlist_from_cue(cue_path)

    def import_cue_playlist(self):
        """Import an existing .cue file as a new playlist. The tracks it
        references are copied into a NEW cue file managed by the app
        (under PLAYLISTS_DIR) rather than editing the original in place,
        so importing never modifies a file outside the app's control.
        Any referenced tracks that no longer exist on disk are skipped.
        Any "mother" playlist link the source file had is NOT carried
        over (it would point outside this app's managed playlists, or to
        a stale/unrelated file) -- use set_playlist_parent afterward if
        needed."""
        src_path = filedialog.askopenfilename(
            title="Import cue playlist",
            filetypes=[("Cue sheets", "*.cue"), ("All files", "*.*")])
        if not src_path:
            return
        try:
            name, track_paths, _src_parent = read_cue_playlist(src_path)
        except OSError as exc:
            self.status_var.set(f"Could not read cue file: {exc}")
            return

        existing_tracks = [p for p in track_paths if os.path.isfile(p)]
        missing = len(track_paths) - len(existing_tracks)

        cue_path = unique_cue_path(name)
        write_cue_playlist(
            cue_path, name, existing_tracks, track_tags=self.track_tags)
        self._load_playlist_from_cue(cue_path)
        if missing:
            self.status_var.set(
                f"Imported playlist '{name}': {len(existing_tracks)} track(s), "
                f"{missing} missing file(s) skipped")

    def _load_playlist_from_cue(self, cue_path, is_restore=False):
        """(Re)read `cue_path` from disk and (re)build its node/tracks in
        the library tree. Used for newly created/imported playlists, and
        to restore previously loaded ones from the session cache. Also
        (re)loads its "mother" playlist link, if any -- resolved lazily
        against `self.playlists` at add-time, so it doesn't matter
        whether the parent cue file has been loaded yet at this point."""
        try:
            name, track_paths, parent_path = read_cue_playlist(cue_path)
        except OSError as exc:
            if not is_restore:
                self.status_var.set(f"Could not load playlist: {exc}")
            return
        existing_tracks = [p for p in track_paths if os.path.isfile(p)]

        info = self.playlists.get(cue_path)
        if info is None:
            node_id = f"playlist::{cue_path}"
            self.library_tree.insert(
                self._playlists_root_id, tk.END, iid=node_id, text=name, open=False)
            info = {
                "name": name, "tracks": existing_tracks, "node_id": node_id,
                "parent": parent_path,
            }
            self.playlists[cue_path] = info
        else:
            info["name"] = name
            info["tracks"] = existing_tracks
            info["parent"] = parent_path
            self.library_tree.item(info["node_id"], text=name)

        self._populate_playlist_node(cue_path)
        if not is_restore:
            self.status_var.set(
                f"Playlist '{name}': {len(existing_tracks)} track(s)")

    def _populate_playlist_node(self, cue_path):
        """Rebuild a playlist's child rows in the library tree from
        `self.playlists[cue_path]["tracks"]`."""
        info = self.playlists.get(cue_path)
        if info is None:
            return
        node_id = info["node_id"]
        self.library_tree.delete(*self.library_tree.get_children(node_id))
        for item_id in [k for k, v in self._playlist_track_info.items()
                        if v[0] == cue_path]:
            del self._playlist_track_info[item_id]

        for index, path in enumerate(info["tracks"]):
            if not os.path.isfile(path):
                continue
            item_id = f"__playlist_track__{cue_path}::{index}"
            self.library_tree.insert(
                node_id, tk.END, iid=item_id, text=os.path.basename(path))
            self._playlist_track_info[item_id] = (cue_path, path)

    def add_tracks_to_playlist(self, cue_path, paths):
        """Append `paths` (real track paths) to the playlist at
        `cue_path`, skipping any already in it, and persist the change to
        its cue file. If `cue_path` has a "mother" playlist set (see
        set_playlist_parent), any track newly added here is ALSO (one-way
        -- adding to the parent never adds back to this playlist)
        propagated up to that parent, and the parent's own parent, and so
        on -- so a "mother" playlist automatically stays a superset of
        everything ever added to any of its "child" playlists."""
        added = self._add_tracks_to_playlist_quiet(cue_path, paths)
        info = self.playlists.get(cue_path)
        if info is None:
            return
        noun = "track" if added == 1 else "tracks"
        if added:
            self.status_var.set(
                f"Added {added} {noun} to playlist '{info['name']}'")
        else:
            self.status_var.set(
                f"Track(s) already in playlist '{info['name']}'")

    def _add_tracks_to_playlist_quiet(self, cue_path, paths, _visited=None):
        """Core logic behind add_tracks_to_playlist, minus the status
        message -- used both for the direct call (which reports the
        count back to the user) and to silently propagate newly added
        tracks up a chain of "mother" playlists. `_visited` guards
        against an (invalid, hand-edited) parent chain that loops back on
        itself. Returns how many tracks were newly added to `cue_path`
        itself."""
        info = self.playlists.get(cue_path)
        if info is None or not paths:
            return 0
        visited = _visited if _visited is not None else set()
        if cue_path in visited:
            return 0
        visited.add(cue_path)

        newly_added = [p for p in paths if p not in info["tracks"]]
        if newly_added:
            info["tracks"].extend(newly_added)
            write_cue_playlist(
                cue_path, info["name"], info["tracks"],
                track_tags=self.track_tags, parent_path=info.get("parent"))
            self._populate_playlist_node(cue_path)

            parent_cue = info.get("parent")
            if parent_cue and parent_cue in self.playlists:
                self._add_tracks_to_playlist_quiet(
                    parent_cue, newly_added, visited)

        return len(newly_added)

    def _playlist_ancestor_chain(self, cue_path):
        """The chain of `cue_path`'s "mother" playlists, starting with
        `cue_path` itself: [cue_path, its parent, its parent's parent,
        ...]. Stops early if a cycle is detected (shouldn't normally
        happen, but a hand-edited cue file could introduce one)."""
        chain = []
        current = cue_path
        seen = set()
        while current and current not in seen:
            seen.add(current)
            chain.append(current)
            info = self.playlists.get(current)
            current = info.get("parent") if info else None
        return chain

    def set_playlist_parent(self, cue_path):
        """Open a small dialog to choose (or clear) `cue_path`'s "mother"
        playlist: from then on, any track added to `cue_path` (the
        "child") is ALSO added to the chosen mother playlist, one-way
        (adding a track to the mother does NOT add it to this playlist).
        Playlists that would create a cycle (this playlist's own
        descendants, or itself) are excluded from the list."""
        info = self.playlists.get(cue_path)
        if info is None:
            return

        # Exclude anything that currently has `cue_path` in ITS ancestor
        # chain (i.e. cue_path's descendants, plus cue_path itself) --
        # picking one of those as cue_path's parent would create a cycle.
        excluded = {
            cp for cp in self.playlists
            if cue_path in self._playlist_ancestor_chain(cp)
        }
        candidates = sorted(
            ((cp, i["name"]) for cp, i in self.playlists.items()
             if cp not in excluded),
            key=lambda kv: kv[1].lower())

        dialog = tk.Toplevel(self.root)
        dialog.configure(bg=self.palette["bg"])
        dialog.title(f"Set Mother Playlist - {info['name']}")
        dialog.geometry("320x380")

        ttk.Label(
            dialog,
            text=(f"Choose a \"mother\" playlist for '{info['name']}': any "
                  "track added here will also be added to it (one-way -- "
                  "the reverse never happens)."),
            wraplength=300, justify=tk.LEFT,
        ).pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)

        list_frame = ttk.Frame(dialog)
        list_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8)
        listbox = tk.Listbox(
            list_frame, exportselection=False,
            bg=self.palette["field_bg"], fg=self.palette["field_fg"],
            selectbackground=self.palette["select_bg"],
            selectforeground=self.palette["select_fg"])
        scroll = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL, command=listbox.yview)
        listbox.configure(yscrollcommand=scroll.set)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        listbox.insert(tk.END, "(None -- no mother playlist)")
        for _cp, name in candidates:
            listbox.insert(tk.END, name)

        current_parent = info.get("parent")
        selected_index = 0
        for row, (cp, _name) in enumerate(candidates, start=1):
            if cp == current_parent:
                selected_index = row
                break
        listbox.selection_set(selected_index)
        listbox.see(selected_index)

        button_row = ttk.Frame(dialog, padding=8)
        button_row.pack(side=tk.BOTTOM, fill=tk.X)

        def on_ok():
            selection = listbox.curselection()
            if not selection:
                dialog.destroy()
                return
            index = selection[0]
            new_parent = None if index == 0 else candidates[index - 1][0]
            self._apply_playlist_parent(cue_path, new_parent)
            dialog.destroy()

        ttk.Button(button_row, text="OK", command=on_ok).pack(
            side=tk.RIGHT, padx=(4, 0))
        ttk.Button(button_row, text="Cancel",
                   command=dialog.destroy).pack(side=tk.RIGHT)

    def _apply_playlist_parent(self, cue_path, new_parent):
        """Set (or clear, if `new_parent` is None) `cue_path`'s "mother"
        playlist and persist it to its cue file."""
        info = self.playlists.get(cue_path)
        if info is None:
            return
        info["parent"] = new_parent
        write_cue_playlist(
            cue_path, info["name"], info["tracks"], track_tags=self.track_tags,
            parent_path=new_parent)
        if new_parent:
            parent_name = self.playlists.get(new_parent, {}).get("name", "?")
            self.status_var.set(
                f"'{info['name']}' will now feed new tracks into '{parent_name}'")
        else:
            self.status_var.set(
                f"Cleared mother playlist for '{info['name']}'")

    def remove_track_from_playlist(self, cue_path, item_id):
        info = self.playlists.get(cue_path)
        track_info = self._playlist_track_info.get(item_id)
        if info is None or track_info is None:
            return
        _cue_path, real_path = track_info
        if real_path in info["tracks"]:
            info["tracks"].remove(real_path)
        write_cue_playlist(
            cue_path, info["name"], info["tracks"], track_tags=self.track_tags,
            parent_path=info.get("parent"))
        self._populate_playlist_node(cue_path)
        self.status_var.set(f"Removed track from playlist '{info['name']}'")

    def rename_playlist(self, cue_path):
        info = self.playlists.get(cue_path)
        if info is None:
            return
        new_name = simpledialog.askstring(
            "Rename Playlist", "New name:", initialvalue=info["name"],
            parent=self.root)
        if not new_name or not new_name.strip():
            return
        info["name"] = new_name.strip()
        write_cue_playlist(
            cue_path, info["name"], info["tracks"], track_tags=self.track_tags,
            parent_path=info.get("parent"))
        self.library_tree.item(info["node_id"], text=info["name"])
        self.status_var.set(f"Renamed playlist to '{info['name']}'")

    def remove_playlist(self, cue_path):
        """Remove the playlist from the library tree and delete its cue
        file. Does NOT touch the audio files it referenced. Any OTHER
        playlist that had this one set as its "mother" loses that link
        (rather than being left pointing at a now-deleted cue file)."""
        info = self.playlists.pop(cue_path, None)
        if info is None:
            return
        for other_cp, other_info in self.playlists.items():
            if other_info.get("parent") == cue_path:
                other_info["parent"] = None
                write_cue_playlist(
                    other_cp, other_info["name"], other_info["tracks"],
                    track_tags=self.track_tags, parent_path=None)
        for item_id in [k for k, v in self._playlist_track_info.items()
                        if v[0] == cue_path]:
            del self._playlist_track_info[item_id]
        if self.library_tree.exists(info["node_id"]):
            self.library_tree.delete(info["node_id"])
        try:
            os.remove(cue_path)
        except OSError:
            pass
        self.status_var.set(f"Removed playlist '{info['name']}'")

    def _view_folder(self, paths, folder_name=None):
        """Show `paths` in the playlist table on the right (same filtering
        that playing them would apply), WITHOUT starting playback --
        lets you browse a folder/album's tracklist without interrupting
        whatever is currently playing."""
        if not paths:
            self.status_var.set("No tracks found to view")
            return
        self.current_filter = ("paths", frozenset(paths))
        self._refresh_playlist_view()
        if folder_name:
            self._show_viewing_folder_label(folder_name)
        self.status_var.set(f"Viewing {len(paths)} track(s)")

    def _open_in_file_manager(self, dirpath):
        """Open `dirpath` in the OS's normal file manager (read-only
        browsing -- doesn't touch the playlist or trigger playback)."""
        if not os.path.isdir(dirpath):
            self.status_var.set(f"Folder not found: {dirpath}")
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", dirpath])
            elif sys.platform.startswith("win"):
                os.startfile(dirpath)  # pylint: disable=no-member
            else:
                subprocess.Popen(["xdg-open", dirpath])
            self.status_var.set(f"Opened in file manager: {dirpath}")
        except Exception as exc:
            self.status_var.set(f"Could not open file manager: {exc}")

    def _build_context_menu(self, primary_paths, selected_paths, include_filters=True,
                            exclude_ignored_from_queue=False):
        """Build the shared right-click dropdown menu for the given
        primary track(s) (what "Play"/"Go to Album" act on) and selected
        track(s) (what "Add to Queue"/"Remove"/"Ignore"/"Properties" act on).
        `include_filters=False` omits "Go to Album"/"More by Same Artist"
        (used for the library tree's folder nodes, where those filters
        don't make sense). `exclude_ignored_from_queue=True` (used for the
        library tree, where "Add to Queue" may expand to a whole folder/
        album/playlist's tracks) skips any ignored track rather than
        queueing it -- ignored tracks are only ever queued when explicitly
        selected directly in the playlist table itself."""
        primary_path = primary_paths[0] if primary_paths else None

        menu = tk.Menu(self.root, tearoff=0, **self._menu_colors())
        menu.add_command(label="Play",
                         command=lambda: self.context_play(primary_paths))
        menu.add_command(label="Shuffle Play",
                         command=self.context_shuffle_play)
        menu.add_command(
            label="Add to Queue",
            command=lambda: self.context_add_to_queue(
                selected_paths, exclude_ignored=exclude_ignored_from_queue))
        if include_filters:
            menu.add_separator()
            menu.add_command(
                label="Go to Album",
                command=lambda: self._filter_by("album", primary_path) if primary_path else None)
            menu.add_command(
                label="More by Same Artist",
                command=lambda: self._filter_by("artist", primary_path) if primary_path else None)
        menu.add_separator()
        menu.add_cascade(
            label="Add to Playlist",
            menu=self._build_add_to_playlist_menu(selected_paths))
        menu.add_command(
            label="Remove from Playlist",
            command=lambda: self.context_remove_from_playlist(selected_paths))
        ignore_label = (
            "Un-ignore" if selected_paths and all(
                p in self.player.ignored for p in selected_paths)
            else "Ignore")
        menu.add_command(label=ignore_label,
                         command=lambda: self.context_ignore(selected_paths))
        menu.add_separator()
        menu.add_command(
            label="Normalize Track Numbers",
            command=lambda: self.context_normalize_track_numbers(selected_paths))
        menu.add_command(label="Properties",
                         command=lambda: self.open_properties(selected_paths))
        return menu

    def _build_add_to_playlist_menu(self, paths):
        """Submenu listing every existing cue playlist (add `paths` to it)
        plus a "New Playlist..." entry (create one pre-filled with
        `paths`), for the shared context menu's "Add to Playlist" cascade."""
        submenu = tk.Menu(self.root, tearoff=0, **self._menu_colors())
        entries = sorted(self.playlists.items(),
                         key=lambda kv: kv[1]["name"].lower())
        for cue_path, info in entries:
            submenu.add_command(
                label=info["name"],
                command=lambda cp=cue_path: self.add_tracks_to_playlist(cp, paths))
        if entries:
            submenu.add_separator()
        submenu.add_command(
            label="New Playlist...",
            command=lambda: self.create_playlist(initial_tracks=paths))
        return submenu

    def _popup_menu(self, menu, event):
        # Ensure the menu closes if the user clicks anywhere outside it.
        menu.bind("<FocusOut>", lambda _e: menu.unpost())
        try:
            menu.tk_popup(event.x_root, event.y_root)
            menu.focus_set()
        finally:
            menu.grab_release()

    # -- drag-and-drop tracks onto a playlist -----------------------------
    def _on_drag_start(self, event):
        tree = event.widget
        if tree.identify_region(event.x, event.y) == "heading":
            return
        row = tree.identify_row(event.y)
        if not row:
            return

        current_selection = tree.selection()
        if row in current_selection and len(current_selection) > 1:
            # Pressing down on one of several already-selected rows: grab
            # the whole selection for the drag, and stop the click from
            # propagating to Treeview's default handling, which would
            # otherwise collapse the selection to just this row before a
            # drag even starts.
            self._begin_drag(tree, current_selection)
            self._move_drag_indicator(event)
            return "break"

        tree.selection_set(row)
        self._begin_drag(tree, (row,))
        self._move_drag_indicator(event)
        return None

    def _begin_drag(self, tree, rows):
        paths = []
        for row in rows:
            for path in self._collect_audio_paths(tree, row):
                if path not in paths:
                    paths.append(path)
        if not paths:
            return
        self._drag_paths = paths
        self._drag_indicator = self._make_drag_indicator(len(paths))

    def _on_drag_motion(self, event):
        if not self._drag_paths:
            return
        self._move_drag_indicator(event)

    def _on_drag_release(self, event):
        paths = self._drag_paths
        self._drag_paths = None
        self._destroy_drag_indicator()
        if not paths:
            return

        target = self.root.winfo_containing(event.x_root, event.y_root)
        if target is not self.library_tree:
            return
        local_y = event.y_root - self.library_tree.winfo_rooty()
        row = self.library_tree.identify_row(local_y)
        cue_path = next(
            (cp for cp, info in self.playlists.items()
             if info["node_id"] == row), None)
        if cue_path is None:
            return
        self.add_tracks_to_playlist(cue_path, paths)

    def _make_drag_indicator(self, count):
        """A small borderless window that follows the cursor while
        dragging track(s) onto a playlist, showing how many are selected.
        Fades in on appearance (via window alpha) rather than popping up
        instantly -- see _destroy_drag_indicator for the matching
        fade-out."""
        try:
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            try:
                top.attributes("-alpha", 0.0)
            except tk.TclError:
                pass
            noun = "track" if count == 1 else "tracks"
            tk.Label(
                top, text=f"{count} {noun}", padx=6, pady=2, font=("", 9),
                bg=self.palette["highlight_bg"], fg=self.palette["highlight_fg"],
            ).pack()
            self._fade_toplevel_alpha(top, 0.0, 0.92)
            return top
        except tk.TclError:
            return None

    def _fade_toplevel_alpha(self, window, start_alpha, end_alpha,
                             total_steps=6, step_ms=20, on_complete=None):
        """Animate a Toplevel's `-alpha` window attribute from
        `start_alpha` to `end_alpha` (0.0-1.0), for smooth fade-in/out of
        transient popups like the drag indicator. Silently no-ops if the
        window (or the platform) doesn't support `-alpha`."""
        def step(i=0):
            if not window.winfo_exists():
                if on_complete:
                    on_complete()
                return
            ratio = (i + 1) / total_steps
            alpha = start_alpha + (end_alpha - start_alpha) * ratio
            try:
                window.attributes("-alpha", alpha)
            except tk.TclError:
                if on_complete:
                    on_complete()
                return
            if i + 1 >= total_steps:
                if on_complete:
                    on_complete()
            else:
                self.root.after(step_ms, step, i + 1)

        step(0)

    def _move_drag_indicator(self, event):
        if self._drag_indicator is not None:
            try:
                self._drag_indicator.geometry(
                    f"+{event.x_root + 12}+{event.y_root + 12}")
            except tk.TclError:
                pass

    def _destroy_drag_indicator(self):
        if self._drag_indicator is not None:
            window = self._drag_indicator
            self._drag_indicator = None
            self._fade_toplevel_alpha(
                window, 0.92, 0.0,
                on_complete=lambda: self._safe_destroy(window))

    @staticmethod
    def _safe_destroy(window):
        try:
            window.destroy()
        except tk.TclError:
            pass

    def _on_progress_left_click(self, event):
        """Left-click on the Now Playing progress bar seeks to that
        position in the track, gliding the bar there smoothly rather
        than jumping instantly."""
        if self.player.current_index is None or self.current_duration <= 0:
            return
        width = self.now_playing_progress.winfo_width()
        if width <= 0:
            return
        ratio = min(max(event.x / width, 0.0), 1.0)
        self._animate_seek_bar(ratio * self.current_duration)

    def _animate_seek_bar(self, target_seconds, total_steps=6, step_ms=20):
        start_value = self.now_playing_progress["value"] or 0.0

        def step(i=0):
            ratio = (i + 1) / total_steps
            self._set_progress(
                start_value + (target_seconds - start_value) * ratio)
            if i + 1 >= total_steps:
                self._seek_to(target_seconds)
            else:
                self.root.after(step_ms, step, i + 1)

        step(0)

    def _on_progress_right_click(self, event):
        """Right-click on the Now Playing progress bar opens the shared
        dropdown menu for the currently playing track."""
        if self.player.current_index is None or not self.player.playlist:
            return
        path = self.player.playlist[self.player.current_index]
        menu = self._build_context_menu([path], [path])
        self._popup_menu(menu, event)

    def _seek_to(self, seconds):
        seconds = max(0.0, min(seconds, self.current_duration))
        self.elapsed_before_pause = seconds
        self.play_started_monotonic = time.monotonic()
        self._set_progress(seconds)

        if self.audio_ready:
            try:
                pygame.mixer.music.play(start=seconds)
                if self.is_paused:
                    pygame.mixer.music.pause()
            except Exception as exc:
                self.status_var.set(f"Seek not supported for this file: {exc}")

        if not self.is_paused:
            self.is_playing = True
            self._tick_progress()

    def context_shuffle_play(self):
        scope = self._current_scope_paths()
        candidates = [
            p for p in scope if p not in self.player.ignored] or scope
        if not candidates:
            self.status_var.set("Playlist is empty")
            return
        path = random.choice(candidates)
        logger.debug("context_shuffle_play: chose %s from %d candidate(s)",
                     os.path.basename(path), len(candidates))
        self._play_track(path)  # manual pick: resets the played-numbers pool

    def context_play(self, paths):
        """Play the given track(s) (Play from the right-click/dropdown menu).
        When more than one track is given, the playlist view switches to
        show exactly this set of tracks (handled by _play_paths)."""
        if not paths:
            return
        self._play_paths(paths)

    def context_add_to_queue(self, paths, exclude_ignored=False):
        """Queue `paths`. When `exclude_ignored` is True (used for the
        library tree's folder/album/playlist "Add to Queue", which
        expands to every descendant track), any ignored track is skipped
        rather than queued -- ignored tracks are only ever queued when
        explicitly selected directly in the playlist table itself
        (single or multi-select), where `exclude_ignored` stays False."""
        if exclude_ignored:
            skipped = sum(1 for p in paths if p in self.player.ignored)
            paths = [p for p in paths if p not in self.player.ignored]
        else:
            skipped = 0
        if not paths:
            if skipped:
                self.status_var.set(
                    f"All {skipped} track(s) are ignored, none added to queue")
            return
        self.player.queue.extend(paths)
        self._refresh_queue_view()
        suffix = f" ({skipped} ignored track(s) skipped)" if skipped else ""
        self.status_var.set(f"Added {len(paths)} track(s) to queue{suffix}")

    def on_add_selection_to_queue(self):
        """Handler for the Now Playing bar's "Add to Queue" button: queues
        the track CURRENTLY PLAYING (this button lives among the other
        Now Playing transport controls -- Play/Pause/Stop/Shuffle/Repeat
        -- which all act on the active track, not on the playlist
        table's selection)."""
        path = self._current_playing_path()
        if path is None:
            self.status_var.set("No track is currently playing")
            return
        self.context_add_to_queue([path])

    def context_remove_from_playlist(self, paths):
        for path in paths:
            self._remove_track(path)
        self.status_var.set(f"Removed {len(paths)} track(s) from playlist")

    def context_ignore(self, paths):
        """Toggle the "ignored" state of `paths`: if every one of them is
        already ignored, un-ignore them all; otherwise ignore them all.
        Ignored tracks are skipped when automatic Next/Previous picks a
        random/sequential track number (manually playing one still
        works), and are shown grayed-out with a "\u2717" marker in both the
        playlist table and the library tree."""
        if not paths:
            return
        already_all_ignored = all(p in self.player.ignored for p in paths)
        if already_all_ignored:
            self.player.ignored.difference_update(paths)
            action = "Un-ignored"
        else:
            self.player.ignored.update(paths)
            action = "Ignored"
        self._refresh_ignored_marks(paths)
        self.status_var.set(f"{action} {len(paths)} track(s)")

    def _refresh_ignored_marks(self, paths):
        """Update the visible "ignored" marker (grayscale + "\u2717" prefix)
        on `paths`' rows in both the playlist table and the library
        tree, without a full rebuild of either."""
        for path in paths:
            if self.playlist_tree.exists(path):
                self.playlist_tree.item(
                    path, values=self._row_values(path), tags=self._row_tags(path))
            self._apply_library_ignored_mark(path)

    def _apply_library_ignored_mark(self, path):
        if not self.library_tree.exists(path):
            return
        ignored = path in self.player.ignored
        base_name = os.path.basename(path)
        text = f"\u2717 {base_name}" if ignored else base_name
        self.library_tree.item(
            path, text=text, tags=("ignored",) if ignored else ())

    def context_normalize_track_numbers(self, paths):
        """Right-click "Normalize Track Numbers": rewrite each track's
        "tracknumber" tag on disk from whatever format it's currently in
        (e.g. "01", "3/12") down to just the plain number (e.g. "1",
        "3"), stripping leading zeros and any "/<total>" suffix. Tracks
        with no parseable track number are left untouched."""
        changed = 0
        skipped = 0
        for path in paths:
            current = read_common_tags(path).get("tracknumber", "")
            match = re.search(r"\d+", current or "")
            if not match:
                skipped += 1
                continue
            normalized = str(int(match.group(0)))
            if normalized == current:
                continue
            try:
                apply_common_tags(path, {"tracknumber": normalized})
            except Exception as exc:
                self.status_var.set(
                    f"Failed to normalize {os.path.basename(path)}: {exc}")
                continue
            self._update_track_cache(path)
            changed += 1

        if changed:
            noun = "track" if changed == 1 else "tracks"
            suffix = f" ({skipped} skipped, no track number)" if skipped else ""
            self.status_var.set(
                f"Normalized track number on {changed} {noun}{suffix}")
        else:
            self.status_var.set("No track numbers needed normalizing")

    def _validate_numeric_input(self, action, proposed):
        """`validatecommand` for the Track #/Disc #/Year/BPM entry boxes:
        always allow deletions (action "0"), but only let insertions
        through if the resulting text is empty or all digits -- this is
        what auto-formats those fields to numbers-only as you type."""
        if action == "0":
            return True
        return proposed == "" or proposed.isdigit()

    def _make_tag_entry(self, parent, key, var, width):
        if key in NUMERIC_TAG_FIELDS:
            return ttk.Entry(
                parent, textvariable=var, width=width,
                validate="key", validatecommand=self._numeric_validate_cmd)
        return ttk.Entry(parent, textvariable=var, width=width)

    def show_properties(self, path=None):
        if path is None:
            selection = self.playlist_tree.selection()
            if not selection:
                self.status_var.set("Select a track to view its properties")
                return
            path = selection[0]

        dialog = tk.Toplevel(self.root)
        dialog.configure(bg=self.palette["bg"])
        dialog.title(f"Properties - {os.path.basename(path)}")
        dialog.geometry("520x600")

        common = read_common_tags(path)
        entries = {}

        edit_frame = ttk.LabelFrame(dialog, text="Tags", padding=8)
        edit_frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        for row, (key, label) in enumerate(COMMON_TAG_FIELDS):
            ttk.Label(edit_frame, text=label, width=14, anchor=tk.W).grid(
                row=row, column=0, sticky=tk.W, pady=2)
            var = tk.StringVar(value=common.get(key, ""))
            self._make_tag_entry(edit_frame, key, var, 40).grid(
                row=row, column=1, sticky=tk.EW, pady=2)
            entries[key] = var
        edit_frame.columnconfigure(1, weight=1)

        # Packed BEFORE the expanding "Details" panel below (even though
        # it's visually at the bottom) -- Tkinter's pack manager carves
        # out space in the order widgets are packed, so a side=BOTTOM
        # widget packed AFTER an expand=True/fill=BOTH one gets squeezed
        # down to a 0-size sliver (i.e. the Save/Cancel buttons become
        # invisible). Packing it first reserves its space up front.
        button_row = ttk.Frame(dialog, padding=(8, 0, 8, 8))
        button_row.pack(side=tk.BOTTOM, fill=tk.X)

        details_frame = ttk.LabelFrame(dialog, text="Details", padding=8)
        details_frame.pack(side=tk.TOP, fill=tk.BOTH,
                           expand=True, padx=8, pady=(0, 8))

        columns = ("field", "value")
        tree = ttk.Treeview(details_frame, columns=columns, show="headings")
        tree.heading("field", text="Field")
        tree.heading("value", text="Value")
        tree.column("field", width=150, anchor=tk.W)
        tree.column("value", width=330, anchor=tk.W)
        scroll = ttk.Scrollbar(
            details_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        for field, value in read_full_metadata(path):
            tree.insert("", tk.END, values=(field, value))

        def on_save():
            updates = {key: var.get() for key, var in entries.items()}
            try:
                apply_common_tags(path, updates, clear_blank=True)
            except Exception as exc:
                self.status_var.set(f"Failed to save tags: {exc}")
                return
            self._update_track_cache(path)
            self.status_var.set(f"Saved tags for {os.path.basename(path)}")
            dialog.destroy()

        ttk.Button(button_row, text="Save", command=on_save).pack(
            side=tk.RIGHT, padx=(4, 0))
        ttk.Button(button_row, text="Cancel",
                   command=dialog.destroy).pack(side=tk.RIGHT)

    def open_properties(self, paths=None):
        if paths is None:
            paths = self.playlist_tree.selection()
        if not paths:
            self.status_var.set("Select a track to view its properties")
            return
        if len(paths) > 1:
            self.open_bulk_properties(paths)
        else:
            self.show_properties(paths[0])

    def open_bulk_properties(self, paths=None):
        if paths is None:
            paths = self.playlist_tree.selection()
        if not paths:
            self.status_var.set("Select tracks to bulk edit properties")
            return
        if len(paths) == 1:
            self.show_properties(paths[0])
            return

        dialog = tk.Toplevel(self.root)
        dialog.configure(bg=self.palette["bg"])
        dialog.title(f"Bulk Edit Properties ({len(paths)} tracks)")
        dialog.geometry("420x320")

        ttk.Label(
            dialog,
            text=(f"Editing {len(paths)} tracks. Fields showing "
                  f"\"{MIXED_SENTINEL}\" differ between tracks and are left "
                  "unchanged unless you edit them."),
            wraplength=380, justify=tk.LEFT,
        ).pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)

        edit_frame = ttk.Frame(dialog, padding=8)
        edit_frame.pack(side=tk.TOP, fill=tk.X, padx=8)
        entries = {}
        common_values = {
            key: self._common_or_mixed(paths, key) for key, _label in COMMON_TAG_FIELDS}
        for row, (key, label) in enumerate(COMMON_TAG_FIELDS):
            ttk.Label(edit_frame, text=label, width=14, anchor=tk.W).grid(
                row=row, column=0, sticky=tk.W, pady=2)
            var = tk.StringVar(value=common_values[key])
            self._make_tag_entry(edit_frame, key, var, 30).grid(
                row=row, column=1, sticky=tk.EW, pady=2)
            entries[key] = var
        edit_frame.columnconfigure(1, weight=1)

        button_row = ttk.Frame(dialog, padding=8)
        button_row.pack(side=tk.BOTTOM, fill=tk.X)

        def on_save():
            updates = {
                key: var.get() for key, var in entries.items()
                if var.get().strip() and var.get() != MIXED_SENTINEL
            }
            if not updates:
                self.status_var.set("No fields changed, nothing to save")
                dialog.destroy()
                return
            failed = 0
            for path in paths:
                try:
                    apply_common_tags(path, updates, clear_blank=False)
                    self._update_track_cache(path)
                except Exception:
                    failed += 1
            ok = len(paths) - failed
            self.status_var.set(
                f"Bulk edit applied to {ok} track(s)" +
                (f", {failed} failed" if failed else ""))
            dialog.destroy()

        ttk.Button(button_row, text="Apply to All",
                   command=on_save).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(button_row, text="Cancel",
                   command=dialog.destroy).pack(side=tk.RIGHT)

    def _common_or_mixed(self, paths, key):
        """Return the shared tag value for `key` across all `paths`, or
        MIXED_SENTINEL if they differ (empty values are ignored when
        comparing, so a track missing the tag doesn't cause a false mixed)."""
        values = set()
        for path in paths:
            tags = read_common_tags(path)
            value = tags.get(key, "").strip()
            if value:
                values.add(value)
        if not values:
            return ""
        if len(values) == 1:
            return next(iter(values))
        return MIXED_SENTINEL

    def _update_track_cache(self, path):
        self.track_tags[path] = self._read_all_track_tags(path)
        if self.playlist_tree.exists(path):
            self.playlist_tree.item(path, values=self._row_values(path))

        current_index = self.player.current_index
        if (current_index is not None
                and 0 <= current_index < len(self.player.playlist)
                and self.player.playlist[current_index] == path):
            tags = self.track_tags[path]
            self.now_title_var.set(tags.get("title", ""))
            self.now_artist_var.set(tags.get("artist") or "Unknown Artist")

    # -- playback ----------------------------------------------------------
    def on_volume_change(self, value):
        if self.audio_ready:
            try:
                pygame.mixer.music.set_volume(float(value) / 100)
            except Exception:
                pass

    def _play_track(self, path, nav=False, from_queue=False):
        """Play `path`. `nav=True` means this call came from _pick_next_path /
        _pick_previous_path (on_next/on_previous), which already updated the
        played-numbers pool/pointer themselves, so it should NOT be reset
        here. `from_queue=True` means this call is playing a queued track
        (added via "Add to Queue" or the queue panel): queued tracks are a
        side-channel that doesn't disturb sequential/shuffle playback --
        they don't reset the played-numbers pool, their number is never
        added to it, and they don't change which view/filter is currently
        displayed. Any other caller (double-click, toolbar Play, context
        menu, Shuffle Play, ...) is a fresh manual choice that restarts the
        played-numbers pool, but otherwise keeps whatever view/filter is
        CURRENTLY active (the full playlist, an album/artist filter from
        "Go to Album"/"More by Same Artist", a "View"'d folder, ...) --
        pressing Play plays through what's actually in view, it does NOT
        jump the view to the track's own album."""
        if path not in self.player.playlist:
            logger.debug(
                "_play_track: %r is not in the playlist, ignoring", path)
            return
        self.player.current_index = self.player.playlist.index(path)
        self._highlight_now_playing_row(path)

        if not nav and not from_queue:
            scope = self._current_scope_paths()
            number = scope.index(path) if path in scope else 0
            self.played_numbers = [number]
            self.history_pos = 0
        logger.debug(
            "_play_track: path=%s nav=%s from_queue=%s shuffle=%s repeat=%s "
            "played_numbers=%s pos=%d",
            os.path.basename(path), nav, from_queue, self.shuffle_enabled,
            self.repeat_enabled, self.played_numbers, self.history_pos)

        if self.audio_ready:
            try:
                pygame.mixer.music.load(path)
                pygame.mixer.music.set_volume(self.volume_var.get() / 100)
                pygame.mixer.music.play()
            except Exception as exc:
                self.status_var.set(f"Playback error: {exc}")

        artist, title, _album, _dur = read_track_tags(path)
        self.current_duration = get_track_duration(path)

        new_art_pil = get_track_art_pil(path) or make_placeholder_art_pil()
        self._crossfade_art(new_art_pil)

        self.now_title_var.set(title)
        self.now_artist_var.set(artist or "Unknown Artist")
        self._animate_color_fade(
            "now_playing_title",
            lambda color: self.now_title_label.configure(foreground=color),
            self.palette["bg"], self.palette["fg"], total_steps=10, step_ms=20)
        self._animate_color_fade(
            "now_playing_artist",
            lambda color: self.now_artist_label.configure(foreground=color),
            self.palette["bg"], self.palette["fg"], total_steps=10, step_ms=20)
        self.now_playing_progress.config(maximum=max(self.current_duration, 1))
        self.duration_var.set(format_duration(self.current_duration) or "0:00")

        self.elapsed_before_pause = 0.0
        self.play_started_monotonic = time.monotonic()
        self.is_playing = True
        self.is_paused = False
        self.status_var.set(f"Playing: {title}")
        self._update_play_pause_button()
        self._pulse_play_pause_button()
        self._tick_progress()

    def _now_playing_text_widgets(self):
        return (self.now_title_label, self.now_artist_label)

    # Supersampling factor for the disk rendering: everything (circle
    # mask, rim, hole) is drawn at diameter * _DISK_SUPERSAMPLE, then
    # downsampled to the real display size with LANCZOS. Drawing the
    # circle directly at the small on-screen size (~72px) leaves visible
    # jagged/mismatched edges between the mask, the rim stroke, and the
    # square canvas corners -- rendering it oversized first and smoothly
    # downsampling removes that ragged "outline" and gives a clean
    # anti-aliased circle instead.
    _DISK_SUPERSAMPLE = 4

    def _build_disk_hires(self, art_pil):
        """Build the un-rotated "spinning disk" rendering of `art_pil` at
        supersampled resolution (a circle with a glossy white/silver rim
        and a white-ringed center spindle hole, like a real pressed CD
        label), painted onto a square canvas filled with the current
        theme's background color so its corners blend invisibly into the
        Now Playing bar behind it. Returns (hires_image, native_diameter).
        The hole itself is filled with a color sampled from the art's own
        corners (like a real CD label's print extending up to the hole),
        not the app's theme background, so it doesn't look like a chunk
        of the UI is missing."""
        diameter = min(art_pil.size)
        hi = diameter * self._DISK_SUPERSAMPLE
        page_bg_color = self.palette["bg"]

        art = art_pil.resize((hi, hi), Image.LANCZOS)
        disk = Image.new("RGB", (hi, hi), color=page_bg_color)
        mask = Image.new("L", (hi, hi), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, hi - 1, hi - 1), fill=255)
        disk.paste(art, (0, 0), mask)

        # Sample the art's own background color (averaged from its four
        # corners) to fill the center hole, so the hole reads as "part of
        # the disk's label" rather than a cutout to the app's theme color.
        corner_inset = max(1, hi // 30)
        corners = [
            art.getpixel((corner_inset, corner_inset)),
            art.getpixel((hi - 1 - corner_inset, corner_inset)),
            art.getpixel((corner_inset, hi - 1 - corner_inset)),
            art.getpixel((hi - 1 - corner_inset, hi - 1 - corner_inset)),
        ]
        label_color = tuple(
            round(sum(channel) / len(corners)) for channel in zip(*corners))

        draw = ImageDraw.Draw(disk)
        rim_width = max(2, hi // 30)
        draw.ellipse(
            (rim_width // 2, rim_width // 2,
             hi - 1 - rim_width // 2, hi - 1 - rim_width // 2),
            outline="#f2f2f2", width=rim_width)

        hole_radius = max(3, hi // 9)
        ring_width = max(2, hi // 45)
        cx = cy = hi // 2
        draw.ellipse(
            (cx - hole_radius - ring_width, cy - hole_radius - ring_width,
             cx + hole_radius + ring_width, cy + hole_radius + ring_width),
            outline="#f2f2f2", width=ring_width)
        draw.ellipse(
            (cx - hole_radius, cy - hole_radius,
             cx + hole_radius, cy + hole_radius),
            fill=label_color)

        return disk, diameter

    def _disk_frame_from_hires(self, hires_image, diameter, angle=0):
        """Rotate the supersampled `hires_image` by `angle` degrees (if
        any) and downsample it to `diameter` for a single smooth,
        anti-aliased on-screen disk frame.

        A slight Gaussian blur is applied BEFORE the downsample: LANCZOS
        (needed for a crisp result) rings/overshoots on very
        high-contrast edges -- like the bright white rim against a dark
        theme's near-black background -- which shows up as a faint dark
        "outline" just outside the rim. Pre-blurring band-limits that
        edge just enough to remove the ringing while still downsampling
        to a crisp, clean circle (the blur radius is tiny relative to
        the supersampled size, so it doesn't noticeably soften the art
        or rim itself)."""
        if angle:
            hires_image = hires_image.rotate(
                -angle, resample=Image.BICUBIC, fillcolor=self.palette["bg"])
        blurred = hires_image.filter(
            ImageFilter.GaussianBlur(radius=self._DISK_SUPERSAMPLE / 2))
        return blurred.resize((diameter, diameter), Image.LANCZOS)

    def _make_disk_image(self, art_pil, angle=0):
        """One-off disk render (e.g. a single crossfade blend frame):
        build + rotate + downsample in one call. For the continuously
        spinning disk, _set_disk_base + _spin_disk_step cache the
        supersampled base instead, to avoid rebuilding it from scratch
        every frame."""
        hires, diameter = self._build_disk_hires(art_pil)
        return self._disk_frame_from_hires(hires, diameter, angle)

    def _set_disk_base(self, art_pil):
        """(Re)build the disk for a "settled" cover art (a new track, a
        theme change, ...): caches both the supersampled un-rotated disk
        (`_disk_hires_image`, reused every frame while spinning) and its
        native-resolution angle-0 rendering (`_disk_base_image`, shown
        while paused/stopped)."""
        hires, diameter = self._build_disk_hires(art_pil)
        self._disk_hires_image = hires
        self._disk_base_image = self._disk_frame_from_hires(hires, diameter)

    def _show_static_art_frame(self):
        """Display a single, non-animating frame for the Now Playing
        album art, honoring the `disk_spin_enabled` toggle (View > Spin
        Album Art): the circular CD-style disk (at its current, possibly
        nonzero, rotation) when enabled, or just the plain square album
        cover -- no circular crop/rim at all -- when disabled. Used
        whenever the display needs to be (re)set outside of the
        continuous spin loop itself (toggling the effect, pausing/
        stopping, a theme change, ...)."""
        if self._current_art_pil is None:
            return
        if self.disk_spin_enabled and self._disk_base_image is not None:
            if self._disk_angle and self._disk_hires_image is not None:
                diameter = self._disk_base_image.size[0]
                frame = self._disk_frame_from_hires(
                    self._disk_hires_image, diameter, self._disk_angle)
            else:
                frame = self._disk_base_image
        else:
            frame = self._current_art_pil
        photo = ImageTk.PhotoImage(frame)
        self.now_playing_art_image = photo
        self.art_label.config(image=photo)

    def _start_disk_spin(self):
        """Begin (or resume) continuously rotating the Now Playing disk.
        A no-op if it's already spinning, or if the effect is turned off
        (View > Spin Album Art)."""
        if not self.disk_spin_enabled:
            return
        if self._disk_spin_job is not None or self._disk_hires_image is None:
            return
        self._spin_disk_step()

    def toggle_disk_spin(self):
        """Handler for the View > "Spin Album Art" checkbutton: turns the
        continuous CD-spin animation on/off. Turning it OFF immediately
        reverts the display to just the plain square album cover (no
        circular disk/rim at all); turning it back ON resumes the disk
        rendering from the same rotation angle it was frozen at."""
        self.disk_spin_enabled = self.disk_spin_var.get()
        if self.disk_spin_enabled:
            if self.is_playing and not self.is_paused:
                self._start_disk_spin()
            else:
                self._show_static_art_frame()
            self.status_var.set("Album art spin: On")
        else:
            self._stop_disk_spin()
            self._show_static_art_frame()
            self.status_var.set("Album art spin: Off")

    def toggle_browsing_mode(self):
        """Handler for the View > "Browsing Mode" checkbutton: when on,
        double-clicking a track/folder/queue row no longer starts
        playback -- only an explicit "Play"/"Play Now" from the
        right-click menu does. Lets you browse the library/playlist and
        do other actions freely while something's already playing,
        without a stray double-click accidentally switching tracks."""
        self.browsing_mode = self.browsing_mode_var.get()
        if self.browsing_mode:
            self.status_var.set(
                "Browsing Mode: On (right-click > Play to start a track)")
        else:
            self.status_var.set("Browsing Mode: Off")

    def _spin_disk_step(self):
        if not (self.is_playing and not self.is_paused) or self._disk_hires_image is None:
            self._disk_spin_job = None
            return
        # A small angle step at a high frame rate (rather than a big jump
        # every 50ms) is what makes the rotation read as smooth motion
        # instead of a visible stutter; the supersampled hi-res source
        # keeps each rotated frame's edge anti-aliased too.
        self._disk_angle = (self._disk_angle + 3) % 360
        diameter = self._disk_base_image.size[0]
        rotated = self._disk_frame_from_hires(
            self._disk_hires_image, diameter, self._disk_angle)
        photo = ImageTk.PhotoImage(rotated)
        self.now_playing_art_image = photo
        self.art_label.config(image=photo)
        self._disk_spin_job = self.root.after(25, self._spin_disk_step)

    def _stop_disk_spin(self):
        """Freeze the disk at its current rotation (used when pausing/
        stopping, and briefly during a track-change crossfade)."""
        if self._disk_spin_job is not None:
            self.root.after_cancel(self._disk_spin_job)
            self._disk_spin_job = None

    def _reset_disk_spin(self):
        """Stop spinning and snap back to angle 0 -- used on Stop, so the
        next Play starts from a clean, un-rotated disk (or, if the
        effect is currently turned off, just the plain album cover)."""
        self._stop_disk_spin()
        self._disk_angle = 0
        self._show_static_art_frame()

    def _crossfade_art(self, new_art_pil, total_steps=8, step_ms=25):
        """Smoothly blend the Now Playing album art from whatever's
        currently shown to `new_art_pil` (a PIL Image), instead of
        swapping it instantly. When the spinning-disk effect is enabled
        (View > Spin Album Art), each blended frame is rendered as a
        circular "disk" (frozen at the current rotation) -- once the
        blend finishes, the new track's disk becomes the spin base and
        rotation resumes (if a track is actively playing). When the
        effect is disabled, the blend uses the plain square art instead,
        with no circular disk/rim at all."""
        existing_job = self._art_crossfade_job
        if existing_job is not None:
            self.root.after_cancel(existing_job)
            self._art_crossfade_job = None
        self._stop_disk_spin()

        old_art_pil = self._current_art_pil
        if old_art_pil is None or old_art_pil.size != new_art_pil.size:
            old_art_pil = new_art_pil

        def step(i=0):
            ratio = (i + 1) / total_steps
            frame = Image.blend(old_art_pil, new_art_pil, ratio)
            if self.disk_spin_enabled:
                frame = self._make_disk_image(frame, angle=self._disk_angle)
            photo = ImageTk.PhotoImage(frame)
            self.now_playing_art_image = photo
            self.art_label.config(image=photo)
            if i + 1 >= total_steps:
                self._current_art_pil = new_art_pil
                self._set_disk_base(new_art_pil)
                self._art_crossfade_job = None
                if self.is_playing and not self.is_paused:
                    self._start_disk_spin()
                self._update_dynamic_theme_from_art(new_art_pil)
            else:
                self._art_crossfade_job = self.root.after(step_ms, step, i + 1)

        step(0)

    def _pulse_play_pause_button(self):
        """A brief color pulse on the Play/Pause button whenever playback
        (re)starts, giving visible feedback beyond just the text change."""
        self._animate_color_fade(
            self.play_pause_button,
            lambda color: self.play_pause_button.configure(bg=color),
            self.palette["select_bg"], self.palette["button_bg"],
            total_steps=10, step_ms=30)

    def _set_progress(self, elapsed):
        capped = max(0.0, min(elapsed, self.current_duration or elapsed))
        self.now_playing_progress["value"] = capped
        self.elapsed_var.set(format_duration(capped) or "0:00")

    def _current_elapsed_seconds(self):
        """Seconds elapsed into the currently-playing/paused/stopped
        track, kept in sync with the seek bar's own tracking. Used by
        both `_tick_progress` and the visualizer (see _tick_visualizer)
        to index into a track's precomputed spectrum frames."""
        if self.is_playing and not self.is_paused and self.play_started_monotonic is not None:
            return self.elapsed_before_pause + \
                (time.monotonic() - self.play_started_monotonic)
        return self.elapsed_before_pause

    def _tick_progress(self):
        if self.is_playing and not self.is_paused:
            elapsed = self._current_elapsed_seconds()
            if self.current_duration and elapsed >= self.current_duration:
                self._set_progress(self.current_duration)
                self.on_next()
                return
            self._set_progress(elapsed)
        if self.is_playing:
            self.progress_job = self.root.after(250, self._tick_progress)

    # -- "Visualizer" popup window (per-track frequency-bar animation) ----
    def open_visualizer(self):
        """Playback menu / Now Playing bar button: open the visualizer
        popup (or just bring it to the front if it's already open)."""
        if self._visualizer_window is not None and self._visualizer_window.winfo_exists():
            self._visualizer_window.deiconify()
            self._visualizer_window.lift()
            return

        window = tk.Toplevel(self.root)
        window.title("Visualizer")
        window.geometry("420x220")
        window.configure(bg=self.palette["field_bg"])
        window.protocol("WM_DELETE_WINDOW", self._on_visualizer_window_close)
        window.bind("<Destroy>", self._on_visualizer_window_destroy, add="+")

        canvas = tk.Canvas(
            window, bg=self.palette["field_bg"], highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self._visualizer_window = window
        self._visualizer_canvas = canvas
        self._visualizer_bar_ids = []

        self._ensure_visualizer_data_for_current_track()
        self._tick_visualizer()

    def _on_visualizer_window_close(self):
        # A plain destroy() is enough -- <Destroy> (bound below, in
        # open_visualizer) handles clearing all the window/job state in
        # one place regardless of how the window goes away (this button,
        # Alt+F4, closing the whole app, ...).
        if self._visualizer_window is not None:
            self._visualizer_window.destroy()

    def _on_visualizer_window_destroy(self, event):
        # Toplevel.bind("<Destroy>") also fires for every CHILD widget
        # being destroyed as part of tearing the window down (the
        # canvas, etc.) -- only react to the event for the window
        # itself, or this would fire (and null out state / cancel jobs
        # multiple times) for each child too.
        if event.widget is not self._visualizer_window:
            return
        if self._visualizer_tick_job is not None:
            self.root.after_cancel(self._visualizer_tick_job)
            self._visualizer_tick_job = None
        self._visualizer_window = None
        self._visualizer_canvas = None
        self._visualizer_bar_ids = []

    def _ensure_visualizer_data_for_current_track(self):
        """Kick off a background analysis (see visualizer.py) of the
        currently-playing track's spectrum, if it isn't already the
        track the held analysis results are for (or already being
        analyzed)."""
        path = self._current_playing_path()
        if not path:
            return
        if path == self._visualizer_track_path:
            return
        if path == self._visualizer_analyzing_path:
            return
        self._visualizer_analyzing_path = path
        thread = threading.Thread(
            target=self._visualizer_analysis_worker, args=(path,), daemon=True)
        thread.start()
        self.root.after(50, self._drain_visualizer_queue)

    def _visualizer_analysis_worker(self, path):
        """Runs in a background thread -- MUST NOT touch any Tk widget
        directly; only ever communicates back via the thread-safe
        `self._visualizer_queue`."""
        try:
            frames, fps = analyze_track_spectrum(
                path, num_bars=self._VISUALIZER_NUM_BARS)
        except Exception as exc:
            self._visualizer_queue.put(("error", path, str(exc)))
            return
        self._visualizer_queue.put(("ok", path, frames, fps))

    def _drain_visualizer_queue(self):
        """Runs on the main/UI thread (scheduled via `root.after`):
        applies a finished background analysis result, if one's ready."""
        try:
            item = self._visualizer_queue.get_nowait()
        except queue.Empty:
            # Still analyzing -- keep polling, but only while the popup
            # is actually open (no point burning timer ticks for a
            # closed window; _ensure_visualizer_data_for_current_track
            # will kick off a fresh poll loop next time it's opened).
            if self._visualizer_window is not None and self._visualizer_window.winfo_exists():
                self.root.after(50, self._drain_visualizer_queue)
            return

        kind = item[0]
        path = item[1]
        if path == self._visualizer_analyzing_path:
            self._visualizer_analyzing_path = None
        if kind == "ok":
            _, _path, frames, fps = item
            if path == self._current_playing_path():
                self._visualizer_track_path = path
                self._visualizer_frames = frames
                self._visualizer_fps = fps
        else:
            _, _path, message = item
            logger.debug(
                "visualizer analysis failed for %s: %s", path, message)
            if path == self._current_playing_path():
                self._visualizer_track_path = path
                self._visualizer_frames = None

    def _tick_visualizer(self):
        window = self._visualizer_window
        if window is None or not window.winfo_exists():
            self._visualizer_tick_job = None
            return

        path = self._current_playing_path()
        if path != self._visualizer_track_path:
            self._ensure_visualizer_data_for_current_track()

        if path and path == self._visualizer_track_path and self._visualizer_frames:
            frames = self._visualizer_frames
            elapsed = self._current_elapsed_seconds()
            frame_index = int(elapsed * self._visualizer_fps)
            frame_index = max(0, min(frame_index, len(frames) - 1))
            self._draw_visualizer_frame(frames[frame_index])
        elif path and path == self._visualizer_track_path and self._visualizer_frames is None:
            self._draw_visualizer_message(
                "Visualization unavailable for this track")
        elif path is None:
            self._draw_visualizer_message("Nothing playing")
        else:
            self._draw_visualizer_message("Analyzing track...")

        self._visualizer_tick_job = self.root.after(50, self._tick_visualizer)

    def _draw_visualizer_message(self, text):
        canvas = self._visualizer_canvas
        if canvas is None:
            return
        canvas.delete("all")
        self._visualizer_bar_ids = []
        width = canvas.winfo_width() or 1
        height = canvas.winfo_height() or 1
        canvas.create_text(
            width // 2, height // 2, text=text, fill=self.palette["field_fg"])

    def _draw_visualizer_frame(self, bars):
        """Draw one frame's bar heights (a list of `num_bars` floats in
        0..1) onto the visualizer canvas. Reuses the same rectangle item
        ids across frames (updating their coordinates/height rather than
        deleting and recreating them each tick) for smoother, cheaper
        redraws."""
        canvas = self._visualizer_canvas
        if canvas is None:
            return
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 1 or height <= 1:
            return

        num_bars = len(bars)
        gap = 2
        bar_width = max(1, (width - gap * (num_bars + 1)) / num_bars)
        bar_color = self.palette["highlight_bg"]

        if len(self._visualizer_bar_ids) != num_bars:
            canvas.delete("all")
            self._visualizer_bar_ids = [
                canvas.create_rectangle(0, 0, 0, 0, fill=bar_color, width=0)
                for _ in range(num_bars)]

        for i, level in enumerate(bars):
            bar_height = max(2, level * (height - 4))
            x0 = gap + i * (bar_width + gap)
            x1 = x0 + bar_width
            y1 = height
            y0 = height - bar_height
            canvas.coords(self._visualizer_bar_ids[i], x0, y0, x1, y1)
            canvas.itemconfig(self._visualizer_bar_ids[i], fill=bar_color)

    def on_play_pause_toggle(self):
        if self.is_playing and not self.is_paused:
            self.on_pause()
        else:
            self.on_play()

    def _update_play_pause_button(self):
        if self.is_playing and not self.is_paused:
            self.play_pause_button.config(text="Pause")
        else:
            self.play_pause_button.config(text="Play")

    def toggle_shuffle(self):
        self.shuffle_enabled = not self.shuffle_enabled
        self.shuffle_button.config(
            relief=tk.SUNKEN if self.shuffle_enabled else tk.RAISED)
        self.status_var.set(
            f"Shuffle {'On' if self.shuffle_enabled else 'Off'}")
        # The played-numbers pool is shared by both modes, so toggling
        # shuffle doesn't reset it: switching mid-album keeps picking from
        # whatever hasn't played yet, just in random order instead of in
        # sequence.
        logger.debug("toggle_shuffle: shuffle_enabled=%s played_numbers=%s",
                     self.shuffle_enabled, self.played_numbers)

    def toggle_repeat(self):
        self.repeat_enabled = not self.repeat_enabled
        self.repeat_button.config(
            relief=tk.SUNKEN if self.repeat_enabled else tk.RAISED)
        self.status_var.set(f"Repeat {'On' if self.repeat_enabled else 'Off'}")

    def on_play(self):
        if self.is_playing and self.is_paused:
            if self.audio_ready:
                try:
                    pygame.mixer.music.unpause()
                except Exception:
                    pass
            self.play_started_monotonic = time.monotonic()
            self.is_paused = False
            self.status_var.set(f"Playing: {self.now_title_var.get()}")
            self._update_play_pause_button()
            self._start_disk_spin()
            self._tick_progress()
            return

        selection = self.playlist_tree.selection()
        if selection:
            path = selection[0]
        elif self.player.playlist:
            index = self.player.current_index or 0
            path = self.player.playlist[index]
        else:
            self.status_var.set("Playlist is empty")
            return
        self._play_track(path)

    def on_pause(self):
        if not self.is_playing or self.is_paused:
            return
        if self.audio_ready:
            try:
                pygame.mixer.music.pause()
            except Exception:
                pass
        self.elapsed_before_pause += time.monotonic() - self.play_started_monotonic
        self.is_paused = True
        if self.progress_job is not None:
            self.root.after_cancel(self.progress_job)
            self.progress_job = None
        self._stop_disk_spin()
        self.status_var.set(f"Paused: {self.now_title_var.get()}")
        self._update_play_pause_button()

    def on_stop(self):
        if self.audio_ready:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
        if self.progress_job is not None:
            self.root.after_cancel(self.progress_job)
            self.progress_job = None
        self.is_playing = False
        self.is_paused = False
        self.elapsed_before_pause = 0.0
        self._set_progress(0)
        self._highlight_now_playing_row(None)
        self._reset_disk_spin()
        self.status_var.set("Stopped")
        self._update_play_pause_button()

    def on_previous(self):
        if not self.player.playlist:
            return
        path = self._pick_previous_path()
        logger.debug("on_previous: shuffle=%s repeat=%s picked=%s",
                     self.shuffle_enabled, self.repeat_enabled,
                     os.path.basename(path) if path else None)
        if path is not None:
            self._play_track(path, nav=True)
        else:
            self.status_var.set(f"Beginning of {self._scope_label()}")

    def on_next(self):
        if self.player.queue:
            path = self.player.queue.pop(0)
            self._refresh_queue_view()
            logger.debug("on_next: playing queued track %s",
                         os.path.basename(path))
            # Queued tracks are a side-channel: they don't reset/consume
            # the played-numbers pool, so sequential/shuffle playback
            # resumes exactly where it left off once the queue is empty.
            self._play_track(path, from_queue=True)
            return
        if not self.player.playlist:
            return
        path = self._pick_next_path()
        logger.debug("on_next: shuffle=%s repeat=%s picked=%s",
                     self.shuffle_enabled, self.repeat_enabled,
                     os.path.basename(path) if path else None)
        if path is not None:
            self._play_track(path, nav=True)
        else:
            self.on_stop()
            self.status_var.set(f"End of {self._scope_label()}")

    def _scope_label(self):
        if self.current_filter is None:
            return "playlist"
        kind, _value = self.current_filter
        return "folder" if kind == "paths" else kind

    def _current_scope_paths(self):
        """The ordered tracks the transport controls should navigate within.
        This follows the playlist table's current DISPLAY order (i.e.
        whatever column sort is active, or insertion order if none) --
        not the underlying playlist list's order -- so sequential
        Next/Previous plays tracks in the order they're shown on screen.
        Falls back to the raw filtered playlist if the tree isn't built
        yet (shouldn't normally happen during playback)."""
        if hasattr(self, "playlist_tree") and self.playlist_tree.winfo_exists():
            return list(self.playlist_tree.get_children())
        if self.current_filter is None:
            return list(self.player.playlist)
        return [p for p in self.player.playlist if self._track_passes_filter(p)]

    def _pick_next_path(self):
        """Advance to the next track (sequential or shuffle, both share the
        same played-numbers pool):
        - If Previous had rewound us into already-played history, step
          forward through that same history first.
        - Otherwise pick a track NUMBER that hasn't played yet (unless
          Repeat is on, in which case the played-numbers pool is bypassed
          entirely and any number is fair game again).
        - If every number in the current album/playlist has already played
          and Repeat is off, return None (stop at the end).
        """
        scope = self._current_scope_paths()
        logger.debug(
            "_pick_next_path: scope=%d track(s) shuffle=%s repeat=%s played_numbers=%s pos=%d",
            len(scope), self.shuffle_enabled, self.repeat_enabled,
            self.played_numbers, self.history_pos)
        if not scope:
            return None

        # Replay forward through history recorded from a previous rewind.
        if 0 <= self.history_pos < len(self.played_numbers) - 1:
            self.history_pos += 1
            number = self.played_numbers[self.history_pos]
            if number < len(scope):
                path = scope[number]
                logger.debug(
                    "_pick_next_path: replaying forward history -> #%d %s (pos %d/%d)",
                    number, os.path.basename(path), self.history_pos,
                    len(self.played_numbers) - 1)
                return path

        current_number = self._current_number_in_scope(scope)

        if self.repeat_enabled:
            # Repeat bypasses the played-numbers pool entirely: any number
            # is eligible again, so playback loops within the current
            # album/playlist instead of stopping.
            available = [i for i in range(len(scope))
                         if scope[i] not in self.player.ignored]
        else:
            played = set(self.played_numbers)
            available = [i for i in range(len(scope))
                         if i not in played and scope[i] not in self.player.ignored]
            if not available:
                logger.debug(
                    "_pick_next_path: all %d track number(s) already played, "
                    "repeat is off -> stop", len(scope))
                return None

        if not available:
            return None

        if self.shuffle_enabled:
            candidates = [n for n in available if n !=
                          current_number] or available
            number = random.choice(candidates)
        else:
            greater = [n for n in available if n > current_number]
            number = min(greater) if greater else min(available)

        if not self.repeat_enabled:
            # Only track played numbers when repeat is off -- this is the
            # pool that stops songs from repeating until the whole
            # album/playlist has played through once.
            self.played_numbers.append(number)
            self.history_pos = len(self.played_numbers) - 1
        path = scope[number]
        logger.debug(
            "_pick_next_path: new pick -> #%d %s (played_numbers now %s)",
            number, os.path.basename(path), self.played_numbers)
        return path

    def _current_number_in_scope(self, scope):
        """The currently-playing track's number (position) within `scope`,
        or -1 if it isn't playing / isn't part of this scope."""
        index = self.player.current_index
        if index is not None and 0 <= index < len(self.player.playlist):
            path = self.player.playlist[index]
            if path in scope:
                return scope.index(path)
        return -1

    # -- session cache (remembers library/playlist/backgrounds/settings) -
    def _restore_from_cache(self):
        """Re-populate the library/playlist, background images, and Now
        Playing bar from the previous session's saved state, if any.
        Library folders are (re)scanned in the BACKGROUND (see
        _start_library_scan) so a large library doesn't freeze the
        window on launch -- the previously-playing track (if any) is
        loaded synchronously first (just one file), so the Now Playing
        bar still restores immediately without waiting on those scans."""
        cache = self.cache
        if not cache:
            return

        now_playing = cache.get("now_playing") or {}
        now_playing_path = now_playing.get("path")
        if now_playing_path and os.path.isfile(now_playing_path):
            self._add_track(now_playing_path)

        for root_dir in sorted(cache.get("library_roots", []), key=len):
            if os.path.isdir(root_dir):
                self._add_library_folder(root_dir, announce=False, log=False)

        # Any individually-opened files (not under a library folder) are
        # saved separately; _add_track is a no-op for paths already added
        # by the library-folder scan above.
        for path in cache.get("playlist", []):
            if os.path.isfile(path):
                self._add_track(path)

        for cue_path in cache.get("playlist_cue_paths", []):
            if os.path.isfile(cue_path):
                self._load_playlist_from_cue(cue_path, is_restore=True)

        right_bg_path = cache.get("right_box_bg_path")
        if right_bg_path and os.path.isfile(right_bg_path):
            try:
                image = Image.open(right_bg_path)
                self.right_box_bg_source_image = image
                self.right_box_bg_path = right_bg_path
                self._set_right_box_background(image)
            except Exception:
                pass

        playlist_bg_path = cache.get("playlist_bg_path")
        if playlist_bg_path and os.path.isfile(playlist_bg_path):
            try:
                image = Image.open(playlist_bg_path)
                self.playlist_bg_source_image = image
                self.playlist_bg_path = playlist_bg_path
                self._apply_playlist_background()
            except Exception:
                pass

        if now_playing_path and now_playing_path in self.player.playlist:
            self._restore_now_playing_display(
                now_playing_path, now_playing.get("elapsed", 0.0))

        if self._library_scan_active:
            self.status_var.set(
                "Restored previous session -- loading library in background...")
        else:
            self.status_var.set("Restored previous session")

    def _restore_now_playing_display(self, path, elapsed):
        """Restore the Now Playing bar to show the last-played track (art,
        title, progress) in a paused state at its last position, without
        auto-starting playback -- so the previous session is visible but
        nothing plays unexpectedly on launch. Pressing Play resumes it."""
        self.player.current_index = self.player.playlist.index(path)
        album = self.track_tags.get(path, {}).get("album", "")
        self.current_filter = ("album", album) if album else None
        self._refresh_playlist_view()

        artist, title, _album, _dur = read_track_tags(path)
        self.current_duration = get_track_duration(path)
        elapsed = max(0.0, min(elapsed, self.current_duration or elapsed))

        self._current_art_pil = get_track_art_pil(
            path) or make_placeholder_art_pil()
        self._disk_angle = 0
        self._set_disk_base(self._current_art_pil)
        self._show_static_art_frame()
        self._update_dynamic_theme_from_art(self._current_art_pil)

        self.now_title_var.set(title)
        self.now_artist_var.set(artist or "Unknown Artist")
        self.now_playing_progress.config(maximum=max(self.current_duration, 1))
        self.duration_var.set(format_duration(self.current_duration) or "0:00")

        if self.audio_ready:
            try:
                pygame.mixer.music.load(path)
                pygame.mixer.music.play(start=elapsed)
                pygame.mixer.music.pause()
            except Exception:
                pass

        self.elapsed_before_pause = elapsed
        self.play_started_monotonic = time.monotonic()
        self.is_playing = True
        self.is_paused = True
        self._set_progress(elapsed)
        self.status_var.set(f"Resumed session (paused): {title}")
        self._update_play_pause_button()

    def _save_cache(self):
        """Persist the current session (settings, library/playlist,
        background images, and the currently-playing track/position) so
        it can be restored next launch."""
        now_playing = None
        if self.player.current_index is not None and self.player.playlist:
            path = self.player.playlist[self.player.current_index]
            elapsed = self.elapsed_before_pause
            if self.is_playing and not self.is_paused:
                elapsed += time.monotonic() - self.play_started_monotonic
            now_playing = {"path": path, "elapsed": elapsed}

        data = {
            "theme_name": self.theme_name,
            "disk_spin_enabled": self.disk_spin_enabled,
            "browsing_mode": self.browsing_mode,
            "dynamic_theme_enabled": self.dynamic_theme_enabled,
            "library_log_path": self.library_log_path,
            "library_roots": self._deduped_library_roots(),
            "playlist": list(self.player.playlist),
            "right_box_bg_path": self.right_box_bg_path,
            "playlist_bg_path": self.playlist_bg_path,
            "now_playing": now_playing,
            "playlist_columns": [
                key for key, visible in self.playlist_column_visible.items() if visible],
            "playlist_cue_paths": list(self.playlists.keys()),
        }
        save_cache(data)

    def _deduped_library_roots(self):
        """`self.player.library_roots` can end up with a folder listed
        both on its own and nested inside another already-tracked root
        (e.g. an album archive imported into a subfolder of an already-
        open library folder). Only keep the outermost folders so the
        cache doesn't store redundant/overlapping roots."""
        roots = sorted(set(self.player.library_roots), key=len)
        deduped = []
        for root_dir in roots:
            root_with_sep = root_dir.rstrip(os.sep) + os.sep
            if not any(root_dir == other or root_with_sep.startswith(
                    other.rstrip(os.sep) + os.sep) for other in deduped):
                deduped.append(root_dir)
        return deduped

    def _on_close(self):
        self._save_cache()
        self.root.destroy()

    def _relaunch_app(self):
        """"Refresh App" (File menu): save the current session (same as a
        normal close) then relaunch the whole process from scratch --
        i.e. a full app restart, restoring from the cache we just wrote
        the same way a normal launch would."""
        self._save_cache()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _pick_previous_path(self):
        """Step back to the previously played track number (sequential or
        shuffle both share the same history), or None if there is nothing
        earlier recorded. While Repeat is on, played numbers aren't tracked,
        so this can only rewind through history recorded before Repeat was
        turned on."""
        scope = self._current_scope_paths()
        logger.debug(
            "_pick_previous_path: scope=%d track(s) shuffle=%s repeat=%s played_numbers=%s pos=%d",
            len(scope), self.shuffle_enabled, self.repeat_enabled,
            self.played_numbers, self.history_pos)
        if not scope or self.history_pos <= 0:
            logger.debug(
                "_pick_previous_path: no earlier history (pos=%d, played_numbers=%s)",
                self.history_pos, self.played_numbers)
            return None
        self.history_pos -= 1
        number = self.played_numbers[self.history_pos]
        if number >= len(scope):
            return None
        path = scope[number]
        logger.debug("_pick_previous_path: rewound to #%d %s (pos=%d)",
                     number, os.path.basename(path), self.history_pos)
        return path
