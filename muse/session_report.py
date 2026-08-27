"""
Teacher dashboard: renders one exam session's metrics.csv + events.jsonl
(written by SessionLogger in focus_metrics.py) into a single self-contained
report.html -- a focus/engagement/workload/drowsiness timeline with vertical
markers wherever a new exam question appeared, plus a per-question summary
table.

Called automatically by `POST /session/stop` against the running
focus_metrics.py server. Can also be run standalone:

    python session_report.py --session sessions/2026-08-24_14-05-01
    python session_report.py --latest        # most recent session under sessions/
"""

from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")          # headless: no display needed to render PNGs
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

METRIC_COLUMNS = ["focus", "engagement", "workload", "drowsiness",
                   "calmness", "theta_beta_ratio", "artifact_ratio"]
CHART_COLUMNS = ["focus", "engagement", "workload", "drowsiness"]
COLORS = {"focus": "#2563eb", "engagement": "#16a34a",
          "workload": "#d97706", "drowsiness": "#dc2626"}


def _load(session_dir: Path):
    csv_path = session_dir / "metrics.csv"
    events_path = session_dir / "events.jsonl"

    df = pd.read_csv(csv_path) if csv_path.exists() else pd.DataFrame(
        columns=["elapsed_s", "timestamp"] + METRIC_COLUMNS)

    events = []
    if events_path.exists():
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return df, events


