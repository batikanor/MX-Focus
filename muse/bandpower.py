"""Band-power extraction for Muse EEG.

BACKBONE ONLY -- no function below is implemented. Constants that appear
here are settled decisions, not defaults. Remaining `DECISION:` markers are
still open. Placeholders (including this module's filename) are not proposals.

Settled:
  - pure core, with a batch and a streaming wrapper around it
  - caller supplies the sample source; this module never imports pylsl, so it
    can be driven by a plain list in a test
  - per-channel output, no pooling here; four scalp channels, aux excluded
  - absolute and relative power both emitted
  - 2 s windows, 0.25 s hop -> 4 updates/sec
  - delta/theta/alpha/beta; gamma dropped (EMG-dominated on dry electrodes)
  - Welch, 1 s sub-segments at 50% overlap -> 1 Hz bins, 3 segments averaged
    (the 2 s window buys averaging, not resolution -- accepted trade)
  - 4th-order Butterworth, zero-phase; no mains notch, since 50/60 Hz sits
    above beta's 30 Hz ceiling and never enters a computed band
  - artifacts and gaps are flagged per channel, never dropped; zero-filling a
    gap is specifically rejected (a step discontinuity smears power across
    every band). The scorer downstream decides what to do with a flag.
  - artifact threshold is adaptive (MAD-based), so it is unit-agnostic and
    self-calibrating -- no recorded session needed to make this module work
  - each window is stamped with its END, on the LSL clock
"""

from __future__ import annotations

from dataclasses import dataclass


BANDS = {
    "delta": (1.0, 4.0),    # artifact indicator, not attention
    "theta": (4.0, 8.0),
    "alpha": (8.0, 12.0),
    "beta": (13.0, 30.0),
}

CHANNELS = ("TP9", "AF7", "AF8", "TP10")

WINDOW_SECONDS = 2.0
HOP_SECONDS = 0.25

WELCH_NPERSEG = 256         # 1 s at 256 Hz
WELCH_NOVERLAP = 128        # 50%

FILTER_ORDER = 4
HIGHPASS_HZ = 1.0
LOWPASS_HZ = 35.0

# DECISION Q: how many MADs above baseline counts as an artifact.
ARTIFACT_MAD_MULTIPLIER = None

# DECISION R: how much history the running MAD baseline is computed over.
ARTIFACT_BASELINE_SECONDS = None


@dataclass
class BandPowers:
    """One window's result.

    absolute : shape (n_channels, n_bands)
    relative : shape (n_channels, n_bands), rows sum to 1
    artifact : shape (n_channels,), bool
    gap      : bool -- window was short on samples
    t        : window end, LSL clock

    DECISION C: whether this stays a dataclass, and the concrete array types.
    """

    absolute: object
    relative: object
    artifact: object
    gap: bool
    t: float


# --- core -------------------------------------------------------------------

def preprocess(window, fs):
    """Detrend, then 1-35 Hz 4th-order Butterworth, zero-phase.

    window : array, shape (n_samples, n_channels)
    """
    raise NotImplementedError


class ArtifactDetector:
    """Per-channel MAD baseline. Stateful, because the baseline is running."""

    def update(self, window):
        """Fold this window into the baseline."""
        raise NotImplementedError

    def flags(self, window):
        """Per-channel bool: does this window exceed the adaptive threshold?

        DECISION S: behaviour before the baseline has filled.
        """
        raise NotImplementedError


def compute_band_powers(window, fs, t) -> BandPowers:
    """Per-channel Welch band power for one window. Shared by both wrappers."""
    raise NotImplementedError


# --- batch wrapper ----------------------------------------------------------

def extract_from_recording(path, fs):
    """Windowed band powers over a CSV written by receive_eeg.py
    (columns EEG_0..EEG_n plus `timestamps`).

    DECISION H: return type -- list of BandPowers, or a DataFrame.
    """
    raise NotImplementedError


# --- streaming wrapper ------------------------------------------------------

class RollingWindow:
    """Accumulates samples and emits 2 s windows every 0.25 s."""

    def push(self, samples, timestamps):
        raise NotImplementedError

    def ready(self) -> bool:
        raise NotImplementedError

    def take(self):
        raise NotImplementedError


def extract_stream(source):
    """Yield BandPowers as windows fill.

    source : any iterable of (samples, timestamps) -- an LSL inlet drained by
             the caller, a replayed recording, or a list in a test.
    """
    raise NotImplementedError
