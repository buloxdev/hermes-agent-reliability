#!/usr/bin/env python3
"""Parse Hermes gateway logs and session transcripts into structured traces.

The parser is intentionally defensive because Hermes emits a mix of:
- timestamped gateway log lines
- inline progress lines without timestamps (for example: [tool], [done], ┊ ...)
- session transcript files in either JSON or JSONL format

Output is a single JSON file containing normalized session records that can be
scored later by scripts/scorer.py.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, pvariance
from typing import Any


DEFAULT_LOG_PATH = Path("~/.hermes/logs/gateway.log").expanduser()
DEFAULT_SESSIONS_DIR = Path("~/.hermes/sessions").expanduser()
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "traces"
JSONL_SESSION_STEM_RE = re.compile(r"^\d{8}_\d{6}_[a-zA-Z0-9]+$")

TIMESTAMP_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d+)? "
    r"(?P<level>[A-Z]+) (?P<logger>[^:]+): (?P<message>.*)$"
)
INBOUND_RE = re.compile(
    r"""inbound message:\s+
    platform=(?P<platform>\S+)\s+
    user=(?P<user>.+?)\s+
    chat=(?P<chat>\S+)\s+
    msg=(?P<quote>["']).*(?P=quote)$
    """,
    re.VERBOSE,
)
RESPONSE_RE = re.compile(
    r"response ready:\s+platform=(?P<platform>\S+)\s+chat=(?P<chat>\S+)\s+"
    r"time=(?P<time>[0-9.]+)s\s+api_calls=(?P<api_calls>\d+)\s+response=(?P<chars>\d+)\s+chars"
)
TIMEOUT_RE = re.compile(r"No response from provider for (?P<seconds>\d+)s")
TOOL_PROGRESS_RE = re.compile(r"^\[(?P<kind>tool|done)\]\s+(?P<body>.*)$")
TOOL_NAME_RE = re.compile(r"[┊ ](?P<tool>[a-zA-Z0-9_.:-]+)\s{2,}")
DATA_POINT_RE = re.compile(
    r"\b(?:\d{1,3}(?:,\d{3})+|\d+\.\d+|\d+%|\d{4}-\d{2}-\d{2}|[A-Z]{2,10}-\d+)\b"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH), help="Path to gateway.log")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for parsed trace JSON output",
    )
    return parser.parse_args()


def isoformat(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def safe_mean(values: list[float]) -> float:
    return mean(values) if values else 0.0


def safe_variance(values: list[float]) -> float:
    return pvariance(values) if len(values) > 1 else 0.0


def normalize_error_text(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"\b\d+(?:\.\d+)?s\b", "<duration>", text)
    text = re.sub(r"\b\d+\b", "<num>", text)
    text = re.sub(r"\s+", " ", text)
    return text


def looks_like_tool_error(content: str) -> bool:
    lowered = content.lower()
    return any(
        token in lowered
        for token in (
            '"error"',
            "error:",
            "traceback",
            "exception",
            "timed out",
            "timeout",
            "failed",
            "not found",
        )
    )


@dataclass
class SessionAccumulator:
    session_id: str
    platform: str | None = None
    chat_id: str | None = None
    user: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    source_files: set[str] = field(default_factory=set)
    gateway_events: list[dict[str, Any]] = field(default_factory=list)
    transcript_messages: list[dict[str, Any]] = field(default_factory=list)
    response_times: list[float] = field(default_factory=list)
    response_api_calls: list[int] = field(default_factory=list)
    response_chars: list[int] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    timeouts: list[dict[str, Any]] = field(default_factory=list)
    restarts: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    specific_data_points: set[str] = field(default_factory=set)
    unmatched_inbound: list[dict[str, Any]] = field(default_factory=list)

    def touch_time(self, when: datetime | None) -> None:
        if when is None:
            return
        if self.start_time is None or when < self.start_time:
            self.start_time = when
        if self.end_time is None or when > self.end_time:
            self.end_time = when

    def add_data_points(self, text: str) -> None:
        for match in DATA_POINT_RE.findall(text or ""):
            self.specific_data_points.add(match)

    def to_dict(self) -> dict[str, Any]:
        tool_output_chars = sum(item.get("output_chars", 0) for item in self.tool_results)
        repeated_errors = defaultdict(int)
        for error in self.errors:
            repeated_errors[normalize_error_text(error["message"])] += 1

        transcript_role_counts = defaultdict(int)
        for message in self.transcript_messages:
            transcript_role_counts[message.get("role", "unknown")] += 1

        total_messages = max(
            len([e for e in self.gateway_events if e["type"] == "inbound_message"]),
            transcript_role_counts["user"],
        )

        metrics = {
            "total_messages": total_messages,
            "avg_response_time": safe_mean(self.response_times),
            "response_time_variance": safe_variance(self.response_times),
            "error_count": len(self.errors),
            "timeout_count": len(self.timeouts),
            "restart_count": len(self.restarts),
            "api_calls_total": sum(self.response_api_calls),
            "api_calls_avg": safe_mean([float(v) for v in self.response_api_calls]),
            "response_count": len(self.response_times),
            "response_chars_total": sum(self.response_chars),
            "response_chars_avg": safe_mean([float(v) for v in self.response_chars]),
            "tool_calls_total": len(self.tool_calls),
            "tool_call_successes": sum(1 for result in self.tool_results if result.get("success")),
            "tool_call_failures": sum(1 for result in self.tool_results if not result.get("success")),
            "tool_output_chars_total": tool_output_chars,
            "transcript_message_count": len(self.transcript_messages),
            "transcript_user_messages": transcript_role_counts["user"],
            "transcript_assistant_messages": transcript_role_counts["assistant"],
            "transcript_tool_messages": transcript_role_counts["tool"],
            "specific_data_points_count": len(self.specific_data_points),
            "repeated_error_patterns": {k: v for k, v in repeated_errors.items() if v > 1},
        }

        return {
            "session_id": self.session_id,
            "platform": self.platform,
            "chat_id": self.chat_id,
            "user": self.user,
            "start_time": isoformat(self.start_time),
            "end_time": isoformat(self.end_time),
            "source_files": sorted(self.source_files),
            "gateway_events": self.gateway_events,
            "transcript_messages": self.transcript_messages,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "metrics": metrics,
        }


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def extract_message_value(raw_line: str, field: str = "msg") -> str | None:
    marker = f"{field}="
    idx = raw_line.find(marker)
    if idx == -1:
        return None
    value = raw_line[idx + len(marker) :].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def select_session_for_gateway_event(
    sessions: dict[str, SessionAccumulator],
    conversation_sessions: dict[tuple[str, str], list[str]],
    platform: str,
    chat_id: str,
    when: datetime,
    user: str | None = None,
) -> SessionAccumulator:
    key = (platform, chat_id)
    candidates = conversation_sessions[key]

    # Reuse the most recent conversation if activity is close in time.
    for session_id in reversed(candidates):
        session = sessions[session_id]
        if session.end_time and when - session.end_time <= timedelta(minutes=30):
            return session

    synthetic_id = f"gateway_{platform}_{chat_id}_{when.strftime('%Y%m%d_%H%M%S')}"
    session = SessionAccumulator(
        session_id=synthetic_id,
        platform=platform,
        chat_id=chat_id,
        user=user,
    )
    sessions[synthetic_id] = session
    conversation_sessions[key].append(synthetic_id)
    return session


def parse_gateway_log(log_path: Path, sessions: dict[str, SessionAccumulator]) -> list[str]:
    warnings: list[str] = []
    if not log_path.exists():
        warnings.append(f"Gateway log missing: {log_path}")
        return warnings

    conversation_sessions: dict[tuple[str, str], list[str]] = defaultdict(list)
    last_timestamped_session_id: str | None = None

    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            match = TIMESTAMP_RE.match(line)

            if match:
                when = parse_timestamp(match.group("timestamp"))
                level = match.group("level")
                message = match.group("message")
                logger = match.group("logger")

                inbound_match = INBOUND_RE.match(message)
                if inbound_match:
                    platform = inbound_match.group("platform")
                    chat_id = inbound_match.group("chat")
                    user = inbound_match.group("user")
                    content = extract_message_value(message, "msg") or ""
                    session = select_session_for_gateway_event(
                        sessions, conversation_sessions, platform, chat_id, when, user=user
                    )
                    session.platform = platform
                    session.chat_id = chat_id
                    session.user = user
                    session.source_files.add(str(log_path))
                    session.touch_time(when)
                    event = {
                        "type": "inbound_message",
                        "timestamp": isoformat(when),
                        "content": content,
                        "logger": logger,
                    }
                    session.gateway_events.append(event)
                    session.unmatched_inbound.append(event)
                    session.add_data_points(content)
                    last_timestamped_session_id = session.session_id
                    continue

                response_match = RESPONSE_RE.match(message)
                if response_match:
                    platform = response_match.group("platform")
                    chat_id = response_match.group("chat")
                    session = select_session_for_gateway_event(
                        sessions, conversation_sessions, platform, chat_id, when
                    )
                    response_time = float(response_match.group("time"))
                    api_calls = int(response_match.group("api_calls"))
                    chars = int(response_match.group("chars"))
                    session.source_files.add(str(log_path))
                    session.touch_time(when)
                    session.response_times.append(response_time)
                    session.response_api_calls.append(api_calls)
                    session.response_chars.append(chars)
                    session.gateway_events.append(
                        {
                            "type": "response_ready",
                            "timestamp": isoformat(when),
                            "response_time_seconds": response_time,
                            "api_calls": api_calls,
                            "response_chars": chars,
                            "matched_inbound": bool(session.unmatched_inbound),
                        }
                    )
                    if session.unmatched_inbound:
                        session.unmatched_inbound.pop(0)
                    last_timestamped_session_id = session.session_id
                    continue

                if "starting hermes gateway" in message.lower() or "gateway restarted successfully" in message.lower():
                    session_id = last_timestamped_session_id or "gateway_system"
                    session = sessions.setdefault(session_id, SessionAccumulator(session_id=session_id))
                    session.source_files.add(str(log_path))
                    session.touch_time(when)
                    session.restarts.append(
                        {"timestamp": isoformat(when), "message": message, "level": level}
                    )
                    session.gateway_events.append(
                        {
                            "type": "restart",
                            "timestamp": isoformat(when),
                            "message": message,
                            "level": level,
                        }
                    )
                    continue

                timeout_match = TIMEOUT_RE.search(message)
                if timeout_match:
                    session_id = last_timestamped_session_id or "gateway_system"
                    session = sessions.setdefault(session_id, SessionAccumulator(session_id=session_id))
                    seconds = int(timeout_match.group("seconds"))
                    session.source_files.add(str(log_path))
                    session.touch_time(when)
                    session.timeouts.append(
                        {
                            "timestamp": isoformat(when),
                            "duration_seconds": seconds,
                            "message": message,
                        }
                    )
                    session.gateway_events.append(
                        {
                            "type": "timeout",
                            "timestamp": isoformat(when),
                            "duration_seconds": seconds,
                            "message": message,
                        }
                    )
                    continue

                if level in {"ERROR", "WARNING"} or any(
                    token in message.lower() for token in ("interrupted", "discarding command", "traceback")
                ):
                    session_id = last_timestamped_session_id or "gateway_system"
                    session = sessions.setdefault(session_id, SessionAccumulator(session_id=session_id))
                    session.source_files.add(str(log_path))
                    session.touch_time(when)
                    session.errors.append(
                        {
                            "timestamp": isoformat(when),
                            "message": message,
                            "level": level,
                        }
                    )
                    session.gateway_events.append(
                        {
                            "type": "error",
                            "timestamp": isoformat(when),
                            "message": message,
                            "level": level,
                        }
                    )
                    continue

                if last_timestamped_session_id:
                    session = sessions[last_timestamped_session_id]
                    session.touch_time(when)
                continue

            # Non-timestamped progress lines are attached to the most recent active session.
            if not last_timestamped_session_id:
                continue

            session = sessions[last_timestamped_session_id]
            tool_match = TOOL_PROGRESS_RE.match(line.strip())
            if tool_match:
                body = tool_match.group("body")
                tool_name_match = TOOL_NAME_RE.search(body)
                tool_name = tool_name_match.group("tool") if tool_name_match else body.split()[0] if body else "unknown"
                success = tool_match.group("kind") == "done"
                session.gateway_events.append(
                    {
                        "type": "tool_progress",
                        "status": tool_match.group("kind"),
                        "tool_name": tool_name,
                        "body": body,
                    }
                )
                if success:
                    session.tool_results.append(
                        {
                            "tool_name": tool_name,
                            "success": True,
                            "output_chars": len(body),
                            "source": "gateway_progress",
                        }
                    )
                else:
                    session.tool_calls.append(
                        {
                            "tool_name": tool_name,
                            "arguments": None,
                            "source": "gateway_progress",
                        }
                    )
                continue

            lowered = line.lower()
            if "interrupted during api call" in lowered:
                session.errors.append({"timestamp": None, "message": line, "level": "ERROR"})
                session.gateway_events.append({"type": "error", "timestamp": None, "message": line, "level": "ERROR"})

    return warnings


def iter_session_files(sessions_dir: Path) -> list[Path]:
    if not sessions_dir.exists():
        return []
    files: list[Path] = []
    for path in sessions_dir.iterdir():
        if not path.is_file():
            continue
        if path.suffix == ".json" and path.stem.startswith("session_"):
            files.append(path)
            continue
        if path.suffix == ".jsonl" and JSONL_SESSION_STEM_RE.match(path.stem):
            files.append(path)
    return sorted(files)


def normalize_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    fn = tool_call.get("function") or {}
    return {
        "tool_name": fn.get("name") or tool_call.get("name") or "unknown",
        "arguments": fn.get("arguments"),
        "call_id": tool_call.get("call_id") or tool_call.get("id"),
        "source": "transcript",
    }


def parse_session_transcript(path: Path, sessions: dict[str, SessionAccumulator]) -> str | None:
    try:
        if path.suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            session_id = payload.get("session_id") or path.stem
            session = sessions.setdefault(session_id, SessionAccumulator(session_id=session_id))
            session.source_files.add(str(path))
            session.platform = session.platform or payload.get("platform")
            session.touch_time(parse_timestamp(payload.get("session_start")))
            session.touch_time(parse_timestamp(payload.get("last_updated")))

            for message in payload.get("messages", []):
                ingest_transcript_message(session, message)

            return None

        # JSONL transcripts are one message per line.
        session_id = path.stem
        session = sessions.setdefault(session_id, SessionAccumulator(session_id=session_id))
        session.source_files.add(str(path))
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                ingest_transcript_message(session, json.loads(raw_line))
        return None
    except Exception as exc:  # pragma: no cover - defensive, not a logic branch
        return f"Failed to parse session transcript {path}: {exc}"


def ingest_transcript_message(session: SessionAccumulator, message: dict[str, Any]) -> None:
    role = message.get("role", "unknown")
    timestamp = parse_timestamp(message.get("timestamp"))
    content = message.get("content")

    session.touch_time(timestamp)
    if message.get("platform") and not session.platform:
        session.platform = message["platform"]

    normalized = {
        "role": role,
        "timestamp": isoformat(timestamp),
        "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
    }
    session.transcript_messages.append(normalized)
    session.add_data_points(normalized["content"] or "")

    if role == "assistant":
        for tool_call in message.get("tool_calls", []) or []:
            session.tool_calls.append(normalize_tool_call(tool_call))
    elif role == "tool":
        text = normalized["content"] or ""
        session.tool_results.append(
            {
                "tool_name": infer_tool_name_from_tool_output(message, text),
                "success": not looks_like_tool_error(text),
                "output_chars": len(text),
                "call_id": message.get("tool_call_id"),
                "source": "transcript",
            }
        )


def infer_tool_name_from_tool_output(message: dict[str, Any], content: str) -> str:
    if message.get("tool_name"):
        return str(message["tool_name"])

    try:
        payload = json.loads(content)
        if isinstance(payload, dict):
            for field in ("tool", "name", "command"):
                if field in payload and isinstance(payload[field], str):
                    return payload[field]
    except Exception:
        pass

    return "unknown"


def correlate_transcripts_with_gateway(sessions: dict[str, SessionAccumulator]) -> None:
    transcript_sessions = [s for s in sessions.values() if any("session" in Path(f).name or f.endswith(".jsonl") for f in s.source_files)]
    gateway_sessions = [s for s in sessions.values() if any(Path(f).name == "gateway.log" for f in s.source_files)]

    for transcript in transcript_sessions:
        if any(event["type"] == "inbound_message" for event in transcript.gateway_events):
            continue

        best_match: SessionAccumulator | None = None
        best_delta: float | None = None
        for gateway in gateway_sessions:
            if transcript.platform and gateway.platform and transcript.platform != gateway.platform:
                continue
            if not transcript.start_time or not gateway.start_time:
                continue
            delta = abs((gateway.start_time - transcript.start_time).total_seconds())
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_match = gateway

        if best_match and best_delta is not None and best_delta <= 15 * 60:
            transcript.gateway_events.extend(best_match.gateway_events)
            transcript.errors.extend(best_match.errors)
            transcript.timeouts.extend(best_match.timeouts)
            transcript.restarts.extend(best_match.restarts)
            transcript.response_times.extend(best_match.response_times)
            transcript.response_api_calls.extend(best_match.response_api_calls)
            transcript.response_chars.extend(best_match.response_chars)
            if not transcript.chat_id:
                transcript.chat_id = best_match.chat_id
            if not transcript.user:
                transcript.user = best_match.user
            transcript.source_files.update(best_match.source_files)


def build_output(sessions: dict[str, SessionAccumulator], warnings: list[str], log_path: Path) -> dict[str, Any]:
    session_records = [
        session.to_dict()
        for session in sorted(
            sessions.values(),
            key=lambda item: item.start_time or datetime.min,
        )
    ]
    return {
        "generated_at": datetime.now().isoformat(),
        "source": {
            "log_path": str(log_path),
            "sessions_dir": str(DEFAULT_SESSIONS_DIR),
        },
        "warnings": warnings,
        "summary": {
            "session_count": len(session_records),
            "total_messages": sum(item["metrics"]["total_messages"] for item in session_records),
            "total_errors": sum(item["metrics"]["error_count"] for item in session_records),
            "total_timeouts": sum(item["metrics"]["timeout_count"] for item in session_records),
        },
        "sessions": session_records,
    }


def main() -> int:
    args = parse_args()
    log_path = Path(args.log_path).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    sessions: dict[str, SessionAccumulator] = {}
    warnings: list[str] = []

    warnings.extend(parse_gateway_log(log_path, sessions))

    session_files = iter_session_files(DEFAULT_SESSIONS_DIR)
    if not session_files:
        warnings.append(f"Session transcript directory missing or empty: {DEFAULT_SESSIONS_DIR}")

    for session_file in session_files:
        warning = parse_session_transcript(session_file, sessions)
        if warning:
            warnings.append(warning)

    correlate_transcripts_with_gateway(sessions)

    payload = build_output(sessions, warnings, log_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"{timestamp}.json"
    output_path.write_text(json_dumps(payload), encoding="utf-8")

    print(
        json.dumps(
            {
                "output_path": str(output_path),
                "session_count": payload["summary"]["session_count"],
                "warnings": len(warnings),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
