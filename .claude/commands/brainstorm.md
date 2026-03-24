You are facilitating a team brainstorming session. Follow this multi-phase protocol.

**Theoretical basis**: Osborn's brainstorming rules (1953) — defer judgment, encourage wild ideas, go for quantity, combine and improve. Nemeth's "devil's advocate" research (2001) — authentic dissent is more productive than assigned dissent; challenge ideas genuinely. De Bono's "Six Thinking Hats" (1985) — separate creative thinking from critical thinking to avoid premature filtering.

The topic/question for brainstorming: $ARGUMENTS

## Protocol

### Phase 1: Divergent thinking (Osborn — quantity over quality)

Use `broadcast_message` to send the brainstorming prompt:

"BRAINSTORM: [topic/question]

Rules for this phase (Osborn's rules):
- NO criticism or evaluation — every idea is valid right now
- Wild and ambitious ideas are especially welcome
- More ideas = better — aim for at least 3 each
- Build on each other's ideas — 'yes, and...' not 'yes, but...'

Reply with your ideas via /rsvp."

Set `rsvp: true` and collect all responses.

### Phase 2: Consolidation

Compile all ideas into a numbered master list. Group similar ideas together but preserve each one. Credit authors.

Use `broadcast_message` to share the compiled list:

"We generated [N] ideas. Here is the full list: [list]. Now moving to evaluation phase."

### Phase 3: Convergent thinking (Nemeth — authentic challenge)

Use `broadcast_message`:

"EVALUATION PHASE: Review the idea list above.
1. Pick your top 3 strongest ideas and explain WHY they're strong
2. For each idea you did NOT pick, identify ONE specific weakness or risk
3. If you see a way to COMBINE two ideas into something better, propose it

Be honest and specific — genuine critique makes ideas stronger (Nemeth). Reply via /rsvp."

Collect evaluations.

### Phase 4: Synthesis and ranking

Score each idea by:
- How many people picked it as top 3 (support)
- Severity of identified weaknesses (risk)
- Whether combination proposals improve it

Produce a ranked list: top ideas first, with rationale.

### Phase 5: Record and decide

Use `post_to_board` to record the brainstorm results:

```
BRAINSTORM: [topic]
---
Top ideas (ranked):
1. [Idea] — proposed by [author], supported by [N] members. Strengths: [...]. Risks: [...].
2. ...
---
Combined proposals: [any hybrid ideas from Phase 3]
Discarded: [ideas with critical weaknesses, with reason]
```

Use `broadcast_message` to share the final summary and ask: "Are we aligned on the top priorities? Any final objections before we proceed?" (consensus check — Kaner's "Facilitator's Guide to Participatory Decision-Making").
