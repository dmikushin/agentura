The agent **$ARGUMENTS** has sent you a message and is waiting for your immediate reply.

You MUST respond RIGHT NOW before doing anything else:

1. Read the message that was just delivered to your input (the text above this command)
2. Compose a relevant response
3. Send it using the `send_message` tool from the **tmux-monitor** MCP server:
   - `target_agent_id`: `$ARGUMENTS`
   - `message`: your response (MUST begin with your own agent_id in square brackets, e.g. `[myhost@/my/path:1234] response text`)
   - `rsvp`: set to `true` only if you need a reply back

This is synchronous communication — the sender is blocked waiting for your answer.
