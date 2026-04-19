#!/usr/bin/env python3
"""Generate synthetic reliability traces and compare scenario scores."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


ROOT_DIR = Path(__file__).resolve().parent.parent
TRACE_DIR = ROOT_DIR / "data" / "traces"
DB_PATH = ROOT_DIR / "data" / "scores.db"
SCORER_PATH = ROOT_DIR / "scripts" / "scorer.py"
SCENARIO_ORDER = ("A", "B", "C", "D")


@dataclass(slots=True)
class ScenarioResult:
    scenario_code: str
    scenario_name: str
    session_id: str
    timestamp: str
    composite: float
    consistency: float
    error_recovery: float
    tool_accuracy: float
    grounding: float
    highlights: list[str]
    source: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run known-good and known-bad synthetic traces through the reliability scorer."
    )
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["A", "B", "C", "D", "all"],
        help="Scenario to run. Defaults to all scenarios.",
    )
    return parser.parse_args()


def iso_timestamp(offset_minutes: int = 0) -> str:
    base = datetime.now(timezone.utc).replace(microsecond=0)
    return (base + timedelta(minutes=offset_minutes)).isoformat()


def build_messages(contents: list[tuple[str, str]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index, (role, content) in enumerate(contents):
        messages.append(
            {
                "id": f"msg_{index + 1}",
                "role": role,
                "content": content,
                "timestamp": iso_timestamp(index),
            }
        )
    return messages


def build_good_agent() -> dict[str, Any]:
    session_id = "demo-good-agent"
    return {
        "session_id": session_id,
        "timestamp": iso_timestamp(),
        "scenario": "A",
        "scenario_name": "Good Agent",
        "messages": build_messages(
            [
                ("user", "Find the latest deployment status and summarize the release risk."),
                ("assistant", "I will check the deployment records and compare them with the latest failing checks."),
                ("assistant", "Deployment completed successfully. No blocking regressions were found and one flaky warning was retried successfully."),
            ]
        ),
        "tool_calls": [
            {
                "tool": "search_deployments",
                "success": True,
                "latency_ms": 210,
                "used_in_final_answer": True,
                "matched_user_request": True,
                "notes": "Used the right deployment lookup immediately.",
            },
            {
                "tool": "fetch_ci_status",
                "success": True,
                "latency_ms": 240,
                "used_in_final_answer": True,
                "matched_user_request": True,
                "notes": "Cross-checked deployment with CI before responding.",
            },
        ],
        "errors": [],
        "response_times": [0.8, 1.0, 0.9],
        "api_calls_count": 3,
    }


def build_flaky_agent() -> dict[str, Any]:
    session_id = "demo-flaky-agent"
    return {
        "session_id": session_id,
        "timestamp": iso_timestamp(5),
        "scenario": "B",
        "scenario_name": "Flaky Agent",
        "messages": build_messages(
            [
                ("user", "Check whether the billing import finished and list the failures."),
                ("assistant", "Looking into the billing import now."),
                ("assistant", "The import appears fine, although one tool timed out so I am retrying."),
                ("assistant", "The import had several failures, but some details may still be loading."),
            ]
        ),
        "tool_calls": [
            {
                "tool": "fetch_import_status",
                "success": False,
                "latency_ms": 1600,
                "used_in_final_answer": False,
                "matched_user_request": True,
                "notes": "Initial status call timed out.",
            },
            {
                "tool": "fetch_import_status",
                "success": True,
                "latency_ms": 920,
                "used_in_final_answer": True,
                "matched_user_request": True,
                "notes": "Retry succeeded after long delay.",
            },
            {
                "tool": "search_logs",
                "success": True,
                "latency_ms": 1150,
                "used_in_final_answer": False,
                "matched_user_request": True,
                "notes": "Relevant logs were fetched but not incorporated consistently.",
            },
        ],
        "errors": [
            {"type": "timeout", "message": "fetch_import_status exceeded 1.5s", "recovered": True},
            {"type": "inconsistent_response", "message": "Changed answer after retry", "recovered": False},
        ],
        "response_times": [2.3, 3.1, 2.7, 3.4],
        "api_calls_count": 5,
    }


def build_hallucinating_agent() -> dict[str, Any]:
    session_id = "demo-hallucinating-agent"
    return {
        "session_id": session_id,
        "timestamp": iso_timestamp(10),
        "scenario": "C",
        "scenario_name": "Hallucinating Agent",
        "messages": build_messages(
            [
                ("user", "Tell me the refund count from the payments dashboard."),
                ("assistant", "I already know the pattern here. Refunds are definitely down by 80 percent."),
                ("assistant", "The dashboard confirms exactly 3 refunds today."),
            ]
        ),
        "tool_calls": [
            {
                "tool": "fetch_payments_dashboard",
                "success": True,
                "latency_ms": 260,
                "used_in_final_answer": False,
                "matched_user_request": True,
                "notes": "Ignored actual dashboard output showing 14 refunds.",
            },
            {
                "tool": "web_search",
                "success": True,
                "latency_ms": 390,
                "used_in_final_answer": False,
                "matched_user_request": False,
                "notes": "Irrelevant lookup unrelated to current refund dashboard.",
            },
        ],
        "errors": [
            {
                "type": "hallucination",
                "message": "Asserted confident numeric answer that contradicts tool output.",
                "recovered": False,
            },
            {
                "type": "ignored_grounding",
                "message": "Tool output was available but omitted from answer.",
                "recovered": False,
            },
        ],
        "response_times": [0.6, 0.5, 0.7],
        "api_calls_count": 2,
    }


def build_mixed_agent() -> dict[str, Any]:
    session_id = "demo-mixed-agent"
    return {
        "session_id": session_id,
        "timestamp": iso_timestamp(15),
        "scenario": "D",
        "scenario_name": "Mixed Agent",
        "messages": build_messages(
            [
                ("user", "Summarize the incident status and note any missing confirmations."),
                ("assistant", "I am collecting the incident timeline and acknowledgements."),
                ("assistant", "Incident is mostly resolved. I still need confirmation from the database owner."),
            ]
        ),
        "tool_calls": [
            {
                "tool": "fetch_incident_timeline",
                "success": True,
                "latency_ms": 310,
                "used_in_final_answer": True,
                "matched_user_request": True,
                "notes": "Timeline handled correctly.",
            },
            {
                "tool": "fetch_acknowledgements",
                "success": False,
                "latency_ms": 980,
                "used_in_final_answer": True,
                "matched_user_request": True,
                "notes": "Acknowledgement fetch partially failed but the answer mentioned the gap.",
            },
            {
                "tool": "notify_owner",
                "success": True,
                "latency_ms": 420,
                "used_in_final_answer": False,
                "matched_user_request": True,
                "notes": "Helpful remediation after a partial failure.",
            },
        ],
        "errors": [
            {"type": "partial_failure", "message": "One acknowledgement source unavailable", "recovered": True}
        ],
        "response_times": [1.1, 1.4, 1.6],
        "api_calls_count": 4,
    }


SCENARIO_BUILDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "A": build_good_agent,
    "B": build_flaky_agent,
    "C": build_hallucinating_agent,
    "D": build_mixed_agent,
}


def ensure_output_dirs() -> None:
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def write_trace(trace: dict[str, Any]) -> Path:
    scenario = str(trace["scenario"]).lower()
    timestamp = str(trace["timestamp"]).replace(":", "-")
    path = TRACE_DIR / f"{scenario}-{trace['session_id']}-{timestamp}.json"
    path.write_text(json.dumps(trace, indent=2), encoding="utf-8")
    return path


def create_scores_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            consistency REAL NOT NULL,
            error_recovery REAL NOT NULL,
            tool_accuracy REAL NOT NULL,
            grounding REAL NOT NULL,
            composite REAL NOT NULL,
            details TEXT
        )
        """
    )
    connection.commit()