def _chart_png_b64(df: pd.DataFrame, events: list[dict]) -> str:
    fig, ax = plt.subplots(figsize=(11, 4.2), dpi=140)

    if not df.empty:
        for col in CHART_COLUMNS:
            if col in df.columns:
                ax.plot(df["elapsed_s"], df[col], label=col.replace("_", " "),
                        color=COLORS.get(col), linewidth=1.6)

    question_events = [e for e in events if e.get("type") == "question_appeared"]
    for e in question_events:
        x = e.get("elapsed_s")
        if x is None:
            continue
        ax.axvline(x, color="#6b7280", linestyle="--", linewidth=0.8, alpha=0.7)
        label = str(e.get("question", e.get("detail", "?")))
        ax.annotate(f"Q{label}", xy=(x, 1.02), xycoords=("data", "axes fraction"),
                    rotation=90, fontsize=7, color="#374151",
                    ha="center", va="bottom")

    ax.set_xlabel("elapsed time (s)")
    ax.set_ylabel("score (0-1)")
    ax.set_ylim(-0.02, 1.05)
    ax.legend(loc="upper right", fontsize=8, ncol=4, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e5e7eb", linewidth=0.7)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _per_question_table(df: pd.DataFrame, events: list[dict]) -> list[dict]:
    """Average metrics in the window between successive question markers."""
    q_events = sorted(
        (e for e in events if e.get("type") == "question_appeared" and e.get("elapsed_s") is not None),
        key=lambda e: e["elapsed_s"],
    )
    if not q_events or df.empty:
        return []

    rows = []
    for i, e in enumerate(q_events):
        start = e["elapsed_s"]
        end = q_events[i + 1]["elapsed_s"] if i + 1 < len(q_events) else df["elapsed_s"].max()
        window = df[(df["elapsed_s"] >= start) & (df["elapsed_s"] < end)]
        if window.empty:
            continue
        rows.append({
            "question": e.get("question", "?"),
            "duration_s": round(float(end - start), 1),
            "avg_focus": round(float(window["focus"].mean()), 3),
            "avg_workload": round(float(window["workload"].mean()), 3),
            "max_drowsiness": round(float(window["drowsiness"].max()), 3),
        })
    return rows


def _summary(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    duration = float(df["elapsed_s"].max() - df["elapsed_s"].min())
    drowsy_frac = float((df["drowsiness"] > 0.6).mean())
    artifact_frac = float((df["artifact_ratio"] > 0.2).mean())
    return {
        "duration_min": round(duration / 60, 1),
        "avg_focus": round(float(df["focus"].mean()), 3),
        "avg_engagement": round(float(df["engagement"].mean()), 3),
        "avg_workload": round(float(df["workload"].mean()), 3),
        "pct_time_drowsy": round(drowsy_frac * 100, 1),
        "pct_time_low_quality": round(artifact_frac * 100, 1),
        "min_focus": round(float(df["focus"].min()), 3),
    }


_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>MX Focus session report - {student}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 2rem;
         color: #111827; background: #f9fafb; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.1rem; }}
  .sub {{ color: #6b7280; margin-bottom: 1.4rem; font-size: 0.9rem; }}
  .cards {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.6rem; }}
  .card {{ background: white; border: 1px solid #e5e7eb; border-radius: 10px;
          padding: 0.9rem 1.2rem; min-width: 120px; }}
  .card .v {{ font-size: 1.5rem; font-weight: 600; }}
  .card .k {{ font-size: 0.75rem; color: #6b7280; text-transform: uppercase;
             letter-spacing: 0.03em; }}
  img {{ max-width: 100%; border: 1px solid #e5e7eb; border-radius: 10px;
        background: white; }}
  table {{ border-collapse: collapse; margin-top: 1.4rem; width: 100%;
          background: white; border: 1px solid #e5e7eb; border-radius: 10px;
          overflow: hidden; }}
  th, td {{ padding: 0.5rem 0.8rem; text-align: left; font-size: 0.85rem;
           border-bottom: 1px solid #f0f0f0; }}
  th {{ background: #f3f4f6; color: #374151; }}
</style></head>
<body>
  <h1>MX Focus &mdash; session report</h1>
  <div class="sub">{student} &middot; {session_dir}</div>

  <div class="cards">{cards}</div>

  <img src="data:image/png;base64,{chart_b64}" alt="focus timeline">

  {question_table}
</body></html>
"""

_CARD = """<div class="card"><div class="v">{v}</div><div class="k">{k}</div></div>"""

_CARD_LABELS = {
    "duration_min": ("min", "duration"),
    "avg_focus": ("", "avg focus"),
    "avg_engagement": ("", "avg engagement"),
    "avg_workload": ("", "avg workload"),
    "pct_time_drowsy": ("%", "time drowsy"),
    "pct_time_low_quality": ("%", "time low signal"),
    "min_focus": ("", "min focus"),
}


def generate(session_dir: str | Path) -> str:
    session_dir = Path(session_dir)
    df, events = _load(session_dir)

    chart_b64 = _chart_png_b64(df, events)
    summary = _summary(df)
    q_rows = _per_question_table(df, events)

    cards = "".join(
        _CARD.format(v=f"{summary[k]}{unit}", k=label)
        for k, (unit, label) in _CARD_LABELS.items() if k in summary
    )

    if q_rows:
        body_rows = "".join(
            f"<tr><td>Q{r['question']}</td><td>{r['duration_s']}s</td>"
            f"<td>{r['avg_focus']}</td><td>{r['avg_workload']}</td>"
            f"<td>{r['max_drowsiness']}</td></tr>"
            for r in q_rows
        )
        question_table = (
            "<table><tr><th>Question</th><th>Time spent</th>"
            "<th>Avg focus</th><th>Avg workload</th><th>Peak drowsiness</th></tr>"
            f"{body_rows}</table>"
        )
    else:
        question_table = (
            '<p style="color:#6b7280">No question-appearance events were '
            "recorded for this session -- the timeline above still shows the "
            "overall attention trace.</p>"
        )

    student = session_dir.name.split("_", 2)[-1] if "_" in session_dir.name else "unnamed"
    html = _HTML_TEMPLATE.format(
        student=student, session_dir=session_dir.name, cards=cards or "<p>No data recorded.</p>",
        chart_b64=chart_b64, question_table=question_table,
    )

    out_path = session_dir / "report.html"
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)


def _latest_session(root: Path) -> Path | None:
    dirs = [d for d in root.glob("*") if d.is_dir()]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", help="path to a session directory")
    ap.add_argument("--latest", action="store_true",
                    help="use the most recently modified session under sessions/")
    args = ap.parse_args()

    if args.latest:
        session_dir = _latest_session(Path("sessions"))
        if session_dir is None:
            raise SystemExit("no sessions found under sessions/")
    elif args.session:
        session_dir = Path(args.session)
    else:
        raise SystemExit("pass --session <dir> or --latest")

    path = generate(session_dir)
    print(f"report written: {path}")


if __name__ == "__main__":
    main()
