#!/usr/bin/env python3
"""Generate a self-contained reliability dashboard from SQLite score data."""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "scores.db"
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "dashboard.html"


@dataclass(slots=True)
class ScoreRecord:
    """Normalized score row used by the dashboard."""

    session_id: str
    timestamp: str
    consistency: float
    error_recovery: float
    tool_accuracy: float
    grounding: float
    composite: float
    highlights: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a zero-dependency HTML dashboard from reliability scores."
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Path to SQLite scores database.")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Path to write the generated HTML dashboard.",
    )
    return parser.parse_args()


def extract_highlights(details: str | None) -> str:
    """Extract a short human-readable highlight string from the details column."""

    if not details:
        return "No details available."

    try:
        parsed = json.loads(details)
    except json.JSONDecodeError:
        compact = " ".join(details.split())
        return compact[:140] + ("..." if len(compact) > 140 else "")

    if isinstance(parsed, dict):
        for key in ("highlights", "summary", "notes", "scenario"):
            value = parsed.get(key)
            if isinstance(value, list):
                items = [str(item).strip() for item in value if str(item).strip()]
                if items:
                    return "; ".join(items[:3])
            if isinstance(value, str) and value.strip():
                return value.strip()
        compact = json.dumps(parsed, separators=(",", ":"))
        return compact[:140] + ("..." if len(compact) > 140 else "")

    if isinstance(parsed, list):
        items = [str(item).strip() for item in parsed if str(item).strip()]
        if items:
            return "; ".join(items[:3])

    return str(parsed)


def load_scores(db_path: Path) -> list[ScoreRecord]:
    if not db_path.exists():
        return []

    query = """
        SELECT session_id, timestamp, consistency, error_recovery, tool_accuracy,
               grounding, composite, details
        FROM scores
        ORDER BY timestamp ASC, id ASC
    """
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(query).fetchall()

    return [
        ScoreRecord(
            session_id=str(row[0]),
            timestamp=str(row[1]),
            consistency=float(row[2] or 0.0),
            error_recovery=float(row[3] or 0.0),
            tool_accuracy=float(row[4] or 0.0),
            grounding=float(row[5] or 0.0),
            composite=float(row[6] or 0.0),
            highlights=extract_highlights(row[7]),
        )
        for row in rows
    ]


def score_color(score: float) -> str:
    if score > 80:
        return "#4ade80"
    if score > 50:
        return "#fbbf24"
    return "#f87171"


def render_empty_state() -> str:
    return """\
    <section class="empty-state">
      <h2>No score data found</h2>
      <p>Populate <code>data/scores.db</code> and rerun the generator to build the dashboard.</p>
    </section>
    """


