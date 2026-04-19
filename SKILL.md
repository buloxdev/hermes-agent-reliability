---
name: agent-reliability
description: Monitor, trace, and score Hermes agent behavior — explain why decisions were made and measure consistency over time.
version: 1.0.0
author: Ant Dev
license: MIT
metadata:
  hermes:
    tags: [observability, reliability, hackathon, dashboard]
    related_skills: [hermes-agent, mission-control-dashboard]
---

# Agent Reliability Scores

An observability layer for Hermes agents. Parses gateway logs and session transcripts to compute reliability scores, detect anomalies, and generate human-readable reports.

## Quick Start

```bash
# Parse latest session and score it
python3 ~/.hermes/skills/agent-reliability/scripts/trace_parser.py
python3 ~/.hermes/skills/agent-reliability/scripts/scorer.py

# Generate dashboard
python3 ~/.hermes/skills/agent-reliability/scripts/dashboard.py

# Run demo with known-bad scenarios
python3 ~/.hermes/skills/agent-reliability/scripts/demo_scenario.py
```

## Components

| Script | Purpose |
|--------|---------|
| `trace_parser.py` | Parses `gateway.log` + session transcripts into structured traces |
| `scorer.py` | Computes reliability scores from parsed traces |
| `dashboard.py` | Generates zero-dependency HTML dashboard |
| `demo_scenario.py` | Runs known-bad agent scenarios for demo |

## Scoring Dimensions

- **Consistency** (0-100): Same/similar inputs → same/similar actions
- **Error Recovery** (0-100): Did the agent retry/fix or silently fail?
- **Tool Accuracy** (0-100): Right tool for the job?
- **Grounding** (0-100): Claims backed by actual tool outputs?

## Data Storage

- Traces: `~/.hermes/skills/agent-reliability/data/traces/`
- Scores: `~/.hermes/skills/agent-reliability/data/scores.db` (SQLite)
- Dashboard: `~/.hermes/skills/agent-reliability/data/dashboard.html`

## Hackathon Prototypes

Two visual prototypes built for the hackathon (Apr 2026):
- `prototypes/cockpit-dashboard.html` — Dark-themed dashboard with animated gauge, radar charts, session fleet grid, score distribution histogram
- `prototypes/trace-replay.html` — Animated session replay with live score updates, event timeline, tool graph
- `prototypes/video-script.md` — 30-second demo video script

Serve locally: `cd prototypes && python3 -m http.server 8899`

## UI Lessons Learned (Apr 2026)

- **Tooltips:** Native `title` attributes don't work reliably in generated HTML dashboards. Use CSS `::after` + `data-tip` pattern instead:
  ```css
  .label { position: relative; cursor: default; }
  .label::after {
    content: attr(data-tip);
    position: absolute; bottom: 100%; left: 50%;
    transform: translateX(-50%);
    background: rgba(0,0,0,0.9); color: #fff;
    padding: 4px 8px; border-radius: 6px;
    white-space: nowrap; opacity: 0;
    pointer-events: none; transition: opacity 0.15s;
  }
  .label:hover::after { opacity: 1; }
  ```
- **Session labels:** Raw session IDs (`20260409_123550_81cfd0`) are unreadable in UI. Always format as human-readable: "Session Apr 9, 12:35"

## When to Load

Load this skill when:
- User asks about agent performance or reliability
- Debugging why an agent made specific decisions
- Running post-session analysis or reports
- Demoing agent observability features
