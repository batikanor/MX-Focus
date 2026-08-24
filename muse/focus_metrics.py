"""
Local focus/attention metrics engine + drop-in REST API.

Why this exists
---------------
The original pipeline sent EEG to AWS IoT Core, scored it with a cloud
scikit-learn model, and exposed a single `calmness` float through API Gateway.
That API Gateway stage no longer exists (the hostname does not resolve), so the
Unity client can no longer obtain a score.

This module reproduces that contract locally and extends it. It reads EEG
straight from the LSL stream (real Muse or `mock_muse_stream.py`), computes
band powers, derives several attention-related indices, and serves them over
plain HTTP.

Endpoints
---------
GET /calmness_data   Legacy shape, byte-compatible with the old AWS response.
                     Unity's existing CalmnessResponse/CalmnessBody parsing
                     works unchanged -- only the URL needs to point here.

GET /metrics         Full metric set (see MetricSet below).

GET /health          Stream liveness + per-channel signal quality.

Usage
-----
    python focus_metrics.py                  # serve on 127.0.0.1:8000
    python focus_metrics.py --port 9000
    python focus_metrics.py --print          # also log metrics to stdout

Metric definitions
------------------
All are computed from Welch power spectral density over a sliding window.

  calmness            alpha / (alpha + beta), 0..1. High alpha with low beta is
                      the classic relaxed-but-awake signature. Kept for
                      backwards compatibility with the Unity client.

  engagement          beta / (alpha + theta) -- Pope et al. engagement index,
                      the standard adaptive-automation attention measure.
                      Normalised to 0..1 against a running baseline.

  theta_beta_ratio    theta / beta at frontal sites. Elevated TBR is the most
                      replicated EEG correlate of inattention.

  focus               Composite: high engagement, low TBR, low artifact load.
                      This is the number to drive VR feedback with.

  workload            frontal theta / posterior alpha. Rises with cognitive
                      effort; useful for spotting a question that is too hard.

  alpha_asymmetry     log(alpha_AF8) - log(alpha_AF7). Negative values are
                      associated with approach/engagement, positive with
                      withdrawal/frustration.

  drowsiness          (delta + theta) / (alpha + beta). Rises as vigilance falls.

  artifact_ratio      Fraction of the window flagged as blink/jaw/movement.
                      When high, treat the other metrics as unreliable.
"""

from __future__ import annotations

import argparse
import csv
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from scipy import signal as sps

from constants import (
    LSL_EEG_CHUNK,
    LSL_SCAN_TIMEOUT,
    MUSE_NB_EEG_CHANNELS,
    MUSE_SAMPLING_EEG_RATE,
)

# Muse channel order as published by muselsl.
TP9, AF7, AF8, TP10, AUX = 0, 1, 2, 3, 4
FRONTAL = [AF7, AF8]
POSTERIOR = [TP9, TP10]

