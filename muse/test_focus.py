"""Tests for focus.

Like the bandpower tests, everything here is synthetic. A failure means the
scoring logic is wrong, not that someone's headband slipped.
"""

import numpy as np
import pytest

import bandpower as bp
import focus


HOP = bp.HOP_SECONDS
CALIBRATION_WINDOWS = int(round(focus.CALIBRATION_SECONDS / HOP))


def make(feature, t, artifact=(False, False, False, False)):
    """A BandPowers whose engagement ratio is exactly `feature`.

    beta / (alpha + theta) with alpha = theta = 0.2 reduces to beta / 0.4.
    """
    relative = np.zeros((len(bp.CHANNELS), len(bp.BANDS)))
    relative[:, bp.BAND_NAMES.index("theta")] = 0.2
    relative[:, bp.BAND_NAMES.index("alpha")] = 0.2
    relative[:, bp.BAND_NAMES.index("beta")] = feature * 0.4
    relative[:, bp.BAND_NAMES.index("delta")] = 0.1

    return bp.BandPowers(
        absolute=relative.copy(),
        relative=relative,
        artifact=np.array(artifact, dtype=bool),
        gap=False,
        t=t,
    )


def calibrate(scorer, features=None, start=0.0):
    """Run a scorer through calibration. Returns the next timestamp."""
    rng = np.random.default_rng(0)
    if features is None:
        features = 1.0 + rng.normal(scale=0.1, size=CALIBRATION_WINDOWS)

    t = start
    for f in features:
        scorer.feed(make(f, t))
        t += HOP
    return t


# --- the feature ------------------------------------------------------------

def test_feature_is_beta_over_alpha_plus_theta():
    assert focus.window_feature(make(1.5, 0.0)) == pytest.approx(1.5)


def test_only_frontal_channels_are_used():
    """TP9/TP10 sit near the jaw; beta there is muscle, not concentration."""
    clean = make(1.0, 0.0)
    contaminated = make(1.0, 0.0)
    for name in ("TP9", "TP10"):
        ch = bp.CHANNELS.index(name)
        contaminated.relative[ch, bp.BAND_NAMES.index("beta")] = 99.0

    assert focus.window_feature(contaminated) == focus.window_feature(clean)


def test_flagged_channel_falls_back_to_the_other():
    both = make(1.0, 0.0)
    af7 = bp.CHANNELS.index("AF7")
    both.relative[af7, bp.BAND_NAMES.index("beta")] = 99.0

    # with AF7 flagged, only AF8 counts, so the outlier must not show
    flags = [False, False, False, False]
    flags[af7] = True
    both.artifact = np.array(flags, dtype=bool)

    assert focus.window_feature(both) == pytest.approx(1.0)


def test_both_frontal_flagged_gives_none():
    flags = [False, False, False, False]
    for name in ("AF7", "AF8"):
        flags[bp.CHANNELS.index(name)] = True

    assert focus.window_feature(make(1.0, 0.0, artifact=flags)) is None


# --- calibration ------------------------------------------------------------

def test_no_score_while_calibrating():
    scorer = focus.FocusScorer()
    t = 0.0
    for _ in range(CALIBRATION_WINDOWS):
        result = scorer.feed(make(1.0, t))
        assert result.calibrating
        assert result.value is None and result.z is None
        t += HOP

    after = scorer.feed(make(1.0, t))
    assert not after.calibrating
    assert after.value is not None


def test_unusable_windows_do_not_count_toward_calibration():
    """Blinking during calibration should lengthen it, not corrupt it."""
    flags = [True, True, True, True]
    scorer = focus.FocusScorer()

    t = 0.0
    for _ in range(CALIBRATION_WINDOWS):
        assert scorer.feed(make(1.0, t, artifact=flags)).calibrating
        t += HOP

    # none of those counted, so the scorer is still calibrating
    assert scorer.feed(make(1.0, t)).calibrating


def test_baseline_freezes_after_calibration():
    """A sustained drift must stay visible instead of becoming the new normal."""
    scorer = focus.FocusScorer()
    t = calibrate(scorer)

    # feed a persistently low feature for a long time
    low = []
    for _ in range(400):
        low.append(scorer.feed(make(0.5, t)).z)
        t += HOP

    # an adaptive baseline would drag z back toward 0; a frozen one does not
    assert low[-1] < -1.0, f"z drifted back to {low[-1]:.2f}; baseline is adapting"


# --- scoring ----------------------------------------------------------------

def test_baseline_level_scores_neutral():
    scorer = focus.FocusScorer()
    t = calibrate(scorer, features=[1.0] * CALIBRATION_WINDOWS)

    # zero-dispersion baseline is the degenerate case; use a varied one
    scorer = focus.FocusScorer()
    t = calibrate(scorer)
    for _ in range(40):
        result = scorer.feed(make(1.0, t))
        t += HOP

    assert result.z == pytest.approx(0.0, abs=0.5)
    assert result.value == pytest.approx(0.5, abs=0.15)


def test_higher_engagement_scores_higher():
    scorer_hi = focus.FocusScorer()
    scorer_lo = focus.FocusScorer()
    t_hi = calibrate(scorer_hi)
    t_lo = calibrate(scorer_lo)

    for _ in range(40):
        hi = scorer_hi.feed(make(1.5, t_hi))
        lo = scorer_lo.feed(make(0.5, t_lo))
        t_hi += HOP
        t_lo += HOP

    assert hi.z > lo.z
    assert hi.value > lo.value


