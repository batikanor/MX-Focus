"""Focus scoring: turn per-window band powers into one number for the VR layer.

Consumes `BandPowers` from bandpower.py (4 channels x 4 bands, 4 per second)
and emits a single scalar the pen-wobble effect can multiply by.

Design decisions, all settled deliberately rather than defaulted into:

  - The score is **relative to the individual**, never to a fixed constant.
    A session opens with a calibration period; everything after is expressed
    against that person's own baseline. Raw band ratios are not comparable
    between people, or even between sessions for one person -- which is
    exactly why the original project's hardcoded `calmness < 0.45` could not
    have meant anything.
  - Calibration happens during the first 30 s of **real work**, not at rest.
    This is what fixes the meaning of z = 0: "your normal working state",
    so a drop below it is precisely the event worth catching. Calibrating at
    rest would put the whole exam at positive z and squeeze the interesting
    signal into the top of the range.
  - The baseline **freezes** once collected. An adaptive baseline slowly
    redefines whatever the student is currently doing as normal, so a
    ten-minute drift would be absorbed and stop being reported -- which
    defeats the point of the tool.
  - Frontal channels only (AF7, AF8). TP9/TP10 sit near the jaw, and jaw EMG
    lands in the beta band -- the very band that indicates concentration.
    Including them would let a clenched jaw read as focus.
  - Feature is beta / (alpha + theta), the Pope engagement index. Alpha in
    the denominator catches disengagement (eyes closed, mind wandering),
    a distinct failure mode from the drowsiness theta tracks.
  - Channels collapse by mean over the *unflagged* ones, so a single bad
    electrode costs precision rather than the whole window.
  - Normalisation is a robust z-score (median/MAD, matching the artifact
    detector in bandpower.py) so one blink surviving calibration cannot
    poison the session baseline.
  - Both numbers are emitted: the z-score, which keeps a real interpretation
    for logs and debugging, and a 0-1 squash of it, which is the bounded
    multiplier the VR layer wants.
  - Smoothing is applied to z, before the squash, so the exponential average
    happens in a linear domain rather than a compressed one.
  - A fully flagged window holds the last score briefly, then decays toward
    neutral. Holding alone would let a fallen-off electrode lock the pen at
    maximum wobble forever with no way for the student to recover.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from bandpower import BAND_NAMES, CHANNELS, HOP_SECONDS


SCORE_CHANNELS = ("AF7", "AF8")
_SCORE_CHANNEL_INDICES = tuple(CHANNELS.index(c) for c in SCORE_CHANNELS)

_BETA = BAND_NAMES.index("beta")
_ALPHA = BAND_NAMES.index("alpha")
_THETA = BAND_NAMES.index("theta")

CALIBRATION_SECONDS = 30.0

# Matches bandpower.ArtifactDetector: 1.4826 * MAD approximates the standard
# deviation for normally distributed data, so z reads in real sigma.
_MAD_TO_SIGMA = 1.4826

# Logistic scale. At 1.0, z = +-2 maps to about 0.12 and 0.88: wide enough
# that ordinary noise does not saturate, narrow enough that the effect
# actually reaches both ends.
SQUASH_SIGMA = 1.0

# Exponential smoothing time constant, in seconds.
SMOOTHING_TAU_SECONDS = 1.0

# How long a flagged stretch is held before it starts decaying to neutral.
ARTIFACT_HOLD_SECONDS = 2.0

# Neutral in z terms. Squashes to 0.5, i.e. no wobble bias either way.
NEUTRAL_Z = 0.0

# Floor on the baseline's dispersion, as a fraction of its own median.
#
# Because the baseline is frozen, a student who happened to be unusually
# steady during calibration would otherwise get a hair-trigger score for the
# whole session: a small MAD makes every later deviation enormous. Measured
# on a near-stationary signal, an unfloored baseline produced z = -27, which
# pins the wobble at maximum and destroys all remaining dynamic range.
#
# Expressed relative to the median so it stays unit-agnostic, like every
# other threshold in this pipeline. This is the continuous form of the
# zero-dispersion guard below.
MIN_BASELINE_DISPERSION = 0.05


@dataclass
class FocusScore:
    """One window's score.

    value       : 0-1 squashed multiplier for the VR layer, or None while
                  calibrating. None is deliberate: it forces the caller to
                  handle calibration rather than silently consuming a
                  meaningless number.
    z           : robust z against this person's baseline, unbounded, or None
    calibrating : True while the baseline is still being collected
    artifact    : carried through from BandPowers
    held        : True when this score is held or decaying rather than
                  measured, because the window was unusable
    t           : window end, on the source clock
    """

    value: float | None
    z: float | None
    calibrating: bool
    artifact: np.ndarray
    held: bool
    t: float


def window_feature(band_powers):
    """Reduce one BandPowers to a raw, uncalibrated engagement number.

    beta / (alpha + theta) on AF7 and AF8, averaged over whichever of the two
    is not flagged. Returns None when neither channel is usable.
    """
    values = []
    for ch in _SCORE_CHANNEL_INDICES:
        if band_powers.artifact[ch]:
            continue
        row = band_powers.relative[ch]
        denominator = row[_ALPHA] + row[_THETA]
        if denominator <= 0:
            continue
        values.append(row[_BETA] / denominator)

    if not values:
        return None
    return float(np.mean(values))


def squash(z):
    """Map an unbounded z to 0-1 for the VR layer."""
    return 1.0 / (1.0 + math.exp(-z / SQUASH_SIGMA))


class Baseline:
    """Accumulates the calibration period, then freezes and normalises."""

    def __init__(self, calibration_seconds=CALIBRATION_SECONDS,
                 hop_seconds=HOP_SECONDS):
        self._needed = int(round(calibration_seconds / hop_seconds))
        self._features: list[float] = []
        self._centre = None
        self._scale = None

    @property
    def ready(self) -> bool:
        return self._centre is not None

    def add(self, feature):
        """Fold one calibration window into the baseline, freezing when full.

        Only windows with a usable feature are counted, so a student who
        blinks a lot simply calibrates for longer rather than calibrating
        on corrupted data.
        """
        if self.ready:
            return

        self._features.append(feature)
        if len(self._features) >= self._needed:
            values = np.asarray(self._features, dtype=float)
            self._centre = float(np.median(values))
            mad = float(np.median(np.abs(values - self._centre)) * _MAD_TO_SIGMA)
            self._scale = max(
                mad, MIN_BASELINE_DISPERSION * abs(self._centre)
            )

    def z(self, feature):
        """Robust z of a raw feature against this person's frozen baseline."""
        if not self.ready:
            raise RuntimeError("z() called before calibration finished")
        # A zero-dispersion baseline means the calibration period carried no
        # variation at all -- almost certainly a dead channel. Report neutral
        # rather than dividing by zero and emitting infinities into the VR
        # layer, where they would become a permanently maximal wobble.
        if self._scale <= 0:
            return NEUTRAL_Z
        return (feature - self._centre) / self._scale


