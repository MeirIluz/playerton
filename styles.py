"""A CSS-like style template describing the look (colors, fonts, padding,
relief, cursor) of every button, label, treeview, and other widget in the
app. This is the SINGLE place to look to restyle the app or add a new
color theme -- nothing about appearance lives in constants.py anymore.

To add your own theme: copy one of the dicts in THEMES below (e.g.
DARK_THEME), rename it, tweak whichever hex colors you like, and add it
to the THEMES dict at the bottom under a new short name -- it'll
immediately show up as a new option (title-cased) in the View menu with
no other code changes required. Every theme dict must define the same
set of keys (see PALETTE_KEYS just below THEMES for what each one is
for and where it's used).

This mirrors how a CSS stylesheet assigns rules to selectors:
  - Bare names like "TButton" or "TLabel" are ttk's built-in widget
    classes -- every ttk widget that doesn't ask for something more
    specific (i.e. is created without a `style=` option) picks up these
    rules, the same way a CSS element selector (e.g. `button { ... }`)
    applies to every `<button>` by default.
  - Names like "Transport.TButton" or "NowPlayingTitle.TLabel" are more
    specific ttk style variants, analogous to CSS classes -- individual
    widgets opt in via their `style="Transport.TButton"` option to get a
    distinct look (larger font, different padding, ...) layered on top
    of the base class's rules (ttk resolves any property a variant
    doesn't override from its base class, e.g. "Transport.TButton" falls
    back to "TButton" for anything not listed here).
  - Names starting with "#" (e.g. "#play_pause_button") target one
    specific PLAIN tk widget (tk.Button/tk.Label/tk.Canvas) by the
    attribute name it's stored under on the App instance. Plain tk
    widgets exist alongside ttk ones because a couple of widgets
    (the Play/Pause, Stop, Add to Queue, Shuffle, and Repeat buttons)
    need per-instance relief/state control that's awkward with ttk, so
    they're left as classic tk widgets and styled directly instead.

Each rule is a dict of property -> value. Values for COLOR properties
(see `_COLOR_PROPS` below) are written as palette KEY NAMES (strings
matching a key in one of THEMES's palettes below, e.g. "bg",
"button_fg", "select_bg") and are resolved to the actual hex color at
apply-time by `resolve_style()`; all other values (fonts, padding,
relief, cursor, row height, ...) are literal and applied as-is,
regardless of the active theme.

Not covered here (handled separately, in `App._apply_theme`, since they
need logic beyond "set these properties"):
  - tk.Menu widgets (colored via `App._menu_colors()`).
  - The right-side Now Playing box's canvas + background photo blending
    (`App._build_right_box`/`_set_right_box_background`), and the
    playlist table's background photo (`App._apply_playlist_background`).
  - Row highlighting tags ("now_playing"/"in_queue" on the playlist
    table, "filter_match" on the library tree) -- these represent
    transient PLAYBACK/SELECTION STATE, not a fixed widget look, and are
    applied via `tag_configure` rather than a style rule.
"""

# ---------------------------------------------------------------------
# THEMES: every color scheme the app can use, keyed by the short name
# shown (title-cased) in the View menu. This is the part most people
# customizing the app will want to edit -- just add/edit a dict below.
#
# PALETTE_KEYS -- what each key in a theme dict means and where it shows
# up in the app:
#   bg            General window/frame/label background.
#   fg            General text color (labels, menu text, headings).
#   field_bg      Background of "content" areas: the playlist/library/
#                 queue tables and text-entry boxes.
#   field_fg      Text color inside those content areas.
#   select_bg     Background of a selected row, and buttons on hover/press.
#   select_fg     Text color of a selected row/hovered button.
#   button_bg     Background of ordinary buttons and column/table headers.
#   button_fg     Text color of ordinary buttons and column/table headers.
#   trough_bg     The "track" behind the volume/seek sliders and
#                 progress bar, and behind scrollbars.
#   highlight_bg  Background of the currently-PLAYING track's row, and of
#                 the library folder matching an active "Go to Album"/
#                 "More by Same Artist" filter.
#   highlight_fg  Text color for the same "now playing" highlight.
#   queue_bg      Background for a track's row when it's sitting in the
#                 queue (but not the one currently playing).
#   queue_fg      Text color for that queued-track highlight.
# ---------------------------------------------------------------------
LIGHT_THEME = {
    "bg": "#f0f0f0",
    "fg": "#000000",
    "field_bg": "#ffffff",
    "field_fg": "#000000",
    "select_bg": "#0078d7",
    "select_fg": "#ffffff",
    "button_bg": "#e1e1e1",
    "button_fg": "#000000",
    "trough_bg": "#d9d9d9",
    "highlight_bg": "#f3e2b3",
    "highlight_fg": "#4a3b0d",
    "queue_bg": "#d6e8e4",
    "queue_fg": "#204a42",
}

