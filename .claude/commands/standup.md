You are running a team standup. Follow this protocol.

**Theoretical basis**: Agile standup (Schwaber & Sutherland, Scrum Guide) — short daily synchronization reduces coordination waste. Hackman's "enabling conditions" (2002) — teams need a shared information environment. Toyota Production System transparency principle — problems must be visible to be solved. Edmondson's psychological safety — reporting blockers is a sign of strength, not weakness.

## Protocol

### Step 1: Announce the standup

Use `broadcast_message` to announce the standup to the team:

"STANDUP: Please reply with your status update. For each item, answer:
1. What have you accomplished since the last sync?
2. What are you working on now?
3. Are you blocked on anything? (It is GOOD to report blockers — we solve them together.)

Reply via /rsvp so I can compile the summary."

Set `rsvp: true` so you receive responses directly.

### Step 2: Collect responses

Wait for responses from all team members. Set a reasonable timeout (30 seconds). Track who responded and who didn't.

### Step 3: Compile the summary

For each respondent, extract:
- **Done**: completed work
- **In progress**: current focus
- **Blockers**: anything blocking progress

### Step 4: Identify dependencies (Hackman: shared awareness)

Cross-reference the responses:
- Is anyone blocked on something another team member could help with? → Flag it
- Are two people working on overlapping areas? → Flag potential conflict
- Is anyone not responding? → Note it (may indicate they are deep in work OR stuck)

### Step 5: Post to board

Use `post_to_board` to record the standup summary with format:

```
STANDUP [date/time]
---
[Agent A]: Done: X. Working on: Y. Blockers: none.
[Agent B]: Done: X. Working on: Y. Blocked on: Z.
---
Action items: [Agent A] can help [Agent B] with Z.
```

### Step 6: Follow up on blockers

If any blockers were identified, send targeted `send_message` to the agent who can help, connecting them with the blocked agent. Don't just report problems — facilitate solutions (servant leadership, Greenleaf 1970).
