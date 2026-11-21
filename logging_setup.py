"""Logging. Running from source: debug output (playback navigation etc.)
printed to the console. Packaged app: warnings/errors only, written to
playerton.log in the settings folder (~/.config/tiny_player/).
"""

import logging
import sys

# The packaged app (PyInstaller, no console window) has nowhere to print
# to, so only warnings are kept there; running from source stays verbose.
_FROZEN = getattr(sys, "frozen", False)
if _FROZEN:
    # No console in the packaged app: keep warnings/errors in a small log
    # file next to the session cache (overwritten each launch), so problems
    # like the EQ engine failing to start can still be diagnosed.
    import os
    from state_cache import CACHE_DIR
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        logging.basicConfig(
            level=logging.WARNING, filename=os.path.join(CACHE_DIR, "playerton.log"),
            filemode="w", encoding="utf-8",
            format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    except OSError:
        logging.basicConfig(level=logging.WARNING)
else:
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
logger = logging.getLogger("playerton")
