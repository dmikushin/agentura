# Agentura

Multi-agent orchestration platform. Deploy, monitor, and coordinate AI agents (Claude Code, Gemini CLI) across local and remote hosts through tmux.

## What it does

Agentura lets AI agents **spawn other agents**, **form teams**, **communicate**, **share a persistent board**, and **coordinate work** — all without human intervention. Agents run on the same machine or on any SSH-accessible remote host.

```
┌──────────────────────────────────────────────────────────────┐
│  docker compose                                              │
│  ┌──────────────────┐  ┌──────────┐  ┌────────────────────┐  │
│  │ agentura-server  │  │ postgres │  │ ollama             │  │
│  │ (aiohttp)        │  │ +pgvector│  │ nomic-embed-text   │  │
│  └──────────────────┘  └──────────┘  └────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
         ▲                    ▲
         │ HTTPS              │ embeddings
    ┌────┴───────────────────────────────────┐
    │  tmux session "my-team"                │
    │  ┌──────────┐ ┌──────────┐ ┌────────┐ │
    │  │ Claude A │ │ Gemini B │ │Claude C│ │
    │  │ (local)  │ │ (local)  │ │(remote)│ │
    │  └──────────┘ └──────────┘ └────────┘ │
    │  each: agentura-run → sidecar + agent  │
    └────────────────────────────────────────┘
```

## Features

### Agent lifecycle
- Spawn Claude or Gemini agents in tmux windows via MCP tools
- Auto-register on startup, detect exit via heartbeat
- Per-PID log files (`agentura-run-{PID}.log`) for debugging
- Graceful restart with `--resume UUID` session recovery
- Team members grouped in named tmux sessions

### Communication
- **send_message** — point-to-point with optional RSVP (synchronous reply)
- **broadcast_message** — send to all team members at once
- **TUI readiness probe** — verifies agent input field is active before injecting text
- **Enter retry loop** — 60 attempts over 30s with Space+Enter, double/triple Enter strategies
- **Bracketed paste** — multiline messages don't trigger premature submit
- **Disconnect notifications** — team members informed when an agent goes offline

