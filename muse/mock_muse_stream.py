"""
Synthetic Muse EEG source — lets you develop the whole pipeline without hardware.

Publishes an LSL outlet that is indistinguishable, to a consumer, from the one
`muselsl stream` creates: type='EEG', 5 channels (TP9, AF7, AF8, TP10, Right AUX)
at 256 Hz.

The signal is built from real EEG band oscillators plus 1/f-ish noise, and the
band mix is swept slowly between a "focused" and a "distracted" profile so that
downstream metrics visibly move:

    focused    -> high beta, low theta   (low theta/beta ratio)
    distracted -> high theta, low beta   (high theta/beta ratio)

Usage:
    python mock_muse_stream.py                # sweep between states every 20 s
    python mock_muse_stream.py --state focused
    python mock_muse_stream.py --state distracted
"""

import argparse
import math
import time

import numpy as np
from pylsl import StreamInfo, StreamOutlet, local_clock

from constants import MUSE_NB_EEG_CHANNELS, MUSE_SAMPLING_EEG_RATE

CHANNEL_NAMES = ["TP9", "AF7", "AF8", "TP10", "Right AUX"]

# (low_hz, high_hz) per classical EEG band
BANDS = {
    "delta": (1.0, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta": (13.0, 30.0),
    "gamma": (30.0, 44.0),
}

# Relative amplitude of each band in the two end-point states.
FOCUSED = {"delta": 0.8, "theta": 0.6, "alpha": 0.9, "beta": 2.2, "gamma": 1.0}
DISTRACTED = {"delta": 1.4, "theta": 2.4, "alpha": 1.8, "beta": 0.6, "gamma": 0.4}


def _make_outlet():
    info = StreamInfo(
        name="MockMuse",
        type="EEG",
        channel_count=MUSE_NB_EEG_CHANNELS,
        nominal_srate=MUSE_SAMPLING_EEG_RATE,
        channel_format="float32",
        source_id="mock-muse-0001",
    )
    chans = info.desc().append_child("channels")
    for name in CHANNEL_NAMES:
        chans.append_child("channel") \
             .append_child_value("label", name) \
             .append_child_value("unit", "microvolts") \
             .append_child_value("type", "EEG")
    return StreamOutlet(info, chunk_size=12, max_buffered=360)


def _blend(t, mode, period=20.0):
    """Return band-amplitude dict for time t (seconds)."""
    if mode == "focused":
        w = 0.0
    elif mode == "distracted":
        w = 1.0
    else:  # sweep: cosine ramp between the two states
        w = 0.5 * (1.0 - math.cos(2.0 * math.pi * t / period))
    return {b: FOCUSED[b] * (1 - w) + DISTRACTED[b] * w for b in BANDS}, w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", choices=["focused", "distracted", "sweep"],
                    default="sweep")
    ap.add_argument("--period", type=float, default=20.0,
                    help="seconds for a full focused->distracted->focused sweep")
    args = ap.parse_args()

    outlet = _make_outlet()
    fs = MUSE_SAMPLING_EEG_RATE
    n_ch = MUSE_NB_EEG_CHANNELS
    chunk = 12                      # matches LSL_EEG_CHUNK
    rng = np.random.default_rng(0)

    # A handful of oscillators per band, at fixed random frequencies/phases.
    osc = []
    for band, (lo, hi) in BANDS.items():
        for _ in range(4):
            osc.append((band, rng.uniform(lo, hi), rng.uniform(0, 2 * np.pi)))

    print(f"Mock Muse EEG outlet live: 'MockMuse', type=EEG, "
          f"{n_ch} ch @ {fs} Hz, state={args.state}")
    print("Consumers (muselsl view, stream_eeg.py, ...) will now find a stream.")
    print("Ctrl-C to stop.\n")

    t0 = local_clock()
    n_sent = 0
    last_report = time.time()
    try:
        while True:
            # Wall-clock pacing: emit only as many samples as time has elapsed.
            elapsed = local_clock() - t0
            target = int(elapsed * fs)
            while n_sent < target:
                idx = np.arange(n_sent, min(n_sent + chunk, target))
                if idx.size == 0:
                    break
                tt = idx / fs
                amps, w = _blend(tt[0], args.state, args.period)

                sig = np.zeros((idx.size, n_ch), dtype=np.float32)
                for band, freq, phase in osc:
                    wave = np.sin(2 * np.pi * freq * tt + phase)
                    # Slight per-channel gain so channels are not identical.
                    gains = 1.0 + 0.15 * np.arange(n_ch)
                    sig += (amps[band] * 8.0 * wave)[:, None] * gains[None, :]

                sig += rng.normal(0, 3.0, sig.shape)      # broadband noise
                sig += 50.0                                # DC offset, like Muse
                outlet.push_chunk(sig.tolist())
                n_sent = idx[-1] + 1

            if time.time() - last_report >= 5.0:
                _, w = _blend(local_clock() - t0, args.state, args.period)
                state = "distracted" if w > 0.6 else "focused" if w < 0.4 else "drifting"
                print(f"  t={elapsed:6.1f}s  samples={n_sent:8d}  state={state} (w={w:.2f})")
                last_report = time.time()

            time.sleep(chunk / fs / 2)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