def render_dashboard(records: list[ScoreRecord], db_path: Path) -> str:
    latest = records[-1] if records else None
    history_payload: list[dict[str, Any]] = [
        {"timestamp": record.timestamp, "composite": round(record.composite, 2)}
        for record in records
    ]

    recent_rows = list(reversed(records[-10:]))
    recent_rows_html = "\n".join(
        f"""
        <tr>
          <td>{html.escape(row.timestamp)}</td>
          <td><span class="score-pill" style="color:{score_color(row.composite)}">{row.composite:.1f}</span></td>
          <td>{html.escape(row.highlights)}</td>
        </tr>
        """
        for row in recent_rows
    )

    gauge_markup = ""
    if latest:
        dimensions = [
            ("Consistency", latest.consistency),
            ("Error Recovery", latest.error_recovery),
            ("Tool Accuracy", latest.tool_accuracy),
            ("Grounding", latest.grounding),
        ]
        gauge_markup = "\n".join(
            f"""
            <div class="metric-card">
              <div class="metric-header">
                <span>{label}</span>
                <strong>{value:.1f}</strong>
              </div>
              <div class="metric-track">
                <div class="metric-fill" style="width:{max(0.0, min(100.0, value)):.1f}%; background:{score_color(value)}"></div>
              </div>
            </div>
            """
            for label, value in dimensions
        )

    current_score_html = (
        f"""
        <section class="hero card">
          <div>
            <p class="eyebrow">Current Composite Score</p>
            <div class="big-score" style="color:{score_color(latest.composite)}">{latest.composite:.1f}</div>
            <p class="muted">Latest session: <code>{html.escape(latest.session_id)}</code></p>
          </div>
          <div class="hero-meta">
            <div>
              <span>Updated</span>
              <strong>{html.escape(latest.timestamp)}</strong>
            </div>
            <div>
              <span>Source</span>
              <strong>{html.escape(str(db_path))}</strong>
            </div>
          </div>
        </section>
        """
        if latest
        else render_empty_state()
    )

    chart_html = """
    <section class="card chart-card">
      <div class="section-header">
        <h2>Score History</h2>
        <p class="muted">Composite score over time</p>
      </div>
      <div class="chart-wrapper">
        <canvas id="historyChart" width="900" height="320" aria-label="Composite score history chart"></canvas>
      </div>
    </section>
    """

    table_html = f"""
    <section class="card table-card">
      <div class="section-header">
        <h2>Recent Sessions</h2>
        <p class="muted">Last 10 scored sessions</p>
      </div>
      <div class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>Timestamp</th>
              <th>Composite</th>
              <th>Highlights</th>
            </tr>
          </thead>
          <tbody>
            {recent_rows_html or '<tr><td colspan="3">No recent sessions available.</td></tr>'}
          </tbody>
        </table>
      </div>
    </section>
    """

    history_json = json.dumps(history_payload)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent Reliability Dashboard</title>
  <style>
    :root {{
      --bg: #1a1a2e;
      --card: #16213e;
      --accent: #0f3460;
      --highlight: #e94560;
      --text: #f7f7fb;
      --muted: #a9b4d0;
      --border: rgba(255, 255, 255, 0.08);
      --shadow: 0 18px 42px rgba(0, 0, 0, 0.28);
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(233, 69, 96, 0.18), transparent 30%),
        linear-gradient(180deg, rgba(15, 52, 96, 0.55), rgba(26, 26, 46, 0.95)),
        var(--bg);
      color: var(--text);
      min-height: 100vh;
    }}

    .page {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
    }}

    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      gap: 16px;
      margin-bottom: 24px;
    }}

    .topbar h1 {{
      margin: 0 0 8px;
      font-size: clamp(1.75rem, 4vw, 2.7rem);
      letter-spacing: -0.03em;
    }}

    .muted {{
      color: var(--muted);
      margin: 0;
    }}

    .card {{
      background: linear-gradient(180deg, rgba(22, 33, 62, 0.94), rgba(15, 52, 96, 0.65));
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }}

    .hero {{
      padding: 24px;
      display: grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 24px;
      margin-bottom: 24px;
    }}

    .eyebrow {{
      text-transform: uppercase;
      letter-spacing: 0.16em;
      color: var(--muted);
      font-size: 0.72rem;
      margin: 0 0 10px;
    }}

    .big-score {{
      font-size: clamp(3rem, 10vw, 5.2rem);
      font-weight: 800;
      line-height: 0.95;
      margin-bottom: 12px;
    }}

    .hero-meta {{
      display: grid;
      gap: 12px;
      align-content: start;
    }}

    .hero-meta div {{
      padding: 14px 16px;
      border-radius: 14px;
      background: rgba(15, 52, 96, 0.45);
      border: 1px solid var(--border);
    }}

    .hero-meta span {{
      display: block;
      color: var(--muted);
      font-size: 0.85rem;
      margin-bottom: 6px;
    }}

    .hero-meta strong {{
      display: block;
      word-break: break-word;
    }}

    .metrics-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
      margin-bottom: 24px;
    }}

    .metric-card {{
      padding: 18px;
      background: rgba(15, 52, 96, 0.32);
      border-radius: 16px;
      border: 1px solid var(--border);
    }}

    .metric-header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 12px;
      font-weight: 600;
    }}

    .metric-track {{
      width: 100%;
      height: 14px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.08);
      overflow: hidden;
    }}

    .metric-fill {{
      height: 100%;
      border-radius: inherit;
      transition: width 300ms ease;
    }}

    .section-header {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      margin-bottom: 18px;
    }}

    .section-header h2 {{
      margin: 0;
      font-size: 1.1rem;
    }}

    .chart-card,
    .table-card {{
      padding: 20px;
      margin-bottom: 24px;
    }}

    .chart-wrapper {{
      width: 100%;
      overflow: hidden;
      border-radius: 16px;
      background: rgba(10, 16, 32, 0.28);
      border: 1px solid var(--border);
      padding: 12px;
    }}

    canvas {{
      width: 100%;
      height: auto;
      display: block;
    }}

    .table-wrapper {{
      overflow-x: auto;
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 640px;
    }}

    th, td {{
      text-align: left;
      padding: 12px 14px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      vertical-align: top;
    }}

    thead th {{
      color: var(--muted);
      font-size: 0.86rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}

    tbody tr:hover {{
      background: rgba(233, 69, 96, 0.06);
    }}

    .score-pill {{
      display: inline-block;
      font-weight: 700;
    }}

    .empty-state {{
      padding: 40px 24px;
      background: rgba(22, 33, 62, 0.7);
      border: 1px dashed rgba(255, 255, 255, 0.14);
      border-radius: 18px;
      text-align: center;
      margin-bottom: 24px;
    }}

    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      color: #ffd1da;
    }}

    @media (max-width: 860px) {{
      .page {{
        padding: 18px;
      }}

      .topbar,
      .hero,
      .section-header {{
        grid-template-columns: 1fr;
        display: grid;
      }}

      .metrics-grid {{
        grid-template-columns: 1fr;
      }}
    }}

    @media (max-width: 560px) {{
      .hero,
      .chart-card,
      .table-card {{
        padding: 16px;
      }}

      .metric-card {{
        padding: 14px;
      }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <header class="topbar">
      <div>
        <h1>Agent Reliability Dashboard</h1>
        <p class="muted">Zero-dependency snapshot of consistency, recovery, tool use, and grounding.</p>
      </div>
      <p class="muted">{len(records)} scored session{"s" if len(records) != 1 else ""}</p>
    </header>

    {current_score_html}

    <section class="metrics-grid">
      {gauge_markup or '<div class="card empty-state"><p class="muted">No dimension metrics available yet.</p></div>'}
    </section>

    {chart_html}
    {table_html}
  </main>

  <script>
    const historyData = {history_json};

    function drawHistoryChart() {{
      const canvas = document.getElementById("historyChart");
      if (!canvas) {{
        return;
      }}

      const context = canvas.getContext("2d");
      const rect = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      const width = Math.max(320, Math.floor(rect.width || 900));
      const height = 320;
      canvas.width = width * ratio;
      canvas.height = height * ratio;
      context.scale(ratio, ratio);
      context.clearRect(0, 0, width, height);

      const padding = {{ top: 18, right: 20, bottom: 38, left: 42 }};
      const innerWidth = width - padding.left - padding.right;
      const innerHeight = height - padding.top - padding.bottom;

      context.fillStyle = "rgba(247, 247, 251, 0.7)";
      context.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";

      if (!historyData.length) {{
        context.textAlign = "center";
        context.fillText("No score history available.", width / 2, height / 2);
        return;
      }}

      const values = historyData.map((point) => Number(point.composite) || 0);
      const minValue = Math.min(0, ...values);
      const maxValue = Math.max(100, ...values);

      context.strokeStyle = "rgba(255, 255, 255, 0.08)";
      context.lineWidth = 1;
      for (let i = 0; i <= 4; i += 1) {{
        const y = padding.top + (innerHeight / 4) * i;
        context.beginPath();
        context.moveTo(padding.left, y);
        context.lineTo(width - padding.right, y);
        context.stroke();

        const labelValue = Math.round(maxValue - ((maxValue - minValue) / 4) * i);
        context.fillStyle = "rgba(169, 180, 208, 0.9)";
        context.textAlign = "right";
        context.fillText(String(labelValue), padding.left - 8, y + 4);
      }}

      const stepX = historyData.length === 1 ? innerWidth / 2 : innerWidth / (historyData.length - 1);
      const points = historyData.map((point, index) => {{
        const x = padding.left + stepX * index;
        const normalized = (Number(point.composite) - minValue) / (maxValue - minValue || 1);
        const y = padding.top + innerHeight - normalized * innerHeight;
        return {{ x, y, label: point.timestamp, value: point.composite }};
      }});

      const gradient = context.createLinearGradient(0, padding.top, 0, padding.top + innerHeight);
      gradient.addColorStop(0, "rgba(233, 69, 96, 0.45)");
      gradient.addColorStop(1, "rgba(233, 69, 96, 0.02)");

      context.beginPath();
      points.forEach((point, index) => {{
        if (index === 0) {{
          context.moveTo(point.x, point.y);
        }} else {{
          context.lineTo(point.x, point.y);
        }}
      }});
      context.lineTo(points[points.length - 1].x, padding.top + innerHeight);
      context.lineTo(points[0].x, padding.top + innerHeight);
      context.closePath();
      context.fillStyle = gradient;
      context.fill();

      context.beginPath();
      points.forEach((point, index) => {{
        if (index === 0) {{
          context.moveTo(point.x, point.y);
        }} else {{
          context.lineTo(point.x, point.y);
        }}
      }});
      context.strokeStyle = "#e94560";
      context.lineWidth = 3;
      context.stroke();

      points.forEach((point, index) => {{
        context.beginPath();
        context.arc(point.x, point.y, 4, 0, Math.PI * 2);
        context.fillStyle = "#f7f7fb";
        context.fill();
        context.beginPath();
        context.arc(point.x, point.y, 2.5, 0, Math.PI * 2);
        context.fillStyle = "#e94560";
        context.fill();

        if (index === 0 || index === points.length - 1 || points.length <= 6 || index % Math.ceil(points.length / 5) === 0) {{
          context.fillStyle = "rgba(169, 180, 208, 0.9)";
          context.textAlign = "center";
          const shortLabel = String(point.label).replace("T", " ").slice(0, 16);
          context.fillText(shortLabel, point.x, height - 12);
        }}
      }});
    }}

    window.addEventListener("load", drawHistoryChart);
    window.addEventListener("resize", drawHistoryChart);
  </script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    db_path = Path(args.db_path).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    records = load_scores(db_path)
    output_path.write_text(render_dashboard(records, db_path), encoding="utf-8")
    print(f"Dashboard written to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
