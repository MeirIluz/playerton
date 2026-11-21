"""Audio output for Playerton, with an optional 10-band equalizer.

The app talks to one object, AudioOutput, through the same small set of
calls pygame's music player has (load / play / pause / unpause / stop /
set_volume). Behind it there are two backends:

  * pygame (SDL_mixer)  -- used whenever the EQ is off. Pure compiled code,
                           zero extra cost; exactly how Playerton always
                           played audio.
  * EqPlayer            -- used while the EQ is on. miniaudio decodes the
                           file, this module applies the EQ with numpy, and
                           miniaudio plays the result. numpy/miniaudio are
                           only imported the first time the EQ is used, so
                           with the EQ off they cost nothing (not even
                           startup time).

Files EqPlayer can't decode (miniaudio handles FLAC, MP3, WAV and OGG
Vorbis) automatically fall back to pygame, just without the EQ.

How the EQ works (EqPlayer):
  The 10 band gains (31 Hz ... 16 kHz, in dB) are turned into one smooth
  frequency-response curve, and from that into a linear-phase FIR filter
  (EQ_TAPS long). Audio is filtered in fixed blocks with FFT "overlap-add"
  convolution: FFT the block, multiply by the filter's spectrum, inverse
  FFT, and add the overlapping tail of the previous block. That costs a
  few FFTs per ~0.1 s of audio -- far cheaper than running 10 biquad
  filters sample by sample in Python. When the gains change, the next
  block is filtered with both the old and new filter and crossfaded, so
  moving a slider never clicks. Boosting bands lowers the overall level
  by the largest boost ("headroom"), so loud music doesn't clip.
"""

import collections
import os
import time

import pygame

from logging_setup import logger

# Center frequencies (Hz) of the 10 EQ bands, low to high.
EQ_BANDS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
EQ_MIN_DB, EQ_MAX_DB = -12.0, 12.0

