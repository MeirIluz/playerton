"""Music-spectrum analysis for the "Visualizer" popup window.

Pygame's `mixer.music` streams audio straight from disk for playback and
doesn't expose a live "what's currently coming out of the speakers" tap,
so a real-time FFT-of-the-output approach isn't available here. Instead,
the WHOLE track is decoded and analyzed into a short-time frequency
spectrum ONCE, up front, in a background thread (see
App._visualizer_analysis_worker) -- a list of frames, each a set of bar
heights, sampled at a fixed rate (`fps`). The popup then simply looks up
whichever frame corresponds to the track's CURRENT playback position
(using the same elapsed-time tracking already driving the seek bar) and
draws it. This keeps the visualizer perfectly in sync with actual
playback position/seeking/pausing, using a genuine analysis of the
track's real audio content -- not a fake/randomized animation -- without
needing a live audio tap.
"""

import numpy as np
import pygame


def analyze_track_spectrum(path, num_bars=32, fps=20):
    """Decode `path` (any format pygame.mixer.Sound can load -- the same
    set this app already plays) and return (frames, fps): `frames` is a
    list of per-frame bar-height lists, each containing `num_bars`
    floats in 0..1 (log-frequency-bucketed, normalized to the track's
    own peak, sqrt-scaled for a more visually even look), one frame
    every `1/fps` seconds of the track. Raises on failure (unsupported
    format, decode error, ...) -- callers should catch and report that
    themselves."""
    sound = pygame.mixer.Sound(path)
    samples = pygame.sndarray.samples(sound)
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    samples = samples.astype(np.float32)

    init = pygame.mixer.get_init()
    sample_rate = abs(init[0]) if init else 44100
    sample_rate = sample_rate or 44100

    frame_size = max(256, sample_rate // fps)
    total_samples = len(samples)
    num_frames = max(1, total_samples // frame_size)

    window = np.hanning(frame_size)
    freqs = np.fft.rfftfreq(frame_size, d=1.0 / sample_rate)
    # Log-spaced bucket edges (roughly 40Hz-Nyquist), like a typical
    # bar-style music visualizer -- real music's energy falls off with
    # frequency, so linear bucketing would cram almost all visible
    # movement into the first couple of bars.
    low, high = 40.0, sample_rate / 2
    edges = np.logspace(np.log10(low), np.log10(high), num_bars + 1)
    bucket_masks = [
        (freqs >= edges[b]) & (freqs < edges[b + 1]) for b in range(num_bars)]

    raw_frames = []
    peak = 1e-6
    for i in range(num_frames):
        start = i * frame_size
        chunk = samples[start:start + frame_size]
        if len(chunk) < frame_size:
            chunk = np.pad(chunk, (0, frame_size - len(chunk)))
        spectrum = np.abs(np.fft.rfft(chunk * window))
        bars = np.zeros(num_bars, dtype=np.float32)
        for b, mask in enumerate(bucket_masks):
            if mask.any():
                bars[b] = spectrum[mask].mean()
        raw_frames.append(bars)
        local_peak = bars.max()
        if local_peak > peak:
            peak = local_peak

    frames = [
        np.sqrt(np.clip(bars / peak, 0.0, 1.0)).tolist() for bars in raw_frames]
    return frames, fps
