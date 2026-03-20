# Agentura

Multi-agent orchestration platform. Deploy, monitor, and coordinate AI agents (Claude Code, Gemini CLI) across local and remote hosts through tmux.

## What it does

Agentura lets AI agents **spawn other agents**, **send messages** to each other, **read each other's output**, and **form teams** — all without human intervention. Agents can run on the same machine or on any SSH-accessible remote host.

```
┌────────────────────────────────────────────────────────┐
│  agentura-server (central daemon)                      │
│                                                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
│  │ Claude A │  │ Gemini B │  │ Claude C (remote)    │  │
│  │ pane %0  │  │ pane %1  │  │ gpu-box:pane %0      │  │
│  │ local    │  │ local    │  │ sidecar → heartbeat  │  │
│  └──────────┘  └──────────┘  └──────────────────────┘  │
│                                                        │
│  Teams: [team-42: A(owner), B, C]                      │
│  Streams: /data/streams/*.md (cursor-based reads)      │
└────────────────────────────────────────────────────────┘
         ▲                              ▲
         │ MCP tools                    │ HTTPS tunnel
    agentura-mcp                  agents.domain.name
```

## Features

- **Agent lifecycle** — spawn Claude or Gemini agents in tmux windows, auto-register, detect exit
- **Messaging** — reliable delivery via `tmux send-keys` with verification; RSVP mode for synchronous replies
- **Stream reading** — cursor-based, deduplicated output capture with TUI-to-markdown cleaning
- **Teams** — UNIX groups model: ownership, admin delegation, join request/approve flow, forced succession with grace period
- **Remote agents** — deploy agents on any SSH host via delegation tokens; sidecar process handles capture, heartbeats, and message polling
- **HTTPS gateway** — autossh tunnel to a public endpoint with Let's Encrypt TLS
- **MCP interface** — 16 tools exposed via Model Context Protocol for agent-to-agent coordination
- **Hot-reload** — edit `mcp_backend.py` and changes take effect on the next tool call without restart
- **SSH authentication** — challenge-response auth using SSH keys from `authorized_keys`

## Components

| Component | File | Description |
|---|---|---|
| **agentura-server** | `server.py` | Central daemon: HTTP API, capture loop, agent/team registries |
| **agent-run** | `agent.py` | Agent launcher: register, deploy skills, fork+sidecar for remote |
| **agentura-mcp** | `mcp_server.py` | MCP frontend (stable process, hot-reloads backend) |
| **mcp_backend** | `mcp_backend.py` | Tool implementations (reloadable) |
| **sidecar** | `sidecar.py` | Remote agent companion: capture, push, heartbeat, poll |
| **tmux_tools** | `tmux_tools.py` | `reliable_send`, `stream_read`, `tui_to_md` |
| **auth** | `auth.py` | SSH challenge-response + bearer/agent/delegation tokens |

## Quick start

### Prerequisites

- Python 3.12+, tmux, Docker
- Claude Code and/or Gemini CLI installed
- SSH key in `secrets/authorized_keys`

### Install and run

```bash
# Install the package
pip install --user -e .

# Start the server + HTTPS tunnel
docker compose up -d

# Verify
curl -s https://agents.domain.name/api/auth/challenge
```

### MCP setup

Add to `~/.claude.json` under `mcpServers`:

```json
{
  "agentura": {
    "command": "agentura-mcp"
  }
}
```

Restart Claude Code. The following tools become available:

| Tool | Description |
|---|---|
| `list_agents` | List all registered agents |
| `list_hosts` | Show available hosts (local + remote from `hosts.json`) |
| `create_agent` | Spawn an agent on any host |
| `read_stream` | Read new output from an agent (cursor-based) |
| `send_message` | Send a message to an agent's input |
| `create_team` | Create a team (you become owner) |
| `request_join_team` | Request to join a team |
| `approve_join` / `deny_join` | Approve or deny join requests |
| `list_teams` | List all teams |
| `list_pending_requests` | Show pending join requests |
| `transfer_ownership` | Transfer team ownership |
| `leave_team` | Leave a team (triggers succession if owner) |
| `add_admin` / `remove_admin` | Manage team admins |
| `force_succession` | Force ownership transfer (admin, 60s grace period) |

## Remote agents

Agents can run on any SSH-accessible host. The architecture is **agent-push**: the remote sidecar pushes data to the central server — the server never SSHes out.

### Host registry

Create `hosts.json` in the data directory:

```json
{
  "gpu-box": {
    "tags": ["gpu", "training"],
    "gpu": "A100 80GB",
    "cpu_count": 64,
    "notes": "ML training server"
  }
}
```

The key (`gpu-box`) is used directly as the SSH host alias — configure
the actual connection details (hostname, port, user, key) in `~/.ssh/config`.

### Auth chain

```
SSH key → bearer token → agent_token → delegation_token
```

The delegation token is passed to the remote host at launch. The sidecar uses it to authenticate with the central server autonomously — no SSH agent forwarding needed.

### Remote agent lifecycle

1. Local agent calls `create_agent(hostname="gpu-box", ...)`
2. Server creates a delegation token
3. SSH to remote host: `agent-run --claude` with `AGENTURA_TOKEN` env
4. `agent-run` forks: child becomes Claude (execvp), parent becomes sidecar
5. Sidecar loop: capture pane → push stream, send heartbeat, poll messages
6. If child dies, sidecar sends final heartbeat and exits
7. Server detects death (heartbeat timeout or child-dead report), triggers team succession

## Authentication

All API endpoints require SSH challenge-response authentication:

1. Client requests a nonce from `/api/auth/challenge`
2. Client signs the nonce with an SSH key via `ssh-agent`
3. Server verifies the signature against `authorized_keys`
4. Server returns a bearer token (TTL: 5 min)

Agent tokens (TTL: 1 hour) are scoped to `(agent_id, user)` for team operations.
Delegation tokens (TTL: 24 hours) allow remote sidecars to operate autonomously.

## Tests

```bash
# Run all tests (requires server running)
python3 tests/test_read_stream.py    # stream reading (11 tests)
python3 tests/test_chat.py           # messaging + RSVP (15 tests)
python3 tests/test_teams_auth.py     # teams + auth (56 tests)
python3 tests/test_remote.py         # remote agents (29 tests)
```

## Architecture

```
docker compose
├── agentura-server     # server.py (aiohttp, port 7850)
│   ├── HTTP API        # register, agents, stream, teams, sidecar endpoints
│   ├── capture loop    # adaptive interval, heartbeat timeout for remote
│   └── auth middleware # bearer + delegation tokens
└── agentura-tunnel     # autossh reverse tunnel → gateway HTTPS
```

The MCP server (`agentura-mcp`) runs as a separate process on the host, connecting to `agentura-server` via HTTPS.
