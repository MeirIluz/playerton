"""Global media keys (Windows): the keyboard's Play/Pause, Next, Previous
and Stop keys control Playerton even when its window isn't focused.

How: the keys are registered as system-wide hotkeys with the Win32
RegisterHotKey API. Windows then delivers each press as a WM_HOTKEY
message -- to the thread that registered it, which needs its own message
loop. So a small background thread registers the keys and waits in
GetMessage; each press is put on a queue that the app's UI thread polls
(Tk must only be touched from the UI thread).

While registered, the media keys go to Playerton instead of other players
(Spotify, browser tabs, ...); App lets the user turn this off in Settings,
which unregisters them. Non-Windows systems: start() does nothing.
"""

import ctypes
import ctypes.wintypes
import queue
import sys
import threading

# Virtual-key codes of the keyboard media keys -> the action they trigger.
MEDIA_KEYS = {
    0xB3: "play_pause",  # VK_MEDIA_PLAY_PAUSE
    0xB0: "next",        # VK_MEDIA_NEXT_TRACK
    0xB1: "previous",    # VK_MEDIA_PREV_TRACK
    0xB2: "stop",        # VK_MEDIA_STOP
}
_MOD_NOREPEAT = 0x4000   # holding a key down doesn't fire repeatedly
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012


class GlobalMediaKeys:
    """Owns the hotkey thread. `actions` is a queue of action names
    ("play_pause", "next", ...) for the UI thread to consume."""

    def __init__(self):
        """Not started yet; call start()."""
        self.actions = queue.Queue()
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self.failed_keys = []  # actions whose key another app already owns

    @property
    def running(self):
        """True while the hotkeys are registered."""
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        """Register the media keys (on the background thread). Returns the
        list of actions that couldn't be registered (empty = all good)."""
        if sys.platform != "win32" or self.running:
            return []
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="media-keys", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)  # until registration has been attempted
        return list(self.failed_keys)

    def stop(self):
        """Unregister the keys and end the thread (they go back to other
        apps)."""
        if not self.running or self._thread_id is None:
            return
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        self._thread.join(2.0)
        self._thread = None
        self._thread_id = None

    def _run(self):
        """Background thread: register hotkeys, then pump messages until
        WM_QUIT, queueing an action for every WM_HOTKEY."""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        registered = []
        self.failed_keys = []
        for hotkey_id, (vk, action) in enumerate(MEDIA_KEYS.items(), start=1):
            if user32.RegisterHotKey(None, hotkey_id, _MOD_NOREPEAT, vk):
                registered.append(hotkey_id)
            else:
                self.failed_keys.append(action)
        self._ready.set()

        ids = {i: action for i, action in enumerate(MEDIA_KEYS.values(), start=1)}
        msg = ctypes.wintypes.MSG()
        try:
            # GetMessageW returns 0 on WM_QUIT and -1 on error.
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == _WM_HOTKEY and msg.wParam in ids:
                    self.actions.put(ids[msg.wParam])
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)

