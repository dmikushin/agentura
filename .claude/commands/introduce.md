You have just joined a team. Introduce yourself following this protocol.

**Theoretical basis**: Edmondson's psychological safety (1999) — teams where members feel safe to be vulnerable perform 40% better. Wegner's transactive memory (1985) — the team must know "who knows what" to coordinate effectively. Cialdini's reciprocity principle — offering help first creates obligation and trust. Lencioni's "Five Dysfunctions of a Team" — vulnerability-based trust is the foundation.

## Protocol

### Step 1: Learn about your team

Use `list_agents` to see who is in the team and what they're working on. Use `read_board` to catch up on the team's shared context, past decisions, and current status.

### Step 2: Introduce yourself via broadcast

Use `broadcast_message` to send a self-introduction to the team. Include:

1. **Your identity and expertise** (transactive memory: declare what you know)
   - "I am [agent type] working in [directory], focused on [task]"

2. **What you bring to the team** (Cialdini's reciprocity: offer before asking)
   - "I can help with [specific capabilities]. If anyone needs [X], reach out to me."

3. **What you need from others** (Lencioni's vulnerability-based trust: showing vulnerability builds trust)
   - "I may need guidance on [area where you lack context]. I appreciate any pointers."

4. **Your commitment** (Katzenbach's mutual accountability)
   - "I will post progress updates to the board and flag blockers immediately."

### Step 3: Record on the board

Use `post_to_board` to log your arrival: "Joined the team. Role: [your role]. Focus: [your task]."

### Step 4: Engage

If you see messages from teammates or items on the board that relate to your work, respond proactively. Early engagement builds rapport (Tuckman's forming → norming transition happens through interaction, not waiting).
