# Playerton

A desktop music player for Windows, written in Python with Tkinter.

It shows your music folders as a library tree, plays from a sortable playlist
table, and has a Now Playing bar with the album art drawn as a spinning disc.

## Features

- **Library and playlists**: folder tree with search, Collapse All / Expand All,
  a sortable playlist table with selectable columns, a play-next queue, and
  playlists saved as `.cue` files.
- **Album import**: imports `.zip` album archives (named `Artist - Album.zip`)
  into `Artist/Year - Album/` folders, and can delete the zip afterwards.
- **Equalizer**: 10 bands with built-in and custom presets, applied live.
  Works on FLAC, MP3, WAV and OGG.
- **Lyrics panel**: reads `.lrc` files and embedded lyrics, can look songs up on
  [LRCLIB](https://lrclib.net), highlights synced lyrics as the song plays, and
  seeks when you click a line. The **Lyrics** button shows or hides it.
- **Album Info**: a button (and a right-click menu item) with links to the album
  on RateYourMusic, Discogs, MusicBrainz, AllMusic, Last.fm, Bandcamp and
  Wikipedia, plus the album's own pages found through MusicBrainz.
- **Look**: several themes, a live custom theme editor (colors, fonts, imported
  font files), colors taken from a background photo, and a title bar and menu bar
  that match the theme.
- **Other**: drag-to-scrub seek slider, an optional animated GIF window icon,
  tag editing (one track or many), and the last session restored on launch.

## Running from source

Requires **Python 3.12** from [python.org](https://www.python.org/), which
includes Tkinter.

```
pip install -r requirements.txt
python file_explorer.py
```

## Building the app

```
python build_app.py
```

This creates `dist/Playerton/Playerton.exe`. Keep the `_internal` folder next to
the exe. `python build_app.py --onefile` builds a single `.exe` instead; it
starts more slowly.

The icon comes from `assets/`. The build uses `icon.gif` first (animated in the
running app), then `icon.png`, then `icon.ico`.

`build/` and `dist/` are not in git. Build your own copy after pulling.

## Where things are stored

Settings, the saved session, playlists, imported fonts, the lyrics cache and
`playerton.log` are stored in your user folder at `~/.config/tiny_player/`, not
in the project. That folder kept the app's old name on purpose: saved playlists
contain absolute paths into it, so renaming it would break them.

## Project layout

| File | What it does |
|---|---|
| `file_explorer.py` | Entry point: run this to start the player |
| `app.py` | The whole UI and player logic (`App`), plus the theme editor, equalizer and Album Info windows. A table of contents at the top lists its sections. |
| `styles.py` | All appearance: color themes, fonts, and the CSS-like `STYLESHEET` |
| `audio_engine.py` | Audio output: pygame normally, the miniaudio + numpy equalizer engine when the EQ is on |
| `lyrics.py` | Finds lyrics (`.lrc` file, embedded, LRCLIB) and parses synced lyrics |
| `album_info.py` | Album Info links: site searches and the MusicBrainz lookup |
| `audio_tags.py` | Tag reading and writing, album art |
| `image_utils.py` | Background photo fitting and fading, palette from an image |
| `playlist_cue.py` | Reads and writes playlists as `.cue` files |
| `archive_utils.py` | Detects `.zip` album archives and parses their names |
| `state_cache.py` | Saves and loads the session between launches |
| `player_state.py`, `constants.py`, `logging_setup.py` | Small shared pieces |
| `build_app.py` | Packages the app with PyInstaller |

Every function has a docstring explaining what it does.

## Notes

- **Privacy:** online lyrics lookup sends only a song's artist, title, album and
  length to lrclib.net. Turn it off in **Settings > Fetch Lyrics Online**.
  Opening Album Info sends the artist and album name to musicbrainz.org. The
  search links send nothing until you click one.
- **Logs:** the packaged app writes warnings to `playerton.log` in the settings
  folder. When run from source, debug output goes to the console.

## Working together

Make changes on a branch and open a pull request into `main`. Don't
force-push to `main`.