### Team board (ogham semantic memory)
- Persistent shared context backed by PostgreSQL + pgvector
- **Semantic search** — find entries by meaning via hybrid search (embeddings + full-text)
- **Auto-linking** — related entries connected in knowledge graph
- **Importance scoring** — entries ranked by significance
- Powered by [ogham-mcp](https://github.com/ogham-mcp/ogham-mcp), embeddings via Ollama (nomic-embed-text)

### Teams
- UNIX groups model: ownership, admin delegation, join/approve flow
- Auto-join on creation — agents added to team at registration, no request/approve ceremony needed
- Forced succession with 60s grace period
- Concurrent-safe MCP config writes (flock)

### Remote agents
- Deploy to any SSH host via delegation tokens
- Agent auto-joins team specified at creation time
- Fish/bash/zsh compatible (`env VAR=value` prefix)
- Host registry served from server (`/hosts` endpoint)

### Social layer
- **Agent bio** — optional self-description visible in `list_agents`
- **Skills** based on organizational science:
  - `/bootstrap-team` — Tuckman, Katzenbach, Belbin
  - `/introduce` — Edmondson, Wegner, Cialdini, Lencioni
  - `/standup` — Scrum Guide, Hackman, Toyota, Greenleaf
  - `/brainstorm` — Osborn, Nemeth, De Bono, Kaner
- **Agent context** (CLAUDE.md / GEMINI.md) deployed from server on startup
- **Sprint timer** with clock hook — elapsed/remaining time shown after every tool call

### Auth
- SSH challenge-response via `~/.ssh/config` IdentityFile (no ssh-agent required)
- Fallback to ssh-agent if no config entry
- Unified auth middleware — bearer, delegation, and agent tokens accepted on all endpoints

## Components

| Component | Language | Description |
|---|---|---|
| **agentura-server** | Python | Central daemon: HTTP API, agent/team registries, board, sprints |
| **agentura-run** | Go | Agent launcher: auth, register, deploy MCP/skills/context, sidecar |
| **agentura-mcp** | Go | MCP stdio server (stable process, delegates to backend) |
| **agentura-mcp-backend** | Go | Tool executor (one invocation per tool call) |
| **agentura-clock** | Go | Post-tool-call hook: prints current time and sprint status |
| **auth.py** | Python | SSH key verification (server-side) |
| **ogham_board.py** | Python | Adapter: agentura board API → ogham semantic memory |

## Quick start

### Prerequisites

- Docker, tmux, Go 1.22+
- Claude Code and/or Gemini CLI installed
- SSH key added to `secrets/authorized_keys`
- `Host <server-hostname>` with `IdentityFile` in `~/.ssh/config`

### Install and run

```bash
# Build Go binaries
make build

# Install to PATH
cp bin/agentura-* ~/go/bin/

# Start server + PostgreSQL + Ollama
docker compose up -d

# Verify
curl https://agents.yourdomain.com/api/auth/challenge
```

### First agent

```bash
# In a tmux session:
agentura-run --claude
# or
agentura-run --gemini
```

MCP config (`.mcp.json` / `.gemini/settings.json`) is auto-created in the working directory.

## MCP Tools

| Tool | Description |
|---|---|
| **list_agents** | List all registered agents with bio, teams, and status |
| **list_hosts** | Show available hosts (local + remote from hosts.json) |
| **create_agent** | Spawn an agent on any host, optionally in a team |
| **read_stream** | Read new output from an agent (cursor-based dedup) |
| **send_message** | Send a message to an agent (with optional RSVP for sync reply) |
| **broadcast_message** | Send to all members of a team |
| **post_to_board** | Write to the team's persistent semantic memory board |
| **read_board** | Read recent entries or search by meaning (`?q=...`) |
| **search_board** | Semantic + full-text hybrid search on the board |
| **restart_agent** | Graceful restart with `--resume` session recovery |
| **interrupt_agent** | Send Escape to cancel an agent's current operation |
| **create_team** | Create a team (you become owner) |
| **request_join_team** | Request to join a team |
| **approve_join** / **deny_join** | Approve or deny join requests |
| **list_teams** | List all teams with members |
| **list_pending_requests** | Show pending join requests |
| **transfer_ownership** | Transfer team ownership |
| **leave_team** | Leave a team (triggers succession if owner) |
| **add_admin** / **remove_admin** | Manage team admins |
| **force_succession** | Force ownership transfer (60s grace period) |
| **timenow** | Check current time and sprint status |
| **start_sprint** | Start a timed sprint for the team |

## Remote agents

Agents can run on any SSH-accessible host. Connection details come from `~/.ssh/config` — `hosts.json` contains only metadata (tags, notes).

```json
{
  "gambetta": {
    "tags": ["remote"],
    "notes": "88-thread/256GB heavy duty machine"
  }
}
```

### Auth chain

```
~/.ssh/config IdentityFile → bearer token → agent_token → delegation_token
```

Delegation tokens carry team assignment — remote agents auto-join on registration.

## Tests

```bash
# Go unit tests
go test ./...

# E2E tests (requires server running)
python3 tests/test_read_stream.py     # stream reading
python3 tests/test_chat.py            # messaging + RSVP
python3 tests/test_teams_auth.py      # teams + auth
python3 tests/test_remote.py          # remote agents
python3 tests/test_broadcast.py       # team broadcast
python3 tests/test_board.py           # semantic board (ogham)
python3 tests/test_sprint.py          # sprint timer + clock
```

All tests run in the pre-commit hook (`hooks/pre-commit`).

## Architecture

```
docker compose
├── agentura-server     # server.py (aiohttp, port 7850)
│   ├── HTTP API        # register, agents, teams, board, sprints, skills, context
│   ├── capture loop    # heartbeat timeout, disconnect notifications
│   └── auth middleware # SSH + bearer + delegation + agent tokens
├── postgres            # pgvector/pgvector:pg16 — board semantic memory
├── ollama              # nomic-embed-text embeddings for board search
└── agentura-tunnel     # autossh reverse tunnel → gateway HTTPS
```

The MCP server (`agentura-mcp`) runs on the host, connecting to `agentura-server` via sidecar IPC socket or HTTPS.
