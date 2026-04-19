# Agent Reliability — Demo Video Script (30 sec)

## Concept
"Can you trust your AI agent?" — Show the problem, show the tool, show the insight.

## Shot List

### Scene 1 — The Hook (0:00 – 0:04)
**Screen:** Black with white text, fades in
**Text:** "Can you trust your AI agent?"
**Style:** Clean, centered, slight fade-in
**Audio:** (optional) subtle tech ambient

### Scene 2 — The Bad Agent (0:04 – 0:14)
**Screen:** Trace Replay dashboard → switch to "Hallucinating Agent" → hit Play
**What happens live:**
- User asks: "Tell me the refund count from the payments dashboard"
- Agent responds: "Refunds are definitely down by 80 percent" ← WRONG
- Tool call fires (fetch_payments_dashboard) — actual answer was 14 refunds
- Agent ignores the data, repeats wrong answer
- **Score drops from 100 → 26 in real-time**
- Grounding score crashes to 7 (flashes red)

**Key moment:** The score counter animating downward — visceral, memorable

### Scene 3 — The Good Agent (0:14 – 0:20)
**Screen:** Reset → switch to "Good Agent" → hit Play
**What happens live:**
- Agent checks deployment status
- Uses two tools correctly, cross-references data
- Score stays at 100 — all green

**Contrast with Scene 2:** Same tool, completely different outcome

### Scene 4 — The Overview (0:20 – 0:26)
**Screen:** Switch to Cockpit Dashboard
**What shows:**
- Radar charts comparing all 4 scenarios side-by-side
- Session fleet grid with color-coded scores
- Score distribution histogram
- Composite gauge at 63.4 (fleet average)

**This is the "wow" shot** — all the data at a glance

### Scene 5 — End Card (0:26 – 0:30)
**Screen:** Fade to dark with text
**Text:**
```
Agent Reliability Scores
The credit score for AI agents.

github.com/[repo-link]
```

## Recording Tips

1. **Use the Trace Replay page:** http://localhost:8899/trace-replay.html
2. **Use the Cockpit page:** http://localhost:8899/cockpit-dashboard.html
3. **Record at 1080p** — clean browser window, no bookmarks bar
4. **Use browser zoom (Cmd+/-)** to fit the page nicely
5. **Mouse movements matter** — smooth, intentional clicks, no random hovering
6. **Speed:** Let the animations play, don't rush. The score dropping IS the money shot.

## Optional Enhancements
- Add text overlays in post (e.g., "Score: 100" → "Score: 26 💀")
- Add subtle background music (dark tech/cinematic)
- Add a voiceover (I can generate TTS if you want)
- Speed up the boring parts, slow down the score drops