DARK_THEME = {
    "bg": "#2b2b2b",
    "fg": "#e0e0e0",
    "field_bg": "#1e1e1e",
    "field_fg": "#e0e0e0",
    "select_bg": "#3a6ea5",
    "select_fg": "#ffffff",
    "button_bg": "#3c3c3c",
    "button_fg": "#e0e0e0",
    "trough_bg": "#1e1e1e",
    "highlight_bg": "#5a4a26",
    "highlight_fg": "#f0e0b0",
    "queue_bg": "#2a4440",
    "queue_fg": "#cdeee6",
}

DARK_BLUE_THEME = {
    "bg": "#0d1b2a",
    "fg": "#dce6f0",
    "field_bg": "#0a1622",
    "field_fg": "#dce6f0",
    "select_bg": "#1b5e91",
    "select_fg": "#ffffff",
    "button_bg": "#1b2f45",
    "button_fg": "#dce6f0",
    "trough_bg": "#0a1622",
    "highlight_bg": "#4a3f26",
    "highlight_fg": "#f0e0b0",
    "queue_bg": "#1f3a45",
    "queue_fg": "#cdeee6",
}

# Warm brown/orange theme with a sky-blue accent (used for the selection
# highlight and the queue marker, to stand out against the warm palette).
SUNSET_THEME = {
    "bg": "#3b2a1e",
    "fg": "#f0d9c0",
    "field_bg": "#2a1d14",
    "field_fg": "#f5e6d3",
    "select_bg": "#4fa8d8",
    "select_fg": "#1a1a1a",
    "button_bg": "#d97b29",
    "button_fg": "#2a1a0a",
    "trough_bg": "#5c4530",
    "highlight_bg": "#e8a33d",
    "highlight_fg": "#2a1a0a",
    "queue_bg": "#7ec8e3",
    "queue_fg": "#123047",
}

# DOOM-inspired theme: near-black metal/UAC gray backdrop with blood-red
# accents and a fire-orange highlight for the currently-playing track
# (evoking the series' iconic black/red UI and lava/fire palette).
DOOM_THEME = {
    "bg": "#0d0505",
    "fg": "#e02020",
    "field_bg": "#140707",
    "field_fg": "#d8b8b8",
    "select_bg": "#8a0303",
    "select_fg": "#ffffff",
    "button_bg": "#2a1010",
    "button_fg": "#e02020",
    "trough_bg": "#1a0a0a",
    "highlight_bg": "#c41e1e",
    "highlight_fg": "#000000",
    "queue_bg": "#4a1414",
    "queue_fg": "#ffb347",
}

# Maps a theme name -> its palette dict; drives the View > Theme menu
# (one radiobutton per entry, in this order) and every color lookup in
# STYLESHEET below. Add a new theme by adding an entry here.
THEMES = {
    "light": LIGHT_THEME,
    "dark": DARK_THEME,
    "dark_blue": DARK_BLUE_THEME,
    "sunset": SUNSET_THEME,
    "doom": DOOM_THEME,
}

# Every key a theme dict must define (used only for validation/reference;
# see the PALETTE_KEYS comment block above for what each one means).
PALETTE_KEYS = (
    "bg", "fg", "field_bg", "field_fg", "select_bg", "select_fg",
    "button_bg", "button_fg", "trough_bg",
    "highlight_bg", "highlight_fg", "queue_bg", "queue_fg",
)

# Fonts are deliberately left at the platform default family (""), only
# varying size/weight, so the app doesn't depend on any specific font
# being installed.
FONTS = {
    "base": ("", 9),
    "small": ("", 8),
    "heading": ("", 10, "bold"),
    "title": ("", 13, "bold"),
    "subtitle": ("", 9),
    "button": ("", 9),
    "nav_button": ("", 8),
    "time": ("", 9),
}

# Property names treated as theme colors: their STYLESHEET value is a
# palette key name (e.g. "bg") to be looked up, not a literal color.
_COLOR_PROPS = {
    "background", "foreground", "fieldbackground", "troughcolor",
    "bg", "fg", "activebackground", "activeforeground",
    "highlightbackground", "highlightcolor", "selectbackground",
    "selectforeground", "insertbackground",
}