class FocusScorer:
    """Stateful: holds one session's calibration baseline and smoothing."""

    def __init__(self, calibration_seconds=CALIBRATION_SECONDS,
                 hop_seconds=HOP_SECONDS):
        self._baseline = Baseline(calibration_seconds, hop_seconds)
        self._hop = hop_seconds
        self._smoothed_z = None
        self._last_t = None
        self._last_good_t = None

    def feed(self, band_powers) -> FocusScore:
        """Consume one window, emit one score."""
        t = band_powers.t
        dt = self._hop if self._last_t is None else max(t - self._last_t, 0.0)
        self._last_t = t

        feature = window_feature(band_powers)

        if not self._baseline.ready:
            if feature is not None:
                self._baseline.add(feature)
            # Reported as calibrating even if that final sample completed the
            # baseline: this window was consumed by calibration, so it has no
            # score to give. The contract is exactly `calibrating is (value is
            # None)`, and scoring starts with the next window.
            return FocusScore(
                value=None,
                z=None,
                calibrating=True,
                artifact=band_powers.artifact,
                held=False,
                t=t,
            )

        if feature is not None:
            self._last_good_t = t
            target = self._baseline.z(feature)
            held = False
        else:
            # Unusable window. Hold briefly -- blinks are short -- then decay
            # toward neutral so a failed electrode fades out instead of
            # freezing the pen at whatever it happened to be showing.
            since_good = t - (self._last_good_t if self._last_good_t is not None else t)
            if since_good <= ARTIFACT_HOLD_SECONDS:
                return self._emit(self._smoothed_z, band_powers, held=True, t=t)
            target = NEUTRAL_Z
            held = True

        if self._smoothed_z is None:
            self._smoothed_z = target
        else:
            alpha = 1.0 - math.exp(-dt / SMOOTHING_TAU_SECONDS)
            self._smoothed_z += alpha * (target - self._smoothed_z)

        return self._emit(self._smoothed_z, band_powers, held=held, t=t)

    def _emit(self, z, band_powers, held, t) -> FocusScore:
        z = NEUTRAL_Z if z is None else z
        return FocusScore(
            value=squash(z),
            z=z,
            calibrating=False,
            artifact=band_powers.artifact,
            held=held,
            t=t,
        )


def score_stream(band_powers_stream, calibration_seconds=CALIBRATION_SECONDS):
    """Yield FocusScore per BandPowers, so this composes with extract_stream.

        scores = score_stream(extract_stream(source, fs))
    """
    scorer = FocusScorer(calibration_seconds)
    for band_powers in band_powers_stream:
        yield scorer.feed(band_powers)