def test_squash_is_bounded_and_monotonic():
    # Extremes: the logistic saturates to exactly 0.0 and 1.0 in float64
    # somewhere past |z| ~ 37, so bounds are inclusive and monotonicity is
    # non-strict. Saturation is harmless here -- it just means no wobble or
    # maximum wobble.
    extreme = [focus.squash(z) for z in np.linspace(-50, 50, 201)]
    assert all(0.0 <= v <= 1.0 for v in extreme)
    assert all(a <= b for a, b in zip(extreme, extreme[1:]))

    # Over the range the score actually operates in, it must be strict.
    usable = [focus.squash(z) for z in np.linspace(-10, 10, 201)]
    assert all(0.0 < v < 1.0 for v in usable)
    assert all(a < b for a, b in zip(usable, usable[1:]))

    assert focus.squash(0.0) == pytest.approx(0.5)


def test_near_degenerate_baseline_is_floored():
    """An unusually steady calibration must not produce a hair-trigger score.

    The baseline is frozen, so a student who happened to be very stable for
    30 s would otherwise carry a tiny MAD all session, turning every later
    deviation into an enormous z. The floor bounds that. Without it, the
    baseline below has a MAD near zero and a 50% change scores in the
    hundreds of thousands of sigma.
    """
    rng = np.random.default_rng(0)
    steady = 1.0 + rng.normal(scale=1e-6, size=CALIBRATION_WINDOWS)

    scorer = focus.FocusScorer()
    t = calibrate(scorer, features=steady)

    result = scorer.feed(make(1.5, t))
    expected = 0.5 / (focus.MIN_BASELINE_DISPERSION * 1.0)
    assert abs(result.z) == pytest.approx(expected, rel=0.05)


def test_flat_baseline_saturates_cleanly_rather_than_exploding():
    """A perfectly flat calibration must not yield infinity or NaN.

    Saturating to 1.0 is the *correct* answer for a 99x deviation -- the
    requirement is that it stays finite and in range, not that it stays off
    the ends. The dispersion floor is what keeps the division sane.
    """
    scorer = focus.FocusScorer()
    t = calibrate(scorer, features=[1.0] * CALIBRATION_WINDOWS)

    result = scorer.feed(make(99.0, t))
    assert np.isfinite(result.z)
    assert 0.0 <= result.value <= 1.0
    assert result.value == pytest.approx(1.0)


def test_all_zero_baseline_falls_back_to_neutral():
    """If the feature is identically zero the floor is zero too.

    That is the one case the dispersion floor cannot rescue, so the explicit
    zero-scale guard still has to catch it.
    """
    scorer = focus.FocusScorer()
    t = calibrate(scorer, features=[0.0] * CALIBRATION_WINDOWS)

    result = scorer.feed(make(5.0, t))
    assert result.z == pytest.approx(focus.NEUTRAL_Z)
    assert result.value == pytest.approx(0.5)


# --- smoothing --------------------------------------------------------------

def test_smoothing_prevents_instant_jumps():
    """A step change must ramp, or the pen flickers instead of informing."""
    scorer = focus.FocusScorer()
    t = calibrate(scorer)

    for _ in range(40):          # settle at baseline
        scorer.feed(make(1.0, t))
        t += HOP

    first = scorer.feed(make(3.0, t)).z
    t += HOP
    later = first
    for _ in range(20):
        later = scorer.feed(make(3.0, t)).z
        t += HOP

    assert first < later, "score jumped straight to its target"
    assert later > 1.0, "score never reached the step"


# --- artifacts --------------------------------------------------------------

def test_brief_artifact_holds_the_last_score():
    flags = [True, True, True, True]
    scorer = focus.FocusScorer()
    t = calibrate(scorer)

    for _ in range(40):
        good = scorer.feed(make(2.0, t))
        t += HOP

    blink = scorer.feed(make(2.0, t, artifact=flags))
    assert blink.held
    assert blink.z == pytest.approx(good.z)


def test_persistent_artifact_decays_to_neutral():
    """A fallen-off electrode must fade out, not freeze the pen at maximum."""
    flags = [True, True, True, True]
    scorer = focus.FocusScorer()
    t = calibrate(scorer)

    for _ in range(40):
        scorer.feed(make(3.0, t))
        t += HOP

    result = None
    for _ in range(200):
        result = scorer.feed(make(3.0, t, artifact=flags))
        t += HOP

    assert result.held
    assert result.z == pytest.approx(focus.NEUTRAL_Z, abs=0.05)
    assert result.value == pytest.approx(0.5, abs=0.02)


# --- composition ------------------------------------------------------------

def test_composes_with_extract_stream():
    """The two modules must join without an adapter in between."""
    fs = 256
    n = fs * 45
    t = np.arange(n) / fs
    rng = np.random.default_rng(0)
    samples = rng.normal(size=(n, 4))

    scores = list(focus.score_stream(bp.extract_stream([(samples, t)], fs)))

    assert scores
    assert any(s.calibrating for s in scores)
    assert any(not s.calibrating for s in scores)
    for s in scores:
        if not s.calibrating:
            assert 0.0 < s.value < 1.0


def test_calibrating_and_value_are_never_inconsistent():
    """The contract is exactly: calibrating is (value is None).

    Regression guard. The window that completed the baseline used to report
    calibrating=False while still returning value=None, so a caller checking
    the flag would have consumed a None as a wobble multiplier.
    """
    scorer = focus.FocusScorer()
    t = 0.0
    for _ in range(CALIBRATION_WINDOWS + 40):
        result = scorer.feed(make(1.0 + (t % 0.3), t))
        assert result.calibrating == (result.value is None)
        assert result.calibrating == (result.z is None)
        t += HOP
