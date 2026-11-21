"""Package Playerton into a standalone Windows app with PyInstaller.

Usage (from this folder):
    python build_app.py            -> dist/Playerton/Playerton.exe
                                      (folder app, fast startup -- default)
    python build_app.py --onefile  -> dist/Playerton.exe
                                      (single file, slower startup)

Requires: pip install pyinstaller  (plus the app's own deps: pygame,
pillow, mutagen).

App icon, in priority order:
  assets/icon.gif  Animated! Plays in the title bar + taskbar while the app
                   runs. Its FIRST frame becomes icon.ico (the .exe's icon
                   in Explorer / shortcuts, which Windows can't animate).
  assets/icon.png  Converted to icon.ico on every build.
  assets/icon.ico  Used as-is.
Any size works (non-square images are padded, not stretched). A default
vinyl-disc icon is generated only if none of these exist.
"""

import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(HERE, "assets", "icon.ico")
ICON_PNG_PATH = os.path.join(HERE, "assets", "icon.png")
ICON_GIF_PATH = os.path.join(HERE, "assets", "icon.gif")
ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
              (128, 128), (256, 256)]


def make_icon(path):
    """Draw a simple vinyl record icon (matching the Now Playing spinning
    disc) at several sizes and save it as a multi-resolution .ico."""
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, size - 8, size - 8), fill="#1b1b1b", outline="#f2f2f2", width=8)
    for inset in (40, 60, 80):  # grooves
        d.ellipse((inset, inset, size - inset, size - inset),
                  outline="#3a3a3a", width=3)
    d.ellipse((92, 92, size - 92, size - 92), fill="#d97b29")  # label
    d.ellipse((120, 120, size - 120, size - 120), fill="#f2f2f2")  # spindle hole
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, sizes=ICON_SIZES)


def icon_from_png(png_path, ico_path):
    """Convert a user-supplied image into a multi-size .ico (padded to a
    square with transparency so it isn't stretched)."""
    img = Image.open(png_path)
    img.seek(0)  # first frame, for animated GIFs
    img = img.convert("RGBA")
    side = max(img.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    square.save(ico_path, sizes=ICON_SIZES)


def main():
    """Prepare the icon (GIF > PNG > existing ICO > generated default),
    then run PyInstaller with the options below."""
    if os.path.isfile(ICON_GIF_PATH):
        icon_from_png(ICON_GIF_PATH, ICON_PATH)
        print("Using icon from", ICON_GIF_PATH, "(animated in the app)")
    elif os.path.isfile(ICON_PNG_PATH):
        icon_from_png(ICON_PNG_PATH, ICON_PATH)
        print("Using icon from", ICON_PNG_PATH)
    elif not os.path.isfile(ICON_PATH):
        make_icon(ICON_PATH)

    import PyInstaller.__main__

    one_file = "--onefile" in sys.argv
    args = [
        os.path.join(HERE, "file_explorer.py"),
        "--name", "Playerton",
        "--windowed",               # no console window
        "--noconfirm",
        "--clean",
        "--icon", ICON_PATH,
        # miniaudio's compiled module imports cffi's backend from C code,
        # which PyInstaller can't see -- without this the EQ engine would
        # fail to load in the packaged app (and quietly fall back to pygame).
        "--hidden-import", "_cffi_backend",
        # Bundled so the window/taskbar icon works inside the app too.
        "--add-data", f"{ICON_PATH}{os.pathsep}assets",
    ]
    if os.path.isfile(ICON_GIF_PATH):
        args += ["--add-data", f"{ICON_GIF_PATH}{os.pathsep}assets"]
    args += [
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", os.path.join(HERE, "build"),
        "--onefile" if one_file else "--onedir",
    ]
    PyInstaller.__main__.run(args)
    target = "Playerton.exe" if one_file else os.path.join("Playerton", "Playerton.exe")
    print("\nBuilt:", os.path.join(HERE, "dist", target))


if __name__ == "__main__":
    main()
