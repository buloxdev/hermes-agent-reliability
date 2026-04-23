#!/usr/bin/env python3
"""Live monitor: parse recent Hermes sessions, score them, and emit a JSON report.

Usage:
    python3 scripts/monitor.py
    python3 scripts/monitor.py --lookback-hours 24 --top-n 15
    python3 scripts/monitor.py --watch 60   # re-run every 60 seconds
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "data" / "monitor-report.json"
TRACE_PARSER = Path(__file__).resolve().parent / "trace_parser.py"
SCORER = Path(__file__).resolve().parent / "scorer.py"

# Thresholds for alerting
THRESHOLDS = {
    "composite_critical": 50.0,
    "composite_warning": 70.0,
    "error_recovery_critical": 40.0,
    "consistency_critical": 40.0,
    "tool_accuracy_critical": 40.0,
    "grounding_critical": 40.0,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=48.0,
        help="Only include sessions started within this many hours (default: 48)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="Include this many top/latest sessions in the report (default: 20)",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Path to write monitor-report.json",
    )
    parser.add_argument(
        "--watch",
        type=int,
        metavar="SECONDS",
        help="Re-run continuously every N seconds",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress terminal output")
    return parser.parse_args()


def run_trace_parser(output_dir: Path) -> dict[str, Any]:
    """Run trace_parser.py on the live Hermes data."""
    cmd = [sys.executable, str(TRACE_PARSER), "--output-dir", str(output_dir)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"trace_parser failed: {result.stderr}")

    # Parser prints JSON summary as last line
    lines = result.stdout.strip().splitlines()
    summary = json.loads(lines[-1]) if lines else {}
    return summary


def run_scorer(traces_dir: Path, db_path: Path) -> dict[str, Any]:
    """Run scorer.py on the parsed traces."""
    cmd = [
        sys.executable,
        str(SCORER),
        "--traces-dir",
        str(traces_dir),
        "--db-path",
        str(db_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"scorer failed: {result.stderr}")

    # Scorer prints human-readable summary; we ignore it and read the DB.
    return {"stdout": result.stdout}


def load_recent_scores(db_path: Path, lookback_hours: float) -> list[dict[str, Any]]:
    """Load scored sessions from the temp SQLite DB."""
    if not db_path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    cutoff_iso = cutoff.isoformat()

    query = """
        SELECT session_id, timestamp, consistency, error_recovery,
               tool_accuracy, grounding, composite, details
        FROM scores
        WHERE timestamp >= ?
        ORDER BY timestamp DESC
    """

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(query, (cutoff_iso,)).fetchall()

    sessions = []
    for row in rows:
        sessions.append(
            {
                "session_id": row[0],
                "timestamp": row[1],
                "consistency": float(row[2] or 0.0),
                "error_recovery": float(row[3] or 0.0),
                "tool_accuracy": float(row[4] or 0.0),
                "grounding": float(row[5] or 0.0),
                "composite": float(row[6] or 0.0),
                "details": row[7],
            }
        )
    return sessions


def build_distribution(scores: list[float]) -> list[int]:
    """Return a flat list of individual scores for client-side binning."""
    return [round(s) for s in scores]


def check_alerts(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect threshold breaches and return alert objects."""
    alerts: list[dict[str, Any]] = []

    for s in sessions:
        sid = s["session_id"]
        comp = s["composite"]
        er = s["error_recovery"]
        c = s["consistency"]
        ta = s["tool_accuracy"]
        g = s["grounding"]

        # Only surface dimension alerts for sessions that are already flagged overall
        flagged = comp < THRESHOLDS["composite_warning"]

        if comp < THRESHOLDS["composite_critical"]:
            alerts.append(
                {
                    "level": "critical",
                    "session_id": sid,
                    "metric": "composite",
                    "value": comp,
                    "message": f"Session {sid}: composite score {comp:.1f} below critical threshold ({THRESHOLDS['composite_critical']:.0f})",
                }
            )
        elif comp < THRESHOLDS["composite_warning"]:
            alerts.append(
                {
                    "level": "warning",
                    "session_id": sid,
                    "metric": "composite",
                    "value": comp,
                    "message": f"Session {sid}: composite score {comp:.1f} below warning threshold ({THRESHOLDS['composite_warning']:.0f})",
                }
            )

        if flagged:
            if er < THRESHOLDS["error_recovery_critical"]:
                alerts.append(
                    {
                        "level": "critical",
                        "session_id": sid,
                        "metric": "error_recovery",
                        "value": er,
                        "message": f"Session {sid}: error recovery {er:.1f} (fragile)",
                    }
                )

            if c < THRESHOLDS["consistency_critical"]:
                alerts.append(
                    {
                        "level": "critical",
                        "session_id": sid,
                        "metric": "consistency",
                        "value": c,
                        "message": f"Session {sid}: consistency {c:.1f} (unstable)",
                    }
                )

            if ta < THRESHOLDS["tool_accuracy_critical"]:
                alerts.append(
                    {
                        "level": "critical",
                        "session_id": sid,
                        "metric": "tool_accuracy",
                        "value": ta,
                        "message": f"Session {sid}: tool accuracy {ta:.1f} (broken tools)",
                    }
                )

            if g < THRESHOLDS["grounding_critical"]:
                alerts.append(
                    {
                        "level": "critical",
                        "session_id": sid,
                        "metric": "grounding",
                        "value": g,
                        "message": f"Session {sid}: grounding {g:.1f} (hallucinating)",
                    }
                )

    # Deduplicate by session_id + metric, keep highest severity
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for a in alerts:
        key = (a["session_id"], a["metric"])
        if key not in seen or (a["level"] == "critical" and seen[key]["level"] != "critical"):
            seen[key] = a

    # Cap to top 10 most severe (critical first, then lowest values)
    def sort_key(a: dict[str, Any]) -> tuple[int, float]:
        return (0 if a["level"] == "critical" else 1, a["value"])

    deduped = sorted(seen.values(), key=sort_key)
    return deduped[:10]