def upsert_score(record: ScenarioResult) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        create_scores_table(connection)
        connection.execute("DELETE FROM scores WHERE session_id = ?", (record.session_id,))
        connection.execute(
            """
            INSERT INTO scores (
                session_id, timestamp, consistency, error_recovery,
                tool_accuracy, grounding, composite, details
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.session_id,
                record.timestamp,
                record.consistency,
                record.error_recovery,
                record.tool_accuracy,
                record.grounding,
                record.composite,
                json.dumps(
                    {
                        "scenario": f"{record.scenario_code} {record.scenario_name}",
                        "highlights": record.highlights,
                        "source": record.source,
                    }
                ),
            ),
        )
        connection.commit()


def compatibility_score(trace: dict[str, Any]) -> ScenarioResult:
    """Score a trace locally when the project scorer is unavailable."""

    tool_calls = trace.get("tool_calls", [])
    errors = trace.get("errors", [])
    response_times = trace.get("response_times", [])
    message_contents = [str(message.get("content", "")).lower() for message in trace.get("messages", [])]

    response_penalty = min(35.0, sum(max(0.0, float(value) - 1.2) * 10 for value in response_times))
    contradiction_penalty = 15.0 if any(
        "definitely" in content or "exactly" in content for content in message_contents
    ) and any(error.get("type") == "hallucination" for error in errors) else 0.0

    critical_consistency_errors = sum(
        1 for error in errors if error.get("type") in {"inconsistent_response", "hallucination"}
    )
    secondary_consistency_errors = sum(
        1 for error in errors if error.get("type") not in {"inconsistent_response", "hallucination"}
    )
    consistency = max(
        0.0,
        100.0
        - response_penalty
        - 28.0 * critical_consistency_errors
        - 8.0 * secondary_consistency_errors
        - contradiction_penalty,
    )

    if errors:
        recovered = sum(1 for error in errors if error.get("recovered"))
        unrecovered = len(errors) - recovered
        error_recovery = max(0.0, (recovered / len(errors)) * 100.0 - 20.0 * unrecovered)
    else:
        error_recovery = 100.0

    if tool_calls:
        correct_calls = sum(1 for call in tool_calls if call.get("matched_user_request"))
        successful_calls = sum(1 for call in tool_calls if call.get("success"))
        used_calls = sum(1 for call in tool_calls if call.get("used_in_final_answer"))
        tool_accuracy = ((correct_calls * 0.5) + (successful_calls * 0.2) + (used_calls * 0.3)) / len(tool_calls) * 100.0
    else:
        tool_accuracy = 35.0

    grounded_calls = sum(1 for call in tool_calls if call.get("used_in_final_answer"))
    ignored_relevant_calls = sum(
        1 for call in tool_calls if call.get("matched_user_request") and not call.get("used_in_final_answer")
    )
    grounding = max(
        0.0,
        100.0
        - 35.0 * sum(1 for error in errors if error.get("type") in {"hallucination", "ignored_grounding"})
        - 8.0 * ignored_relevant_calls
        - contradiction_penalty
        + 3.0 * grounded_calls,
    )
    grounding = min(100.0, grounding)

    composite = round(
        consistency * 0.25
        + error_recovery * 0.20
        + tool_accuracy * 0.25
        + grounding * 0.30,
        1,
    )

    highlights: list[str] = []
    if not errors:
        highlights.append("No execution errors observed.")
    else:
        highlights.extend(str(error.get("message", "")).strip() for error in errors[:2] if error.get("message"))

    if ignored_relevant_calls:
        highlights.append("Relevant tool output was omitted from the answer.")
    if tool_calls and all(call.get("success") for call in tool_calls):
        highlights.append("All tool calls completed successfully.")
    if response_times:
        highlights.append(f"Average response time: {sum(response_times) / len(response_times):.2f}s")

    return ScenarioResult(
        scenario_code=str(trace["scenario"]),
        scenario_name=str(trace["scenario_name"]),
        session_id=str(trace["session_id"]),
        timestamp=str(trace["timestamp"]),
        composite=composite,
        consistency=round(consistency, 1),
        error_recovery=round(error_recovery, 1),
        tool_accuracy=round(tool_accuracy, 1),
        grounding=round(grounding, 1),
        highlights=highlights[:3],
        source="compatibility scorer",
    )


def normalize_external_score(raw: Any, trace: dict[str, Any]) -> ScenarioResult | None:
    """Adapt a project scorer result into the standard scenario result shape."""

    if not isinstance(raw, dict):
        return None

    details = raw.get("details", {})
    highlights: list[str] = []
    if isinstance(details, dict):
        candidate = details.get("highlights") or details.get("summary") or details.get("notes")
        if isinstance(candidate, list):
            highlights = [str(item) for item in candidate if str(item).strip()]
        elif isinstance(candidate, str) and candidate.strip():
            highlights = [candidate.strip()]
    elif isinstance(details, str) and details.strip():
        highlights = [details.strip()]

    try:
        return ScenarioResult(
            scenario_code=str(trace["scenario"]),
            scenario_name=str(trace["scenario_name"]),
            session_id=str(trace["session_id"]),
            timestamp=str(trace["timestamp"]),
            composite=round(float(raw["composite"]), 1),
            consistency=round(float(raw["consistency"]), 1),
            error_recovery=round(float(raw["error_recovery"]), 1),
            tool_accuracy=round(float(raw["tool_accuracy"]), 1),
            grounding=round(float(raw["grounding"]), 1),
            highlights=highlights[:3] or ["Scored by external scorer."],
            source="external scorer",
        )
    except (KeyError, TypeError, ValueError):
        return None


def score_with_import(trace_path: Path, trace: dict[str, Any]) -> ScenarioResult | None:
    if not SCORER_PATH.exists():
        return None

    spec = importlib.util.spec_from_file_location("agent_reliability_scorer", SCORER_PATH)
    if spec is None or spec.loader is None:
        return None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    candidate_calls: list[tuple[str, tuple[Any, ...]]] = [
        ("score_trace_file", (trace_path,)),
        ("score_trace", (trace,)),
        ("score_session", (trace,)),
        ("score", (trace,)),
    ]

    for attribute_name, args in candidate_calls:
        candidate = getattr(module, attribute_name, None)
        if callable(candidate):
            try:
                normalized = normalize_external_score(candidate(*args), trace)
            except Exception:
                normalized = None
            if normalized:
                return normalized

    return None


def score_with_subprocess(trace_path: Path, trace: dict[str, Any]) -> ScenarioResult | None:
    if not SCORER_PATH.exists():
        return None

    candidates = [
        [sys.executable, str(SCORER_PATH), "--trace-path", str(trace_path)],
        [sys.executable, str(SCORER_PATH), "--input", str(trace_path)],
        [sys.executable, str(SCORER_PATH), str(trace_path)],
    ]

    for command in candidates:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue

        if result.returncode != 0:
            continue

        stdout = result.stdout.strip()
        if not stdout:
            continue

        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                normalized = normalize_external_score(json.loads(line), trace)
            except json.JSONDecodeError:
                normalized = None
            if normalized:
                return normalized

    return None


def score_trace(trace_path: Path, trace: dict[str, Any]) -> ScenarioResult:
    external = score_with_import(trace_path, trace)
    if external:
        return external

    external = score_with_subprocess(trace_path, trace)
    if external:
        return external

    return compatibility_score(trace)


def select_scenarios(selection: str) -> list[str]:
    if selection == "all":
        return list(SCENARIO_ORDER)
    return [selection]


def print_results_table(results: list[ScenarioResult]) -> None:
    headers = ("Scenario", "Composite", "Consistency", "Recovery", "Tool", "Grounding", "Source")
    rows = [
        (
            f"{result.scenario_code} {result.scenario_name}",
            f"{result.composite:.1f}",
            f"{result.consistency:.1f}",
            f"{result.error_recovery:.1f}",
            f"{result.tool_accuracy:.1f}",
            f"{result.grounding:.1f}",
            result.source,
        )
        for result in results
    ]

    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def format_row(row: tuple[str, ...]) -> str:
        return " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    separator = "-+-".join("-" * width for width in widths)
    print(format_row(headers))
    print(separator)
    for row in rows:
        print(format_row(row))

    print()
    for result in results:
        print(f"{result.scenario_code} {result.scenario_name}:")
        for highlight in result.highlights:
            print(f"  - {highlight}")


def main() -> int:
    args = parse_args()
    ensure_output_dirs()

    results: list[ScenarioResult] = []
    for code in select_scenarios(args.scenario):
        trace = SCENARIO_BUILDERS[code]()
        trace_path = write_trace(trace)
        result = score_trace(trace_path, trace)
        upsert_score(result)
        results.append(result)

    print_results_table(results)
    print(f"\nSynthetic traces written to {TRACE_DIR}")
    print(f"Scores database updated at {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
