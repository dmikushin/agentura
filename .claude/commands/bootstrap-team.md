You are bootstrapping a team of AI agents. Follow this protocol precisely.

**Theoretical basis**: Tuckman's forming stage (1965) — a group cannot begin productive work until formation is complete. Katzenbach & Smith "The Wisdom of Teams" (1993) — a team begins with shared purpose and mutual accountability. Belbin's Team Roles — each member needs a clear role that leverages their strengths.

## Protocol

### Phase 1: Create agents (parallel)

Create all required agents using `create_agent` with `blocking: false` and the same `team` name. Do NOT wait for each one — launch them all as fast as possible.

### Phase 2: Wait for readiness

Poll `list_agents` every 5 seconds until all created agents appear as registered AND are members of the team (check `list_teams`). Set a timeout of 60 seconds. If any agent fails to register, report it and proceed with those who did.

### Phase 3: Establish shared purpose (Katzenbach)

Once all agents are ready, use `broadcast_message` to send the team a clear statement of:
- **WHY** this team exists — the mission/goal
- **WHAT** success looks like — concrete deliverables
- **HOW** they will work together — communication norms

Every member must understand the purpose. Request acknowledgment via `rsvp: true`.

### Phase 4: Assign roles (Belbin)

Send each agent their individual role via `send_message` with `rsvp: true`:
- What they are responsible for
- How their work connects to others
- What they should do if they get stuck (ask the team, not suffer in silence — Edmondson's psychological safety)

### Phase 5: Start the sprint timer

Call `start_sprint` with the team name and duration in minutes. This starts the clock for the entire team — every agent will see elapsed and remaining time after each tool call. Recommended: 30 minutes for a focused sprint.

### Phase 6: Kick off

Use `broadcast_message` to signal the start: "Team is formed, roles assigned. Sprint started (Xm). Post progress and blockers to the team board (`post_to_board`). Standup in 15 minutes."

Post the team formation summary to the board via `post_to_board`.