# Built-in presets: band gains in dB, in EQ_BANDS order.
EQ_PRESETS = {
    "Flat":         [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "Bass Boost":   [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    "Treble Boost": [0, 0, 0, 0, 0, 1, 2, 4, 5, 6],
    "Rock":         [4, 3, 2, 0, -1, -1, 1, 2, 3, 4],
    "Pop":          [-1, 1, 2, 3, 2, 0, -1, -1, 0, 1],
    "Jazz":         [3, 2, 1, 2, -1, -1, 0, 1, 2, 3],
    "Classical":    [3, 2, 1, 0, 0, 0, -1, -1, 1, 2],
    "Electronic":   [5, 4, 1, 0, -2, 1, 0, 1, 3, 4],
    "Hip-Hop":      [5, 4, 2, 3, -1, -1, 1, -1, 1, 2],
    "Vocal":        [-2, -2, -1, 1, 3, 3, 2, 1, 0, -1],
    "Acoustic":     [3, 2, 1, 1, 2, 2, 2, 2, 1, 1],
    "Loudness":     [5, 3, 0, 0, -1, 0, -1, 1, 4, 3],
}

# File types the EQ engine (miniaudio) can decode.
EQ_EXTENSIONS = (".flac", ".mp3", ".wav", ".ogg")

SAMPLE_RATE = 44100      # everything is decoded/resampled to this rate
CHANNELS = 2
EQ_BLOCK = 4096          # frames filtered per FFT block (~93 ms)
EQ_TAPS = 4097           # FIR filter length (odd => exact linear phase)
EQ_FFT = 8192            # FFT size: EQ_BLOCK + EQ_TAPS - 1
DEVICE_BUFFER_MS = 150   # output buffer; absorbs short stalls of the UI thread


def clamp_gains(gains):
    """Return exactly 10 gains (dB), each clamped to the allowed range."""
    gains = list(gains or [])[:len(EQ_BANDS)]
    gains += [0.0] * (len(EQ_BANDS) - len(gains))
    return [max(EQ_MIN_DB, min(EQ_MAX_DB, float(g))) for g in gains]


class EqPlayer:
    """Decodes a file with miniaudio, applies the EQ, and plays it through
    a miniaudio output device. One instance is reused for every track.

    Thread note: miniaudio calls `_callback` (a generator) on its own audio
    thread to ask for samples. The UI thread only ever swaps whole objects
    (`_pending_spectrum`, `volume`) that the audio thread picks up on its
    next block, so no locks are needed."""

    def __init__(self):
        """Import numpy/miniaudio (first EQ use only) and start flat."""
        import miniaudio
        import numpy as np
        self._ma = miniaudio
        self._np = np
        self.volume = 1.0
        self._device = None
        self._stream = None
        self._spectrum = self.design_spectrum([0.0] * len(EQ_BANDS))
        self._pending_spectrum = None
        self._tail = None

    # -- filter design ------------------------------------------------------
    def design_spectrum(self, gains):
        """Turn 10 band gains (dB) into the FFT spectrum of a linear-phase
        FIR filter, ready to multiply block spectra with."""
        np = self._np
        freqs = np.fft.rfftfreq(EQ_FFT, 1.0 / SAMPLE_RATE)
        # Interpolate the gains smoothly on a log-frequency axis (how
        # graphic EQs behave); below 31 Hz / above 16 kHz hold the end band.
        log_f = np.log10(np.maximum(freqs, 20.0))
        db = np.interp(log_f, np.log10(EQ_BANDS), gains)
        db -= max(0.0, max(gains))          # headroom: never boost above 0 dB
        magnitude = 10.0 ** (db / 20.0)
        # Zero-phase impulse response -> shift to make it causal (delay of
        # EQ_TAPS // 2 frames), cut to EQ_TAPS taps, and window it so the
        # cut doesn't ripple the response.
        impulse = np.fft.irfft(magnitude, EQ_FFT)
        impulse = np.roll(impulse, EQ_TAPS // 2)[:EQ_TAPS] * np.hanning(EQ_TAPS)
        return np.fft.rfft(impulse, EQ_FFT)[:, None]  # column: same for L and R

    def set_gains(self, gains):
        """Apply new band gains; the audio thread crossfades to them on
        its next block (so it never clicks)."""
        self._pending_spectrum = self.design_spectrum(clamp_gains(gains))

    # -- playback -----------------------------------------------------------
    def play(self, path, start_seconds=0.0):
        """Start playing `path` from `start_seconds`. Raises if miniaudio
        can't decode the file (the caller then falls back to pygame)."""
        self.stop()
        ma = self._ma
        self._stream = ma.stream_file(
            path, output_format=ma.SampleFormat.FLOAT32, nchannels=CHANNELS,
            sample_rate=SAMPLE_RATE, frames_to_read=EQ_BLOCK,
            seek_frame=max(0, int(start_seconds * SAMPLE_RATE)))
        self._tail = self._np.zeros((EQ_TAPS - 1, CHANNELS))
        self._device = ma.PlaybackDevice(
            output_format=ma.SampleFormat.FLOAT32, nchannels=CHANNELS,
            sample_rate=SAMPLE_RATE, buffersize_msec=DEVICE_BUFFER_MS,
            app_name="Playerton")
        callback = self._callback()
        next(callback)  # miniaudio wants an already-started generator
        self._device.start(callback)

    def stop(self):
        """Stop output immediately and release the device and decoder."""
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # -- audio thread ------------------------------------------------------
    def _next_block(self):
        """Decode and EQ the next EQ_BLOCK frames. Returns an (n, 2) array,
        or None once the file has ended."""
        np = self._np
        if self._stream is None:
            return None
        try:
            chunk = self._stream.send(EQ_BLOCK)
        except (StopIteration, Exception):
            self._stream = None
            return None
        samples = np.frombuffer(chunk, dtype=np.float32).reshape(-1, CHANNELS)
        if not len(samples):
            self._stream = None
            return None

        # Overlap-add convolution of this block with the EQ filter.
        spectrum = np.fft.rfft(samples, n=EQ_FFT, axis=0)
        out = np.fft.irfft(spectrum * self._spectrum, EQ_FFT, axis=0)
        pending = self._pending_spectrum
        if pending is not None:
            # Gains changed: filter with the new curve too and crossfade
            # old -> new across this block.
            self._pending_spectrum = None
            new = np.fft.irfft(spectrum * pending, EQ_FFT, axis=0)
            ramp = np.ones((EQ_FFT, 1))
            ramp[:EQ_BLOCK, 0] = np.linspace(0.0, 1.0, EQ_BLOCK)
            out = out * (1.0 - ramp) + new * ramp
            self._spectrum = pending
        out[:EQ_TAPS - 1] += self._tail
        self._tail = out[EQ_BLOCK:EQ_BLOCK + EQ_TAPS - 1].copy()
        return out[:len(samples)]

    def _callback(self):
        """Generator miniaudio pulls output from: it's sent how many frames
        the device needs and yields exactly that many (silence after the
        end of the file -- the app moves on to the next track itself)."""
        np = self._np
        queue = collections.deque()
        queued = 0
        required = yield b""
        while True:
            try:
                while queued < required:
                    block = self._next_block()
                    if block is None:
                        block = np.zeros((required - queued, CHANNELS))
                    queue.append(block)
                    queued += len(block)
                parts, need = [], required
                while need:
                    head = queue[0]
                    if len(head) <= need:
                        parts.append(queue.popleft())
                        need -= len(head)
                    else:
                        parts.append(head[:need])
                        queue[0] = head[need:]
                        need = 0
                queued -= required
                out = np.concatenate(parts) * self.volume
                out = np.clip(out, -1.0, 1.0).astype(np.float32)
            except Exception:
                out = np.zeros((required, CHANNELS), dtype=np.float32)
            required = yield out


class AudioOutput:
    """What the app uses to play audio. Same calls as pygame.mixer.music,
    plus set_eq(). Picks the backend per track: EqPlayer while the EQ is
    on (and the file type is supported), otherwise pygame. Tracks the
    playback position itself, so it can resume/switch backends at the
    right spot."""

    def __init__(self, pygame_ready):
        """`pygame_ready`: whether pygame.mixer.init() succeeded."""
        self.pygame_ready = pygame_ready
        self.path = None
        self.volume = 1.0
        self.eq_enabled = False
        self.eq_gains = [0.0] * len(EQ_BANDS)
        self._eq = None            # EqPlayer, created on first EQ use
        self._backend = None       # "eq" | "pygame" | None (nothing running)
        self._playing = False      # a track is started (playing or paused)
        self._paused = False
        self._start_pos = 0.0      # position (s) when output last (re)started
        self._started_at = 0.0     # time.monotonic() at that moment
        self.last_eq_error = None  # why the EQ engine couldn't be used, if so

    # -- position ------------------------------------------------------------
    def position(self):
        """Current playback position in seconds."""
        if not self._playing:
            return 0.0
        if self._paused:
            return self._start_pos
        return self._start_pos + (time.monotonic() - self._started_at)

    def eq_active(self):
        """True if the current track is going through the EQ engine."""
        return self._backend == "eq"

    # -- pygame.mixer.music-compatible API ------------------------------------
    def load(self, path):
        """Select the file to play (stops whatever is playing)."""
        self.stop()
        self.path = path

    def play(self, start=0.0):
        """Start the loaded file from `start` seconds."""
        self._start_backend(start)

    def pause(self):
        """Pause, remembering the position."""
        if not self._playing or self._paused:
            return
        self._start_pos = self.position()
        self._paused = True
        if self._backend == "eq":
            self._eq.stop()        # restarted from _start_pos on unpause
            self._backend = None
        elif self._backend == "pygame":
            try:
                pygame.mixer.music.pause()
            except Exception:
                pass

    def unpause(self):
        """Resume from where pause() left off."""
        if not self._playing or not self._paused:
            return
        if self._backend == "pygame" and not self._wants_eq():
            try:
                pygame.mixer.music.unpause()
            except Exception:
                pass
            self._paused = False
            self._started_at = time.monotonic()
        else:
            # EQ engine (or the EQ was toggled while paused): restart the
            # right backend at the paused position.
            self._start_backend(self._start_pos)

    def seek(self, seconds):
        """Jump to `seconds`, keeping the playing/paused state. While paused
        nothing is started -- the new position is used on unpause()."""
        if not self._playing:
            return
        if self._paused:
            self._stop_backend()
            self._start_pos = max(0.0, float(seconds))
        else:
            self._start_backend(seconds)

    def cue_paused(self, start=0.0):
        """Load-and-pause in one step: mark the loaded file as paused at
        `start` without making any sound (used to restore the last
        session's track). unpause() then starts it from there."""
        self._stop_backend()
        self._playing = True
        self._paused = True
        self._start_pos = max(0.0, float(start))

    def stop(self):
        """Stop playback completely."""
        self._stop_backend()
        self._playing = False
        self._paused = False

    def set_volume(self, volume):
        """Volume 0.0-1.0 for both backends."""
        self.volume = max(0.0, min(1.0, float(volume)))
        if self._eq is not None:
            self._eq.volume = self.volume
        if self.pygame_ready:
            try:
                pygame.mixer.music.set_volume(self.volume)
            except Exception:
                pass

    # -- equalizer -------------------------------------------------------------
    def set_eq(self, enabled, gains):
        """Turn the EQ on/off and/or change its gains. Gain changes apply
        live; turning it on/off mid-track switches backend at the current
        position (or on the next unpause, if paused)."""
        self.eq_gains = clamp_gains(gains)
        if self._eq is not None:
            self._eq.set_gains(self.eq_gains)
        enabled = bool(enabled)
        if enabled == self.eq_enabled:
            return
        self.eq_enabled = enabled
        if self._playing and not self._paused:
            self._start_backend(self.position())
        elif self._playing and self._paused and self._backend == "pygame":
            # Release pygame now; unpause() will start the right backend.
            self._stop_backend()

    # -- internals ---------------------------------------------------------------
    def _wants_eq(self):
        """Should the current file use the EQ engine?"""
        return (self.eq_enabled and self.path is not None
                and os.path.splitext(self.path)[1].lower() in EQ_EXTENSIONS)

    def _start_backend(self, start):
        """(Re)start output of the current file at `start` seconds on the
        appropriate backend."""
        self._stop_backend()
        if self.path is None:
            return
        start = max(0.0, float(start))
        backend = None
        if self._wants_eq():
            try:
                if self._eq is None:
                    self._eq = EqPlayer()
                    self._eq.set_gains(self.eq_gains)
                self._eq.volume = self.volume
                self._eq.play(self.path, start)
                backend = "eq"
                self.last_eq_error = None
            except Exception as exc:  # missing package, undecodable file, ...
                self.last_eq_error = exc
                logger.warning("EQ engine unavailable for %s, using pygame: %r",
                               os.path.basename(self.path), exc, exc_info=True)
        if backend is None and self.pygame_ready:
            pygame.mixer.music.load(self.path)
            pygame.mixer.music.set_volume(self.volume)
            pygame.mixer.music.play(start=start)
            backend = "pygame"
        self._backend = backend
        self._playing = True
        self._paused = False
        self._start_pos = start
        self._started_at = time.monotonic()

    def _stop_backend(self):
        """Silence whichever backend is running."""
        if self._backend == "eq" and self._eq is not None:
            self._eq.stop()
        elif self._backend == "pygame":
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
        self._backend = None