def print_alerts(alerts: list[dict[str, Any]], quiet: bool) -> None:
    if quiet or not alerts:
        return

    print()
    print("=" * 60)
    print("  AGENT RELIABILITY ALERTS")
    print("=" * 60)

    critical = [a for a in alerts if a["level"] == "critical"]
    warning = [a for a in alerts if a["level"] == "warning"]

    for a in critical:
        print(f"  [CRITICAL] {a['message']}")
    for a in warning:
        print(f"  [WARNING]  {a['message']}")

    print("=" * 60)
    print(f"  {len(critical)} critical, {len(warning)} warning")
    print()


def print_summary(report: dict[str, Any], quiet: bool) -> None:
    if quiet:
        return

    fleet = report["fleet_status"]
    print()
    print(f"  Monitor Report: {report['generated_at']}")
    print(f"  Sessions scored: {fleet['sessions_scored']}  (lookback: {report['lookback_hours']}h)")
    print(f"  Fleet avg composite: {fleet['avg_composite']:.1f}")
    print(f"  Dimensions:  C={fleet['avg_consistency']:.1f}  R={fleet['avg_error_recovery']:.1f}  T={fleet['avg_tool_accuracy']:.1f}  G={fleet['avg_grounding']:.1f}")
    print(f"  Alerts: {len(report['alerts'])}")
    print(f"  Output: {report['output_path']}")
    print()


def build_report(
    sessions: list[dict[str, Any]],
    lookback_hours: float,
    top_n: int,
    output_path: Path,
) -> dict[str, Any]:
    if not sessions:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "lookback_hours": lookback_hours,
            "fleet_status": {
                "sessions_scored": 0,
                "avg_composite": 0.0,
                "avg_consistency": 0.0,
                "avg_error_recovery": 0.0,
                "avg_tool_accuracy": 0.0,
                "avg_grounding": 0.0,
            },
            "sessions": [],
            "distribution": [],
            "alerts": [],
            "output_path": str(output_path),
        }

    composites = [s["composite"] for s in sessions]
    consistencies = [s["consistency"] for s in sessions]
    recoveries = [s["error_recovery"] for s in sessions]
    accuracies = [s["tool_accuracy"] for s in sessions]
    groundings = [s["grounding"] for s in sessions]

    fleet_status = {
        "sessions_scored": len(sessions),
        "avg_composite": round(mean(composites), 2),
        "avg_consistency": round(mean(consistencies), 2),
        "avg_error_recovery": round(mean(recoveries), 2),
        "avg_tool_accuracy": round(mean(accuracies), 2),
        "avg_grounding": round(mean(groundings), 2),
    }

    # Top sessions by recency (already sorted DESC by timestamp), limited to top_n
    top_sessions = sessions[:top_n]

    # Build distribution from ALL sessions in lookback
    distribution = build_distribution(composites)

    alerts = check_alerts(sessions)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": lookback_hours,
        "fleet_status": fleet_status,
        "sessions": [
            {
                "session_id": s["session_id"],
                "timestamp": s["timestamp"],
                "composite": s["composite"],
                "consistency": s["consistency"],
                "error_recovery": s["error_recovery"],
                "tool_accuracy": s["tool_accuracy"],
                "grounding": s["grounding"],
            }
            for s in top_sessions
        ],
        "distribution": distribution,
        "alerts": alerts,
        "output_path": str(output_path),
    }


def run_cycle(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="hermes-monitor-") as tmpdir:
        tmp_path = Path(tmpdir)
        traces_dir = tmp_path / "traces"
        db_path = tmp_path / "scores.db"

        # 1. Parse live sessions
        tp_summary = run_trace_parser(traces_dir)

        # 2. Score them
        run_scorer(traces_dir, db_path)

        # 3. Load recent scores
        sessions = load_recent_scores(db_path, args.lookback_hours)

        # 4. Build report
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        report = build_report(sessions, args.lookback_hours, args.top_n, output_path)

        # 5. Write JSON
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        # 6. Terminal output
        print_alerts(report["alerts"], args.quiet)
        print_summary(report, args.quiet)

        return report


def main() -> int:
    args = parse_args()

    if args.watch:
        print(f"Monitoring every {args.watch}s. Press Ctrl+C to stop.")
        while True:
            try:
                run_cycle(args)
            except Exception as exc:
                print(f"Monitor cycle failed: {exc}", file=sys.stderr)
            time.sleep(args.watch)
    else:
        run_cycle(args)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
