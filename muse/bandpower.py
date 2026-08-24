"""Band-power extraction for Muse EEG.

Design decisions, all settled deliberately rather than defaulted into:

  - Pure core with a batch and a streaming wrapper. The caller supplies the
    sample source, so this module never imports pylsl and can be driven by a
    plain list in a test -- no headband required to verify it.
  - Per-channel output, no pooling. Pooling is lossy and irreversible; it is
    a modelling choice that belongs downstream where it can be evaluated.
  - Absolute and relative power both emitted. Relative cancels most
    contact-quality variation; absolute is what reveals a dead electrode.
  - 2 s windows, 0.25 s hop -> 4 updates/sec.
  - delta/theta/alpha/beta. Gamma is dropped: above ~30 Hz a dry-electrode
    headset mostly measures jaw and forehead muscle, not brain.
  - Welch with 1 s sub-segments at 50% overlap -> 1 Hz bins, 3 segments
    averaged. The 2 s window therefore buys averaging, not resolution; that
    trade was made knowingly, because a jittery estimate would make the pen
    wobble on its own.
  - 4th-order Butterworth 1-35 Hz, applied CAUSALLY and STATEFULLY across the
    whole stream rather than zero-phase per window. Zero-phase filtering
    (filtfilt) restarts at every window boundary, and the resulting edge
    transients are low-frequency, so they leak into delta -- measured at 6%
    of total power with strong 50 Hz interference. Since only power spectra
    are computed here, phase is never used, so the property filtfilt exists
    to provide was being paid for and discarded. A filter that never
    restarts has no edges to ring at. No explicit detrend is needed either:
    the 1 Hz highpass removes DC and drift continuously.
  - No mains notch: 50/60 Hz sits above beta's 30 Hz ceiling and never
    enters a computed band.
  - Artifacts and gaps are flagged per channel, never dropped and never
    zero-filled. Dropping punches holes in a realtime loop; zero-filling
    creates a step discontinuity that smears power across every band.
  - The artifact threshold is adaptive (MAD-based), so it is unit-agnostic
    and self-calibrating per person and per session.
  - Each window is stamped with its END, on the source clock: that is the
    moment the score becomes actionable. Start and centre are one
    subtraction away, since the window length is fixed.

Note on session start: the filter needs roughly a second to settle, so the
earliest windows carry a startup transient. The 5 s artifact warm-up already
covers this -- everything before the baseline fills is flagged anyway.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi, welch


BANDS = {
    "delta": (1.0, 4.0),    # artifact indicator, not attention
    "theta": (4.0, 8.0),
    "alpha": (8.0, 12.0),
    "beta": (13.0, 30.0),
}

BAND_NAMES = tuple(BANDS)

# Muse 2 scalp electrodes, in the order MuseLSL streams them. The device's
# fifth channel is an auxiliary input carrying floating-input noise unless
# something is physically plugged into it, so it is excluded.
CHANNELS = ("TP9", "AF7", "AF8", "TP10")

WINDOW_SECONDS = 2.0
HOP_SECONDS = 0.25

WELCH_NPERSEG = 256         # 1 s at 256 Hz
WELCH_NOVERLAP = 128        # 50%

FILTER_ORDER = 4
HIGHPASS_HZ = 1.0
LOWPASS_HZ = 35.0

# Scaled so the multiplier reads in sigma-equivalents: for normally
# distributed data, 1.4826 * MAD approximates the standard deviation.
_MAD_TO_SIGMA = 1.4826

ARTIFACT_MAD_MULTIPLIER = 5.0
ARTIFACT_BASELINE_SECONDS = 30.0
ARTIFACT_MIN_BASELINE_SECONDS = 5.0

# A window is flagged as a gap when its samples span more than this multiple
# of the time they should span at the nominal rate.
GAP_TOLERANCE = 1.5


@dataclass
class BandPowers:
    """One window's result.

    absolute : (n_channels, n_bands) float, band power in input units squared
    relative : (n_channels, n_bands) float, each row sums to 1
    artifact : (n_channels,) bool
    gap      : bool, the window was short on samples or spanned too long
    t        : float, window end on the source clock
    """

    absolute: np.ndarray
    relative: np.ndarray
    artifact: np.ndarray
    gap: bool
    t: float


# --- filtering --------------------------------------------------------------

class CausalBandpass:
    """Stateful 1-35 Hz Butterworth, carried across the whole stream.

    Stateful on purpose. Filtering each window independently would restart
    the filter at every boundary and ring; carrying state means the filter
    sees one continuous signal and never has an edge to ring at. A
    consequence worth knowing: results depend on the filter's history, so a
    given window is only reproducible by replaying the stream that preceded
    it. That is what makes the batch and streaming paths agree.
    """

    def __init__(self, fs):
        self._sos = butter(
            FILTER_ORDER,
            (HIGHPASS_HZ, LOWPASS_HZ),
            btype="band",
            fs=fs,
            output="sos",
        )
        self._zi = None

    def process(self, chunk):
        chunk = np.asarray(chunk, dtype=float)
        if chunk.shape[0] == 0:
            return chunk

        if self._zi is None:
            # Seed the filter at the steady state for the first sample rather
            # than at zero, which keeps the startup step small.
            self._zi = sosfilt_zi(self._sos)[:, :, None] * chunk[0][None, None, :]

        filtered, self._zi = sosfilt(self._sos, chunk, axis=0, zi=self._zi)
        return filtered


# --- core -------------------------------------------------------------------

def compute_band_powers(window, fs):
    """Per-channel Welch band power for one *already filtered* window.

    Returns (absolute, relative), each (n_channels, n_bands).

    Note that `relative` is normalised against the sum of the four bands, not
    against total signal power. The conventional edges leave 12-13 Hz
    unassigned, so that sliver is excluded from both numerator and
    denominator. That is a consequence of the band edges, not an error.
    """
    window = np.asarray(window, dtype=float)
    nperseg = min(WELCH_NPERSEG, window.shape[0])
    noverlap = min(WELCH_NOVERLAP, nperseg // 2)

    freqs, psd = welch(window, fs=fs, nperseg=nperseg, noverlap=noverlap, axis=0)
    psd = psd.T    # (n_freqs, n_channels) -> channels lead

    absolute = np.empty((psd.shape[0], len(BANDS)), dtype=float)
    for j, (lo, hi) in enumerate(BANDS.values()):
        mask = (freqs >= lo) & (freqs < hi)
        absolute[:, j] = np.trapezoid(psd[:, mask], freqs[mask], axis=1)

    total = absolute.sum(axis=1, keepdims=True)
    # A silent channel would divide by zero; emit zeros rather than NaN so a
    # dead electrode stays distinguishable from a corrupt computation.
    relative = np.divide(
        absolute, total, out=np.zeros_like(absolute), where=total > 0
    )
    return absolute, relative


class ArtifactDetector:
    """Per-channel adaptive amplitude threshold from a running MAD baseline.

    Deliberately unit-agnostic: because the threshold is derived from the
    signal's own dispersion, it behaves identically whether MuseLSL emits
    microvolts or raw ADC counts, and it recalibrates per person and session.
    """

    def __init__(self, fs, n_channels=len(CHANNELS)):
        self._min_samples = int(ARTIFACT_MIN_BASELINE_SECONDS * fs)
        self._n_channels = n_channels
        self._baseline: deque = deque(maxlen=int(ARTIFACT_BASELINE_SECONDS * fs))

    @property
    def ready(self) -> bool:
        return len(self._baseline) >= self._min_samples

    def update(self, window):
        """Fold a raw window into the baseline."""
        self._baseline.extend(np.asarray(window, dtype=float))

    def flags(self, window):
        """Per-channel bool: does this window exceed the adaptive threshold?

        Before the baseline has filled, everything is flagged. An
        uncalibrated detector confidently reporting "clean" is worse than one
        reporting nothing, so this fails safe.
        """
        if not self.ready:
            return np.ones(self._n_channels, dtype=bool)

        baseline = np.asarray(self._baseline, dtype=float)
        centre = np.median(baseline, axis=0)
        mad = np.median(np.abs(baseline - centre), axis=0) * _MAD_TO_SIGMA
        threshold = ARTIFACT_MAD_MULTIPLIER * mad

        deviation = np.abs(np.asarray(window, dtype=float) - centre).max(axis=0)
        # A channel with zero dispersion is flat-lined, which is itself a
        # fault; flag it rather than dividing by zero.
        return np.where(mad > 0, deviation > threshold, True)


def _is_gap(timestamps, fs) -> bool:
    """A window whose samples span far longer than nominal has dropped data."""
    timestamps = np.asarray(timestamps, dtype=float)
    if timestamps.size < 2:
        return True
    expected = (timestamps.size - 1) / fs
    return bool((timestamps[-1] - timestamps[0]) > expected * GAP_TOLERANCE)


def _process_window(raw, filtered, timestamps, fs, detector) -> BandPowers:
    """Combine one window's raw and filtered forms into a result.

    Artifacts are detected on the *raw* window rather than the filtered one,
    because the 1 Hz highpass strips much of the low-frequency energy that
    makes a blink recognisable in the first place.

    This reads the detector but never updates it. The baseline is fed from
    the incoming stream instead -- see extract_stream.
    """
    raw = np.asarray(raw, dtype=float)
    artifact = detector.flags(raw)

    absolute, relative = compute_band_powers(filtered, fs)
    return BandPowers(
        absolute=absolute,
        relative=relative,
        artifact=artifact,
        gap=_is_gap(timestamps, fs),
        t=float(np.asarray(timestamps, dtype=float)[-1]),
    )


# --- windowing --------------------------------------------------------------

class RollingWindow:
    """Accumulates samples and emits fixed-length overlapping windows."""

    def __init__(self, fs):
        self._size = int(round(WINDOW_SECONDS * fs))
        self._hop = int(round(HOP_SECONDS * fs))
        self._samples: deque = deque()
        self._timestamps: deque = deque()

    def push(self, samples, timestamps):
        self._samples.extend(np.asarray(samples, dtype=float))
        self._timestamps.extend(np.asarray(timestamps, dtype=float))

    def ready(self) -> bool:
        return len(self._samples) >= self._size

    def take(self):
        """Return (window, timestamps) and advance by one hop."""
        if not self.ready():
            raise RuntimeError("take() called before a full window accumulated")

        window = np.array([self._samples[i] for i in range(self._size)])
        stamps = np.array([self._timestamps[i] for i in range(self._size)])

        for _ in range(min(self._hop, len(self._samples))):
            self._samples.popleft()
            self._timestamps.popleft()

        return window, stamps


# --- wrappers ---------------------------------------------------------------

def extract_stream(source, fs):
    """Yield BandPowers as windows fill.

    source : iterable of (samples, timestamps), where samples is
             (n, n_channels). An LSL inlet drained by the caller, a replayed
             recording, or a list in a test -- this module does not care.

    Raw and filtered forms are buffered in lockstep: the artifact detector
    needs the unfiltered signal, the spectrum needs the filtered one, and
    both must describe the same window.

    The artifact baseline is fed from the arriving chunks, not from the
    emitted windows. Windows overlap by 87.5%, so feeding it windows would
    enter every sample roughly eight times over -- a "30 second" baseline
    would hold about 5 seconds of real signal. Feeding the stream makes that
    double-counting structurally impossible rather than merely avoided.
    """
    bandpass = CausalBandpass(fs)
    raw_buffer = RollingWindow(fs)
    filtered_buffer = RollingWindow(fs)
    detector = ArtifactDetector(fs)

    for samples, timestamps in source:
        samples = np.asarray(samples, dtype=float)
        if samples.shape[0] == 0:
            continue

        raw_buffer.push(samples, timestamps)
        filtered_buffer.push(bandpass.process(samples), timestamps)
        detector.update(samples)

        while raw_buffer.ready():
            raw_window, stamps = raw_buffer.take()
            filtered_window, _ = filtered_buffer.take()
            yield _process_window(raw_window, filtered_window, stamps, fs, detector)


def extract_from_recording(path, fs):
    """Windowed band powers over a CSV written by receive_eeg.py.

    Expects columns EEG_0..EEG_n plus `timestamps`. Only the first four EEG
    columns are read; on a Muse those are TP9, AF7, AF8, TP10.

    Returns a list of BandPowers -- the same type extract_stream yields, so
    downstream code cannot tell which produced it.
    """
    import pandas as pd

    frame = pd.read_csv(path)
    eeg_columns = [c for c in frame.columns if c.startswith("EEG_")]
    if len(eeg_columns) < len(CHANNELS):
        raise ValueError(
            f"{path} has {len(eeg_columns)} EEG columns, "
            f"need at least {len(CHANNELS)}"
        )

    samples = frame[eeg_columns[: len(CHANNELS)]].to_numpy(dtype=float)
    timestamps = frame["timestamps"].to_numpy(dtype=float)
    return list(extract_stream([(samples, timestamps)], fs))