STYLESHEET = {
    # -- generic ttk widget classes (the default look for every plain
    # ttk widget of that type, unless it opts into a more specific
    # style below via `style="X.TButton"` etc.) -----------------------
    "TFrame": {
        "background": "bg",
    },
    "TLabel": {
        "background": "bg",
        "foreground": "fg",
        "font": FONTS["base"],
    },
    "TButton": {
        "background": "button_bg",
        "foreground": "button_fg",
        "font": FONTS["button"],
        "padding": (6, 3),
    },
    "TCheckbutton": {
        "background": "bg",
        "foreground": "fg",
        "font": FONTS["base"],
    },
    "TScale": {
        "background": "bg",
    },
    "TProgressbar": {
        "background": "select_bg",
        "troughcolor": "trough_bg",
    },
    "TLabelframe": {
        "background": "bg",
        "foreground": "fg",
    },
    "TLabelframe.Label": {
        "background": "bg",
        "foreground": "fg",
        "font": FONTS["heading"],
    },
    "TEntry": {
        "fieldbackground": "field_bg",
        "foreground": "field_fg",
        "font": FONTS["base"],
    },
    "TPanedwindow": {
        "background": "bg",
    },
    "TScrollbar": {
        "background": "button_bg",
        "troughcolor": "trough_bg",
    },
    "Treeview": {
        "background": "field_bg",
        "fieldbackground": "field_bg",
        "foreground": "field_fg",
        "font": FONTS["base"],
        "rowheight": 22,
    },
    "Treeview.Heading": {
        "background": "button_bg",
        "foreground": "fg",
        "font": FONTS["heading"],
    },

    # -- specific ttk style variants (opt-in via a widget's `style=`) --
    "Transport.TButton": {
        # Toolbar's |<< / Play / Pause / Stop / >>| buttons.
        "font": FONTS["button"],
        "padding": (5, 3),
    },
    "NavBox.TButton": {
        # The Now Playing bar's compact right-side |<< / >>| buttons.
        "font": FONTS["nav_button"],
        "padding": (2, 1),
    },
    "PanelAction.TButton": {
        # "Set/Clear Background..." buttons and the queue panel's
        # Up/Down/Remove/Clear buttons.
        "font": FONTS["small"],
        "padding": (4, 2),
    },
    "NowPlayingTitle.TLabel": {
        "background": "bg",
        "foreground": "fg",
        "font": FONTS["title"],
    },
    "NowPlayingArtist.TLabel": {
        "background": "bg",
        "foreground": "fg",
        "font": FONTS["subtitle"],
    },
    "ProgressTime.TLabel": {
        # The elapsed/duration ("0:00") labels either side of the
        # progress bar.
        "background": "bg",
        "foreground": "fg",
        "font": FONTS["time"],
    },
    "ToolbarLabel.TLabel": {
        # The toolbar's "Vol" label.
        "background": "bg",
        "foreground": "fg",
        "font": FONTS["small"],
    },
    "QueueHeader.TLabel": {
        # The queue panel's "Queue" header label.
        "background": "bg",
        "foreground": "fg",
        "font": FONTS["heading"],
    },
    "StatusBar.TLabel": {
        "background": "bg",
        "foreground": "fg",
        "font": FONTS["small"],
        "relief": "sunken",
        "padding": (4, 2),
    },
    "ViewingFolder.TLabel": {
        # The transient "Currently viewing folder: ..." label shown
        # above the playlist table, which fades out a couple seconds
        # after appearing (see App._show_viewing_folder_label).
        "background": "bg",
        "foreground": "fg",
        "font": FONTS["small"],
        "padding": (4, 2),
    },
    "Library.Treeview": {
        "font": FONTS["base"],
        "rowheight": 20,
    },
    "Queue.Treeview": {
        "font": FONTS["base"],
        "rowheight": 20,
    },

    # -- plain tk widgets, targeted by their App attribute name --------
    "#play_pause_button": {
        "font": FONTS["button"],
        "bg": "button_bg", "fg": "button_fg",
        "activebackground": "select_bg", "activeforeground": "select_fg",
        "highlightbackground": "bg",
        "relief": "raised", "cursor": "hand2",
    },
    "#stop_button": {
        "font": FONTS["button"],
        "bg": "button_bg", "fg": "button_fg",
        "activebackground": "select_bg", "activeforeground": "select_fg",
        "highlightbackground": "bg",
        "relief": "raised", "cursor": "hand2",
    },
    "#add_queue_button": {
        "font": FONTS["button"],
        "bg": "button_bg", "fg": "button_fg",
        "activebackground": "select_bg", "activeforeground": "select_fg",
        "highlightbackground": "bg",
        "relief": "raised", "cursor": "hand2",
    },
    "#shuffle_button": {
        # No "relief" here on purpose: toggle_shuffle() flips it between
        # sunken/raised to show on/off state, and _apply_theme() runs on
        # every theme switch too -- reapplying a fixed relief here would
        # wipe out that toggle state whenever the theme changes.
        "font": FONTS["button"],
        "bg": "button_bg", "fg": "button_fg",
        "activebackground": "select_bg", "activeforeground": "select_fg",
        "highlightbackground": "bg",
        "cursor": "hand2",
    },
    "#repeat_button": {
        "font": FONTS["button"],
        "bg": "button_bg", "fg": "button_fg",
        "activebackground": "select_bg", "activeforeground": "select_fg",
        "highlightbackground": "bg",
        "cursor": "hand2",
    },
    "#art_label": {
        "bg": "field_bg",
    },
}


def resolve_style(props, palette):
    """Resolve a STYLESHEET rule's color placeholders (palette key names)
    against `palette`, returning a plain kwargs dict ready to pass to
    `widget.configure(**...)` or `ttk.Style().configure(name, **...)`.
    Non-color properties (font, padding, relief, cursor, row height, ...)
    are passed through unchanged."""
    resolved = {}
    for prop, value in props.items():
        if prop in _COLOR_PROPS and isinstance(value, str):
            resolved[prop] = palette.get(value, value)
        else:
            resolved[prop] = value
    return resolved
