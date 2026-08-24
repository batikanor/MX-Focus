"""Tests for bandpower.

The point of these is that they need no hardware. Every input is synthetic,
so a failure means the DSP is wrong rather than that someone's headband
slipped.
"""

import numpy as np
import pytest

import bandpower as bp


FS = 256


def sine(freq, seconds=4.0, fs=FS, amplitude=1.0, n_channels=4):
    """A clean sine on every channel, plus its timestamps."""
    n = int(seconds * fs)
    t = np.arange(n) / fs
    wave = amplitude * np.sin(2 * np.pi * freq * t)
    samples = np.tile(wave[:, None], (1, n_channels))
    return samples, t


def first_window(samples, timestamps, fs=FS):
    results = list(bp.extract_stream([(samples, timestamps)], fs))
    assert results, "no windows produced"
    return results[0]


# --- the core claim: power lands in the right band --------------------------

@pytest.mark.parametrize(
    "freq, expected_band",
    [
        (6.0, "theta"),
        (10.0, "alpha"),
        (20.0, "beta"),
    ],
)
def test_pure_tone_lands_in_its_band(freq, expected_band):
    result = first_window(*sine(freq))
    j = bp.BAND_NAMES.index(expected_band)

    for ch in range(len(bp.CHANNELS)):
        share = result.relative[ch, j]
        assert share > 0.9, (
            f"{freq} Hz put only {share:.1%} of channel {ch}'s power in "
            f"{expected_band}; full row {dict(zip(bp.BAND_NAMES, result.relative[ch]))}"
        )


def test_out_of_band_tone_is_suppressed():
    """50 Hz mains sits above the 35 Hz lowpass and must not reach any band."""
    clean = first_window(*sine(10.0))
    samples, t = sine(10.0)
    contaminated, _ = sine(50.0, amplitude=5.0)

    result = first_window(samples + contaminated, t)
    np.testing.assert_allclose(result.relative, clean.relative, atol=0.01)


# --- output shape and invariants --------------------------------------------

def test_shapes():
    result = first_window(*sine(10.0))
    assert result.absolute.shape == (len(bp.CHANNELS), len(bp.BANDS))
    assert result.relative.shape == (len(bp.CHANNELS), len(bp.BANDS))
    assert result.artifact.shape == (len(bp.CHANNELS),)
    assert isinstance(result.gap, bool)


def test_relative_rows_sum_to_one():
    result = first_window(*sine(10.0))
    np.testing.assert_allclose(result.relative.sum(axis=1), 1.0, rtol=1e-9)


def test_silent_channel_gives_zeros_not_nan():
    samples, t = sine(10.0)
    samples[:, 2] = 0.0
    result = first_window(samples, t)

    assert not np.isnan(result.relative).any()
    np.testing.assert_array_equal(result.relative[2], np.zeros(len(bp.BANDS)))


def test_timestamp_is_window_end():
    samples, t = sine(10.0)
    result = first_window(samples, t)
    expected_end = t[int(bp.WINDOW_SECONDS * FS) - 1]
    assert result.t == pytest.approx(expected_end)


# --- windowing --------------------------------------------------------------

def test_window_count_and_hop():
    seconds = 10.0
    samples, t = sine(10.0, seconds=seconds)
    results = list(bp.extract_stream([(samples, t)], FS))

    size = int(bp.WINDOW_SECONDS * FS)
    hop = int(bp.HOP_SECONDS * FS)
    assert len(results) == (len(samples) - size) // hop + 1

    # consecutive windows are one hop apart
    gaps = np.diff([r.t for r in results])
    np.testing.assert_allclose(gaps, bp.HOP_SECONDS, atol=1e-9)


def test_chunked_input_matches_single_push():
    """Feeding the same data in pieces must not change the result."""
    samples, t = sine(10.0, seconds=6.0)
    whole = list(bp.extract_stream([(samples, t)], FS))

    chunks = [
        (samples[i : i + 37], t[i : i + 37]) for i in range(0, len(samples), 37)
    ]
    pieces = list(bp.extract_stream(chunks, FS))

    assert len(whole) == len(pieces)
    for a, b in zip(whole, pieces):
        np.testing.assert_allclose(a.absolute, b.absolute, rtol=1e-9)


def test_take_before_ready_raises():
    buffer = bp.RollingWindow(FS)
    with pytest.raises(RuntimeError):
        buffer.take()


# --- artifacts --------------------------------------------------------------

def test_everything_flagged_before_baseline_fills():
    detector = bp.ArtifactDetector(FS)
    assert not detector.ready
    assert detector.flags(np.zeros((512, 4))).all()


