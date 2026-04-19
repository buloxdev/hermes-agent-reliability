#!/usr/bin/env python3
"""Compute Hermes agent reliability scores from parsed trace files."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any


DEFAULT_TRACES_DIR = Path(__file__).resolve().parents[1] / "data" / "traces"
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "scores.db"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--traces-dir",
        default=str(DEFAULT_TRACES_DIR),
        help="Directory containing parsed trace JSON files",
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help="SQLite database path for reliability scores",
    )
    return parser.parse_args()


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def load_trace_sessions(traces_dir: Path) -> list[dict[str, Any]]:
    sessions: dict[tuple[str, str], dict[str, Any]] = {}
    if not traces_dir.exists():
        return []

    for path in sorted(traces_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        trace_generated_at = payload.get("generated_at") or path.stem
        for session in payload.get("sessions", []):
            key = (session.get("session_id") or path.stem, trace_generated_at)
            sessions[key] = session | {"trace_timestamp": trace_generated_at, "trace_file": str(path)}

    return list(sessions.values())


def calculate_consistency(session: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    metrics = session.get("metrics", {})
    response_events = [
        event for event in session.get("gateway_events", []) if event.get("type") == "response_ready"
    ]
    response_times = [float(event.get("response_time_seconds", 0.0)) for event in response_events]
    repeated_patterns = metrics.get("repeated_error_patterns", {}) or {}

    if response_times:
        avg_rt = mean(response_times)
        variance = mean((value - avg_rt) ** 2 for value in response_times)
        stdev = math.sqrt(variance)
        cv = stdev / avg_rt if avg_rt > 0 else 1.0
        variance_penalty = min(45.0, cv * 60.0)
    else:
        avg_rt = 0.0
        stdev = 0.0
        cv = 1.0
        variance_penalty = 20.0

    repeat_count = sum(count - 1 for count in repeated_patterns.values())
    repeat_penalty = min(35.0, repeat_count * 8.0)
    missing_response_penalty = 0.0
    if metrics.get("total_messages", 0) > metrics.get("response_count", 0):
        missing_response_penalty = min(
            20.0,
            float(metrics["total_messages"] - metrics["response_count"]) * 5.0,
        )

    score = clamp(100.0 - variance_penalty - repeat_penalty - missing_response_penalty)
    details = {
        "avg_response_time": avg_rt,
        "response_time_stdev": stdev,
        "response_time_cv": cv,
        "variance_penalty": variance_penalty,
        "repeat_penalty": repeat_penalty,
        "missing_response_penalty": missing_response_penalty,
        "repeated_error_patterns": repeated_patterns,
    }
    return score, details


def calculate_error_recovery(session: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    events = list(session.get("gateway_events", []))
    timeout_events = [event for event in events if event.get("type") == "timeout"]
    error_events = [event for event in events if event.get("type") == "error"]
    response_events = [event for event in events if event.get("type") == "response_ready"]

    recoveries = 0
    failures = 0
    retries = 0

    for index, event in enumerate(events):
        if event.get("type") not in {"error", "timeout"}:
            continue

        recovered = False
        for later in events[index + 1 : index + 6]:
            if later.get("type") == "response_ready":
                recoveries += 1
                recovered = True
                break
            if later.get("type") == "inbound_message":
                retries += 1
        if not recovered:
            failures += 1

    if not error_events and not timeout_events:
        score = 100.0 if response_events else 70.0
    else:
        total_incidents = len(error_events) + len(timeout_events)
        recovery_rate = recoveries / total_incidents if total_incidents else 1.0
        retry_bonus = min(10.0, retries * 2.0)
        failure_penalty = failures * 15.0
        timeout_penalty = len(timeout_events) * 4.0
        score = clamp(35.0 + recovery_rate * 55.0 + retry_bonus - failure_penalty - timeout_penalty)

    details = {
        "error_count": len(error_events),
        "timeout_count": len(timeout_events),
        "recoveries": recoveries,
        "unrecovered_failures": failures,
        "retry_signals": retries,
    }
    return score, details


def calculate_tool_accuracy(session: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    metrics = session.get("metrics", {})
    tool_calls = session.get("tool_calls", [])
    tool_results = session.get("tool_results", [])
    transcript_messages = session.get("transcript_messages", [])

    if not tool_calls and not tool_results:
        # Neutral-positive if no tools were required.
        return 80.0, {
            "tool_calls": 0,
            "tool_results": 0,
            "successful_results": 0,
            "failure_results": 0,
            "response_after_tool_ratio": None,
            "note": "No tool usage observed",
        }

    successes = sum(1 for result in tool_results if result.get("success"))
    failures = sum(1 for result in tool_results if not result.get("success"))
    completion_ratio = successes / len(tool_results) if tool_results else 0.0

    response_after_tool = 0
    for index, message in enumerate(transcript_messages):
        if message.get("role") != "tool":
            continue
        if any(later.get("role") == "assistant" for later in transcript_messages[index + 1 : index + 4]):
            response_after_tool += 1

    response_after_tool_ratio = response_after_tool / len(tool_results) if tool_results else 0.0

    orphan_call_penalty = 0.0
    if len(tool_calls) > len(tool_results):
        orphan_call_penalty = min(20.0, (len(tool_calls) - len(tool_results)) * 4.0)

    inappropriate_selection_penalty = 0.0
    if tool_calls and metrics.get("response_chars_total", 0) == 0:
        inappropriate_selection_penalty = 20.0

    score = clamp(
        30.0
        + completion_ratio * 45.0
        + response_after_tool_ratio * 20.0
        - failures * 8.0
        - orphan_call_penalty
        - inappropriate_selection_penalty
    )
    details = {
        "tool_calls": len(tool_calls),
        "tool_results": len(tool_results),
        "successful_results": successes,
        "failure_results": failures,
        "completion_ratio": completion_ratio,
        "response_after_tool_ratio": response_after_tool_ratio,
        "orphan_call_penalty": orphan_call_penalty,
        "inappropriate_selection_penalty": inappropriate_selection_penalty,
    }
    return score, details


def calculate_grounding(session: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    metrics = session.get("metrics", {})
    transcript_messages = session.get("transcript_messages", [])
    tool_output_chars = float(metrics.get("tool_output_chars_total", 0))
    assistant_chars = sum(
        len(message.get("content") or "")
        for message in transcript_messages
        if message.get("role") == "assistant"
    )
    data_points = int(metrics.get("specific_data_points_count", 0))
    tool_results = session.get("tool_results", [])

    if tool_output_chars > 0:
        ratio = assistant_chars / tool_output_chars if tool_output_chars else 0.0
        if ratio <= 3.0:
            ratio_score = 95.0
        elif ratio <= 6.0:
            ratio_score = 85.0
        elif ratio <= 10.0:
            ratio_score = 70.0
        else:
            ratio_score = max(40.0, 100.0 - (ratio - 10.0) * 4.0)
    else:
        ratio = None
        ratio_score = 65.0 if assistant_chars > 0 else 50.0

    data_point_bonus = min(20.0, data_points * 1.5)
    unsupported_penalty = 0.0
    if tool_results and not any(result.get("success") for result in tool_results):
        unsupported_penalty = 15.0

    score = clamp(ratio_score + data_point_bonus - unsupported_penalty)
    details = {
        "assistant_chars": assistant_chars,
        "tool_output_chars": tool_output_chars,
        "assistant_to_tool_output_ratio": ratio,
        "ratio_score": ratio_score,
        "specific_data_points_count": data_points,
        "data_point_bonus": data_point_bonus,
        "unsupported_penalty": unsupported_penalty,
    }
    return score, details


def compute_score(session: dict[str, Any]) -> dict[str, Any]:
    consistency, consistency_details = calculate_consistency(session)
    error_recovery, recovery_details = calculate_error_recovery(session)
    tool_accuracy, tool_details = calculate_tool_accuracy(session)
    grounding, grounding_details = calculate_grounding(session)

    composite = round(
        consistency * 0.25
        + error_recovery * 0.25
        + tool_accuracy * 0.25
        + grounding * 0.25,
        2,
    )

    stable_timestamp = (
        session.get("end_time")
        or session.get("start_time")
        or session.get("trace_timestamp")
        or datetime.now().isoformat()
    )

    return {
        "session_id": session.get("session_id"),
        "timestamp": stable_timestamp,
        "consistency": round(consistency, 2),
        "error_recovery": round(error_recovery, 2),
        "tool_accuracy": round(tool_accuracy, 2),
        "grounding": round(grounding, 2),
        "composite": composite,
        "details": {
            "trace_file": session.get("trace_file"),
            "metrics": session.get("metrics", {}),
            "consistency": consistency_details,
            "error_recovery": recovery_details,
            "tool_accuracy": tool_details,
            "grounding": grounding_details,
        },
    }


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
          id INTEGER PRIMARY KEY,
          session_id TEXT,
          timestamp TEXT,
          consistency REAL,
          error_recovery REAL,
          tool_accuracy REAL,
          grounding REAL,
          composite REAL,
          details TEXT
        );
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_scores_session_timestamp ON scores(session_id, timestamp);"
    )
    connection.commit()


def store_scores(connection: sqlite3.Connection, scores: list[dict[str, Any]]) -> int:
    inserted = 0
    for score in scores:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO scores (
              session_id, timestamp, consistency, error_recovery,
              tool_accuracy, grounding, composite, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                score["session_id"],
                score["timestamp"],
                score["consistency"],
                score["error_recovery"],
                score["tool_accuracy"],
                score["grounding"],
                score["composite"],
                json.dumps(score["details"], sort_keys=True),
            ),
        )
        inserted += cursor.rowcount
    connection.commit()
    return inserted


def print_summary(scores: list[dict[str, Any]], inserted: int, db_path: Path) -> None:
    if not scores:
        print("No trace sessions found. Nothing scored.")
        return

    avg_composite = mean(score["composite"] for score in scores)
    worst = min(scores, key=lambda item: item["composite"])
    best = max(scores, key=lambda item: item["composite"])

    print(f"Scored {len(scores)} session(s); inserted {inserted} row(s) into {db_path}")
    print(f"Average composite: {avg_composite:.2f}")
    print(
        "Best session: "
        f"{best['session_id']} composite={best['composite']:.2f} "
        f"(C={best['consistency']:.1f} R={best['error_recovery']:.1f} "
        f"T={best['tool_accuracy']:.1f} G={best['grounding']:.1f})"
    )
    print(
        "Worst session: "
        f"{worst['session_id']} composite={worst['composite']:.2f} "
        f"(C={worst['consistency']:.1f} R={worst['error_recovery']:.1f} "
        f"T={worst['tool_accuracy']:.1f} G={worst['grounding']:.1f})"
    )


def main() -> int:
    args = parse_args()
    traces_dir = Path(args.traces_dir).expanduser()
    db_path = Path(args.db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    sessions = load_trace_sessions(traces_dir)
    scores = [compute_score(session) for session in sessions]

    with sqlite3.connect(db_path) as connection:
        ensure_schema(connection)
        inserted = store_scores(connection, scores)

    print_summary(scores, inserted, db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