BANDS = {
    "delta": (1.0, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta": (13.0, 30.0),
    "gamma": (30.0, 44.0),
}

# np.trapz was renamed np.trapezoid in NumPy 2.0; support both.
_trapz = getattr(np, "trapezoid", None) or np.trapz

WINDOW_SECONDS = 4.0          # analysis window
UPDATE_HZ = 10.0              # how often metrics are recomputed
ARTIFACT_UV = 150.0           # |sample| above this (after detrend) = artifact
EPS = 1e-12


@dataclass
class MetricSet:
    calmness: float = 0.5
    focus: float = 0.5
    engagement: float = 0.5
    theta_beta_ratio: float = 0.0
    workload: float = 0.0
    alpha_asymmetry: float = 0.0
    drowsiness: float = 0.0
    artifact_ratio: float = 0.0
    band_powers: dict = field(default_factory=dict)
    signal_quality: list = field(default_factory=list)
    stream_ok: bool = False
    updated_at: float = 0.0


def _bandpowers(window: np.ndarray, fs: float) -> np.ndarray:
    """Welch PSD -> absolute band power. Returns (n_channels, n_bands)."""
    # Detrend removes the ~50 uV DC offset the Muse carries.
    win = sps.detrend(window, axis=0, type="constant")
    nperseg = min(len(win), int(fs * 1.0))          # 1 s segments -> 1 Hz bins
    freqs, psd = sps.welch(win, fs=fs, nperseg=nperseg, axis=0)

    out = np.zeros((win.shape[1], len(BANDS)))
    for bi, (lo, hi) in enumerate(BANDS.values()):
        mask = (freqs >= lo) & (freqs < hi)
        if mask.any():
            # Integrate the PSD across the band.
            out[:, bi] = _trapz(psd[mask], freqs[mask], axis=0)
    return out


def _norm(x: float, lo: float, hi: float) -> float:
    """Clamp x into 0..1 given an expected operating range."""
    if hi <= lo:
        return 0.5
    return float(np.clip((x - lo) / (hi - lo), 0.0, 1.0))


class SessionLogger:
    """Per-exam-session recorder: a timestamped metrics CSV plus an event
    log (question appearances, manual markers, ...). One instance covers one
    student sitting one exam; call start() at the beginning and stop() (which
    also renders the teacher dashboard) at the end.

    Layout written under sessions/<start_time>[_<student>]/:
        metrics.csv   one row per ~1 s: elapsed_s, timestamp, all MetricSet fields
        events.jsonl  one JSON object per line: elapsed_s, timestamp, type, ...
        report.html   generated by session_report.py when the session stops
    """

    LOG_PERIOD = 1.0   # seconds between metrics.csv rows

    def __init__(self, root_dir: str = "sessions"):
        self.root = Path(root_dir)
        self.root.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self.session_dir: Path | None = None
        self.student: str | None = None
        self.start_time: float | None = None
        self._csv_file = None
        self._csv_writer = None
        self._last_log_t = 0.0

    @property
    def active(self) -> bool:
        return self._csv_writer is not None

    def start(self, student: str | None = None) -> str:
        with self._lock:
            self._close_locked()
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            name = f"{ts}_{student}" if student else ts
            self.session_dir = self.root / name
            self.session_dir.mkdir(parents=True, exist_ok=True)
            self._csv_file = open(self.session_dir / "metrics.csv", "w",
                                   newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(
                ["elapsed_s", "timestamp", "focus", "calmness", "engagement",
                 "theta_beta_ratio", "workload", "alpha_asymmetry",
                 "drowsiness", "artifact_ratio"])
            self.start_time = time.time()
            self._last_log_t = 0.0
            self.student = student
            print(f"[session] started -> {self.session_dir}")
            return str(self.session_dir)

    def maybe_log_metrics(self, m: "MetricSet"):
        """Called every analysis tick; throttles itself to LOG_PERIOD."""
        now = time.time()
        if not self.active or (now - self._last_log_t) < self.LOG_PERIOD:
            return
        with self._lock:
            if not self._csv_writer:
                return
            self._last_log_t = now
            self._csv_writer.writerow([
                round(now - self.start_time, 2), round(now, 3), m.focus,
                m.calmness, m.engagement, m.theta_beta_ratio, m.workload,
                m.alpha_asymmetry, m.drowsiness, m.artifact_ratio,
            ])
            self._csv_file.flush()

    def log_event(self, event: dict) -> dict:
        with self._lock:
            if not self.session_dir:
                # No explicit start() yet -- auto-start an unnamed session so
                # events from an eager Unity client are never silently lost.
                pass
            now = time.time()
            row = {
                "elapsed_s": round(now - self.start_time, 2) if self.start_time else None,
                "timestamp": now,
                **event,
            }
            if self.session_dir:
                with open(self.session_dir / "events.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
            return row

    def _close_locked(self):
        if self._csv_file:
            self._csv_file.close()
        self._csv_file = None
        self._csv_writer = None

    def stop(self, render_report: bool = True) -> dict:
        with self._lock:
            session_dir = self.session_dir
            self._close_locked()
        if not session_dir:
            return {"session_dir": None, "report": None}

        report_path = None
        if render_report:
            try:
                import session_report
                report_path = session_report.generate(session_dir)
            except Exception as e:
                print(f"[session] report generation failed: {e}")

        print(f"[session] stopped -> {session_dir}")
        self.session_dir = None
        self.start_time = None
        return {"session_dir": str(session_dir), "report": report_path}


class MetricsEngine:
    """Pulls EEG from LSL in a background thread and keeps MetricSet current."""

    def __init__(self, fs: float = MUSE_SAMPLING_EEG_RATE,
                 n_ch: int = MUSE_NB_EEG_CHANNELS,
                 session_logger: "SessionLogger | None" = None):
        self.fs = fs
        self.n_ch = n_ch
        self.buf = deque(maxlen=int(WINDOW_SECONDS * fs))
        self.metrics = MetricSet()
        self.session_logger = session_logger
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # Running percentiles let engagement/focus self-calibrate per person,
        # which matters because absolute EEG power varies hugely between users.
        self._eng_hist = deque(maxlen=int(120 * UPDATE_HZ))   # ~2 min
        # Drowsiness is a slow-changing physiological state, not a per-tick
        # event -- median-smooth over a ~3 s window so one noisy PSD estimate
        # can't spike an alert.
        self._drowsy_hist = deque(maxlen=int(3 * UPDATE_HZ))
        self._last_sample_t = 0.0

    # -- acquisition ------------------------------------------------------
    def _acquire(self):
        from pylsl import StreamInlet, resolve_byprop

        while not self._stop.is_set():
            print("[metrics] looking for an EEG stream...")
            streams = resolve_byprop("type", "EEG", timeout=LSL_SCAN_TIMEOUT)
            if not streams:
                print("[metrics] no EEG stream; retrying in 3 s")
                time.sleep(3)
                continue

            info = streams[0]
            print(f"[metrics] connected to '{info.name()}' "
                  f"({info.channel_count()} ch @ {info.nominal_srate()} Hz)")
            inlet = StreamInlet(info, max_chunklen=LSL_EEG_CHUNK)
            self.fs = info.nominal_srate() or self.fs
            self.n_ch = info.channel_count()
            self.buf = deque(maxlen=int(WINDOW_SECONDS * self.fs))
            self._last_sample_t = time.time()

            while not self._stop.is_set():
                # Pull everything available -- do NOT throttle to 1 sample.
                data, ts = inlet.pull_chunk(timeout=1.0, max_samples=256)
                if ts:
                    self.buf.extend(data)
                    self._last_sample_t = time.time()
                elif time.time() - self._last_sample_t > 5.0:
                    print("[metrics] stream went silent; rescanning")
                    break

    # -- analysis ---------------------------------------------------------
    def _analyse(self):
        period = 1.0 / UPDATE_HZ
        while not self._stop.is_set():
            time.sleep(period)
            if len(self.buf) < int(self.fs * 2):        # need >=2 s
                with self._lock:
                    self.metrics.stream_ok = False
                continue

            w = np.asarray(self.buf, dtype=float)[:, : self.n_ch]

            # --- artifact load ---
            centred = w - w.mean(axis=0, keepdims=True)
            bad = np.abs(centred) > ARTIFACT_UV
            artifact_ratio = float(bad.any(axis=1).mean())

            # --- per-channel signal quality (0..1) ---
            # Flat (disconnected) or wildly noisy channels both score low.
            sd = centred.std(axis=0)
            quality = [
                round(float(np.clip(1.0 - abs(np.log10((s + EPS) / 20.0)) / 1.5,
                                    0.0, 1.0)), 3)
                for s in sd
            ]

            # --- band powers ---
            bp = _bandpowers(w, self.fs)               # (n_ch, n_bands)
            names = list(BANDS)
            idx = {n: i for i, n in enumerate(names)}

            def band(name, chans=None):
                rows = bp if chans is None else bp[chans]
                return float(rows[:, idx[name]].mean())

            delta_a, theta_a = band("delta"), band("theta")
            alpha_a, beta_a = band("alpha"), band("beta")

            theta_f = band("theta", FRONTAL)
            beta_f = band("beta", FRONTAL)
            alpha_p = band("alpha", POSTERIOR)

            # --- derived indices ---
            calmness = alpha_a / (alpha_a + beta_a + EPS)
            engagement_raw = beta_a / (alpha_a + theta_a + EPS)
            tbr = theta_f / (beta_f + EPS)
            workload = theta_f / (alpha_p + EPS)

            # --- drowsiness ---
            # Relative (not absolute) band power: each channel's power in a
            # band divided by that channel's *total* power in this window.
            # This makes the index robust to electrode-contact/impedance
            # swings, which otherwise masquerade as a state change.
            total_p = bp.sum(axis=1, keepdims=True) + EPS
            bp_rel = bp / total_p
            delta_r = float(bp_rel[:, idx["delta"]].mean())
            theta_r = float(bp_rel[:, idx["theta"]].mean())
            beta_r = float(bp_rel[:, idx["beta"]].mean())
            # Alpha is deliberately excluded from the ratio: it rises both in
            # a calm-but-focused state and in early drowsiness, so it can't
            # discriminate the two. Beta dropping out while theta/delta rise
            # is the unambiguous part of the drowsiness signature.
            drowsy_raw = (theta_r + delta_r) / (beta_r + EPS)
            drowsy_norm = _norm(drowsy_raw, 0.5, 8.0)
            self._drowsy_hist.append(drowsy_norm)
            drowsiness = float(np.median(self._drowsy_hist))

            if self.n_ch > max(FRONTAL):
                a_left = bp[AF7, idx["alpha"]]
                a_right = bp[AF8, idx["alpha"]]
                asym = float(np.log(a_right + EPS) - np.log(a_left + EPS))
            else:
                asym = 0.0

            # Self-calibrating engagement: rank current value against this
            # session's own distribution rather than an absolute threshold.
            self._eng_hist.append(engagement_raw)
            if len(self._eng_hist) >= 20:
                lo, hi = np.percentile(self._eng_hist, [10, 90])
                engagement = _norm(engagement_raw, lo, hi)
            else:
                engagement = _norm(engagement_raw, 0.2, 2.0)

            # Composite focus: engagement up, TBR down, artifacts penalised.
            tbr_term = 1.0 - _norm(tbr, 1.0, 6.0)
            focus = 0.6 * engagement + 0.4 * tbr_term
            focus *= (1.0 - 0.5 * artifact_ratio)

            with self._lock:
                self.metrics = MetricSet(
                    calmness=round(float(calmness), 4),
                    focus=round(float(np.clip(focus, 0, 1)), 4),
                    engagement=round(float(engagement), 4),
                    theta_beta_ratio=round(float(tbr), 4),
                    workload=round(float(workload), 4),
                    alpha_asymmetry=round(asym, 4),
                    drowsiness=round(float(drowsiness), 4),
                    artifact_ratio=round(artifact_ratio, 4),
                    band_powers={n: round(band(n), 3) for n in names},
                    signal_quality=quality,
                    stream_ok=True,
                    updated_at=time.time(),
                )
                if self.session_logger:
                    self.session_logger.maybe_log_metrics(self.metrics)

    def snapshot(self) -> MetricSet:
        with self._lock:
            return self.metrics

    def start(self):
        for fn in (self._acquire, self._analyse):
            threading.Thread(target=fn, daemon=True).start()

    def stop(self):
        self._stop.set()


def make_handler(engine: MetricsEngine, logger: SessionLogger):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            # Unity's UnityWebRequest is not browser-CORS bound, but a web
            # dashboard would be, so allow it.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}

        def do_GET(self):
            m = engine.snapshot()
            path = self.path.split("?")[0].rstrip("/") or "/"

            if path in ("/calmness_data", "/prod/calmness_data"):
                # Exactly the legacy AWS envelope: body is a JSON *string*.
                self._send({"statusCode": 200,
                            "body": json.dumps({"calmness": m.calmness})})
            elif path == "/metrics":
                self._send(asdict(m))
            elif path == "/health":
                self._send({"stream_ok": m.stream_ok,
                            "signal_quality": m.signal_quality,
                            "artifact_ratio": m.artifact_ratio,
                            "age_seconds": round(time.time() - m.updated_at, 2)
                            if m.updated_at else None})
            else:
                self._send({"error": "not found",
                            "endpoints": ["/calmness_data", "/metrics",
                                          "/health", "/session/start",
                                          "/session/stop", "/session/event"]},
                           code=404)

        def do_POST(self):
            path = self.path.split("?")[0].rstrip("/") or "/"
            body = self._read_json_body()

            if path == "/session/start":
                session_dir = logger.start(student=body.get("student"))
                self._send({"session_dir": session_dir})
            elif path == "/session/stop":
                self._send(logger.stop())
            elif path == "/session/event":
                if "type" not in body:
                    self._send({"error": "event body needs a 'type' field"}, code=400)
                else:
                    self._send(logger.log_event(body))
            else:
                self._send({"error": "not found"}, code=404)

        def log_message(self, *a):        # silence per-request logging
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="log metrics to stdout once per second")
    ap.add_argument("--student", default=None,
                    help="name/id to auto-start a session log under; "
                         "omit to start an unnamed session automatically")
    ap.add_argument("--no-session", action="store_true",
                    help="disable session logging entirely")
    args = ap.parse_args()

    logger = SessionLogger()
    engine = MetricsEngine(session_logger=None if args.no_session else logger)
    engine.start()
    if not args.no_session:
        logger.start(student=args.student)

    srv = ThreadingHTTPServer((args.host, args.port), make_handler(engine, logger))
    print(f"[metrics] serving on http://{args.host}:{args.port}")
    print("[metrics]   /calmness_data  (drop-in for the dead AWS endpoint)")
    print("[metrics]   /metrics        (full metric set)")
    print("[metrics]   /health         (signal quality)")
    print("[metrics]   /session/start  POST {student?}  -> restart the log under a new name")
    print("[metrics]   /session/stop   POST             -> close the log, render report.html")
    print("[metrics]   /session/event  POST {type,...}  -> record a timestamped marker")
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        while True:
            time.sleep(1)
            if args.do_print:
                m = engine.snapshot()
                if m.stream_ok:
                    print(f"focus={m.focus:.3f} calm={m.calmness:.3f} "
                          f"eng={m.engagement:.3f} TBR={m.theta_beta_ratio:.2f} "
                          f"load={m.workload:.2f} drowsy={m.drowsiness:.2f} "
                          f"artifact={m.artifact_ratio:.2f}")
                else:
                    print("waiting for EEG stream...")
    except KeyboardInterrupt:
        print("\nshutting down")
        engine.stop()
        if not args.no_session:
            result = logger.stop()
            if result.get("report"):
                print(f"[session] report: {result['report']}")


if __name__ == "__main__":
    main()