def test_calm_signal_is_not_flagged_once_calibrated():
    rng = np.random.default_rng(0)
    detector = bp.ArtifactDetector(FS)
    detector.update(rng.normal(size=(FS * 10, 4)))

    assert detector.ready
    assert not detector.flags(rng.normal(size=(512, 4))).any()


def test_spike_is_flagged():
    rng = np.random.default_rng(0)
    detector = bp.ArtifactDetector(FS)
    detector.update(rng.normal(size=(FS * 10, 4)))

    window = rng.normal(size=(512, 4))
    window[100, 1] = 500.0  # a blink-sized excursion on one channel

    flags = detector.flags(window)
    assert flags[1]
    assert not flags[[0, 2, 3]].any(), "spike on one channel flagged others"


def test_threshold_is_unit_agnostic():
    """The whole point of MAD: scaling the input must not change the flags."""
    rng = np.random.default_rng(0)
    baseline = rng.normal(size=(FS * 10, 4))
    window = rng.normal(size=(512, 4))
    window[100, 1] = 500.0

    small = bp.ArtifactDetector(FS)
    small.update(baseline)

    large = bp.ArtifactDetector(FS)
    large.update(baseline * 1000.0)

    np.testing.assert_array_equal(
        small.flags(window), large.flags(window * 1000.0)
    )


def test_baseline_is_not_double_counted():
    """Regression guard.

    Windows overlap by 87.5%, so feeding the detector emitted windows rather
    than arriving samples entered every sample about eight times. The
    baseline then claimed 30 s while holding ~5.5 s of real signal, and the
    5 s warm-up expired after ~0.6 s of unique data. Observable symptom: the
    detector went live far too early, so that is what this pins down.
    """
    rng = np.random.default_rng(0)
    n = FS * 20
    samples = rng.normal(size=(n, 4))
    t = np.arange(n) / FS

    # chunked the way an LSL inlet delivers, not one big push
    chunks = [(samples[i : i + 12], t[i : i + 12]) for i in range(0, n, 12)]
    results = list(bp.extract_stream(chunks, FS))

    first_clean = next(r for r in results if not r.artifact.any())

    # The detector must go live when the baseline genuinely holds
    # ARTIFACT_MIN_BASELINE_SECONDS of signal -- within one hop, since that
    # is the resolution at which windows are emitted. Under the old
    # double-counting this fired at ~2.75 s.
    assert abs(first_clean.t - bp.ARTIFACT_MIN_BASELINE_SECONDS) <= bp.HOP_SECONDS, (
        f"detector went live at {first_clean.t:.3f}s, expected "
        f"~{bp.ARTIFACT_MIN_BASELINE_SECONDS}s"
    )

    settled = [r for r in results if r.t > 8.0]
    assert not any(r.artifact.any() for r in settled), (
        "calm signal flagged after the baseline filled"
    )


def test_flatlined_channel_is_flagged():
    detector = bp.ArtifactDetector(FS)
    baseline = np.random.default_rng(0).normal(size=(FS * 10, 4))
    baseline[:, 3] = 0.0
    detector.update(baseline)

    assert detector.flags(np.zeros((512, 4)))[3]


# --- gaps -------------------------------------------------------------------

def test_contiguous_data_is_not_a_gap():
    assert not first_window(*sine(10.0)).gap


def test_dropped_samples_are_flagged_as_gap():
    samples, t = sine(10.0)
    t = t.copy()
    t[300:] += 5.0  # five seconds of missing data mid-window

    assert first_window(samples, t).gap


# --- batch path -------------------------------------------------------------

def test_recording_matches_stream(tmp_path):
    import pandas as pd

    samples, t = sine(10.0, seconds=6.0)
    frame = pd.DataFrame(samples, columns=[f"EEG_{i}" for i in range(4)])
    frame["timestamps"] = t
    path = tmp_path / "eeg.csv"
    frame.to_csv(path, index=False)

    from_file = bp.extract_from_recording(path, FS)
    from_stream = list(bp.extract_stream([(samples, t)], FS))

    assert len(from_file) == len(from_stream)
    for a, b in zip(from_file, from_stream):
        # atol is not cosmetic: the CSV round-trips floats through text, so
        # bands that are essentially zero differ in the last bits. Bands
        # carrying real power match to within rtol.
        np.testing.assert_allclose(a.absolute, b.absolute, rtol=1e-6, atol=1e-18)


def test_recording_rejects_too_few_channels(tmp_path):
    import pandas as pd

    frame = pd.DataFrame(np.zeros((100, 2)), columns=["EEG_0", "EEG_1"])
    frame["timestamps"] = np.arange(100) / FS
    path = tmp_path / "short.csv"
    frame.to_csv(path, index=False)

    with pytest.raises(ValueError, match="need at least"):
        bp.extract_from_recording(path, FS)
