"""Debug logging for playback navigation (shuffle/repeat/next/previous).

Prints to the console the app was launched from; set the level to
logging.INFO (or higher) to silence it once you're done debugging.
"""

import logging

logging.basicConfig(level=logging.DEBUG,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("tiny_player")
