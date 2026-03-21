"""
mcp_backend.py — tool implementations for agentura MCP server.

Reloadable at runtime by mcp_server.py (frontend).
All functions are plain Python — no MCP decorators here.
"""

import json
import os
import platform
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from auth import authenticate

MONITOR_URL = os.environ.get("AGENTURA_URL")
if not MONITOR_URL:
    raise RuntimeError("AGENTURA_URL environment variable is required")
AGENT_RUN_PATH = "agent-run"
DATA_DIR = Path(os.environ.get("AGENTURA_DATA_DIR", "/data"))
HOSTS_REGISTRY_PATH = Path(os.environ.get("HOSTS_REGISTRY_PATH", str(DATA_DIR / "hosts.json")))

AGENT_PRESETS = {"claude", "gemini"}

# --- State (survives within a single module load) ---
_cursors: dict[str, int] = {}
_auth_token: str | None = None
_agent_token: str | None = os.environ.get("AGENT_TOKEN")
_agent_id: str | None = os.environ.get("AGENT_ID")


# --- HTTP helpers ---

def _refresh_token():
    global _auth_token, _agent_token, _agent_id
    try:
        _auth_token = authenticate(MONITOR_URL)
    except Exception:
        _auth_token = None
    # Also refresh agent_token and agent_id from env (agent.py may have set them)
    env_token = os.environ.get("AGENT_TOKEN")
    if env_token:
        _agent_token = env_token
    env_id = os.environ.get("AGENT_ID")
    if env_id:
        _agent_id = env_id


def _refresh_agent_token(agent_id: str):
    """Refresh the agent token via the server."""
    global _agent_token
    try:
        resp = _post("/api/auth/agent-token", {"agent_id": agent_id})
        if resp.get("status") == "ok":
            _agent_token = resp["agent_token"]
            os.environ["AGENT_TOKEN"] = _agent_token
    except Exception:
        pass


def _get_auth_headers() -> dict:
    if _auth_token is None:
        _refresh_token()
    if _auth_token:
        return {"Authorization": f"Bearer {_auth_token}"}
    return {}


def _get(path: str) -> dict:
    for attempt in range(2):
        req = urllib.request.Request(f"{MONITOR_URL}{path}", headers=_get_auth_headers())
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                _refresh_token()
                continue
            raise


def _post(path: str, data: dict) -> dict:
    for attempt in range(2):
        headers = {"Content-Type": "application/json"}
        headers.update(_get_auth_headers())
        req = urllib.request.Request(
            f"{MONITOR_URL}{path}", data=json.dumps(data).encode(),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                _refresh_token()
                continue
            raise


def _resolve_agent(agent_id: str) -> tuple[dict | None, str | None]:
    try:
        data = _get("/agents")
    except urllib.error.URLError:
        return None, "agentura server is not running"
    for a in data.get("agents", []):
        if a.get("agent_id") == agent_id:
            return a, None
    return None, f"agent '{agent_id}' not found (use list_agents to see available agents)"


def _format_agent(a: dict) -> str:
    agent_id = a.get("agent_id", f"{a.get('hostname', '?')}@{a.get('cwd', '?')}:{a.get('pid', '?')}")
    name = a["name"]
    pane = a["pane_id"]
    started = a.get("started_at", "?")
    cmd = " ".join(a.get("cmd", []))
    host = a.get("hostname", "?")
    return f"- **{agent_id}** — {name} (pane {pane}, host {host}, since {started})\n  cmd: `{cmd}`"


# --- Tool implementations (called by frontend) ---

def list_agents() -> str:
    """List all AI agents currently registered with agentura."""
    try:
        data = _get("/agents")
    except urllib.error.URLError:
        return "Error: agentura server is not running (cannot connect to {})".format(MONITOR_URL)

    agents = data.get("agents", [])
    if not agents:
        return "No agents currently registered."

    lines = [_format_agent(a) for a in agents]
    return f"{len(agents)} agent(s) connected:\n\n" + "\n".join(lines)


def _load_host_registry() -> dict:
    """Load hosts.json registry."""
    if HOSTS_REGISTRY_PATH.exists():
        try:
            return json.loads(HOSTS_REGISTRY_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def list_hosts() -> str:
    """List available hosts including remote hosts from hosts.json registry."""
    hostname = platform.node()
    lines = [f"- **{hostname}** (local)"]

    hosts = _load_host_registry()
    for name, info in hosts.items():
        if name == hostname:
            continue  # skip if same as local
        parts = [f"**{name}**"]
        tags = info.get("tags", [])
        if tags:
            parts.append(f"tags: {', '.join(tags)}")
        gpu = info.get("gpu", "")
        if gpu:
            parts.append(f"GPU: {gpu}")
        cpu = info.get("cpu_count")
        if cpu:
            parts.append(f"CPUs: {cpu}")
        notes = info.get("notes", "")
        if notes:
            parts.append(notes)
        lines.append(f"- {' | '.join(parts)}")

    return f"{len(lines)} host(s) available:\n\n" + "\n".join(lines)


def create_agent(hostname: str, cwd: str, agent_type: str,
                 blocking: bool = True, team: str = "") -> str:
    """Create a new AI agent in a tmux window (local or remote).

    If the sender has no teams, a new team is auto-created for both agents.
    If team is specified, the new agent joins that team.
    Sender identity is always taken from AGENT_ID env (set by agent-run).
    """
    sender_agent_id = _agent_id or ""

    if agent_type not in AGENT_PRESETS:
        types = ", ".join(AGENT_PRESETS)
        return f"Error: unknown agent type '{agent_type}', expected one of: {types}"

    local = platform.node()
    if hostname == local:
        return _create_local_agent(hostname, cwd, agent_type, blocking, team, sender_agent_id)

    # Remote agent
    host_registry = _load_host_registry()
    host_info = host_registry.get(hostname)
    if not host_info:
        return (f"Error: host '{hostname}' not found in host registry. "
                f"Available: {', '.join([local] + list(host_registry.keys()))}")

    return _create_remote_agent(hostname, host_info, cwd, agent_type,
                                blocking, team, sender_agent_id)


def _create_local_agent(hostname: str, cwd: str, agent_type: str,
                        blocking: bool, team: str, sender_agent_id: str) -> str:
    """Create a local agent in a tmux window."""
    shell_cmd = (
        f"cd {shlex.quote(cwd)} && "
        f"AGENTURA_URL={shlex.quote(MONITOR_URL)} "
        f"exec {shlex.quote(str(AGENT_RUN_PATH))} --{agent_type}"
    )

    try:
        result = subprocess.run(
            ["tmux", "new-window", "-P", "-F", "#{pane_id}", "-n", agent_type, shell_cmd],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return "Error: tmux is not installed or not in PATH"

    if result.returncode != 0:
        return f"Error: failed to create tmux window: {result.stderr.strip()}"

    pane_id = result.stdout.strip()
    new_agent_id = _wait_for_registration(pane_id, blocking)
    team_msg = _handle_team_assignment(new_agent_id, team, sender_agent_id)

    if new_agent_id:
        data = _get("/agents")
        for a in data.get("agents", []):
            if a.get("agent_id") == new_agent_id:
                return f"Agent created{team_msg}:\n\n{_format_agent(a)}"

    if not blocking:
        return f"Agent '{agent_type}' launched in pane {pane_id} (non-blocking, use list_agents to check)"

    return f"Warning: agent launched in pane {pane_id} but not registered after 30s"


AGENTURA_PKG = "git+https://github.com/dmikushin/agentura"


def _ssh_run(ssh_address: str, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command on a remote host via SSH."""
    return subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", ssh_address, cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _ensure_remote_agentura(ssh_address: str) -> str | None:
    """Ensure agentura is installed on the remote host. Returns error or None."""
    try:
        result = _ssh_run(ssh_address, "which agent-run", timeout=10)
        if result.returncode == 0:
            return None  # already installed
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Install
    try:
        result = _ssh_run(
            ssh_address,
            f"pip install --user {shlex.quote(AGENTURA_PKG)}",
            timeout=120,
        )
        if result.returncode != 0:
            return f"pip install failed: {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return "pip install timed out"
    except FileNotFoundError:
        return "ssh not found"

    return None


def _ensure_remote_mcp_config(ssh_address: str, cwd: str) -> str | None:
    """Ensure .mcp.json with agentura server exists in remote cwd. Returns error or None."""
    mcp_config = json.dumps({
        "mcpServers": {
            "agentura": {
                "command": "agentura-mcp",
                "env": {
                    "AGENTURA_URL": MONITOR_URL,
                },
            }
        }
    })
    try:
        # Only create if missing (don't overwrite user's config)
        cmd = (
            f"test -f {shlex.quote(cwd)}/.mcp.json || "
            f"echo {shlex.quote(mcp_config)} > {shlex.quote(cwd)}/.mcp.json"
        )
        result = _ssh_run(ssh_address, cmd, timeout=10)
        if result.returncode != 0:
            return f"failed to create .mcp.json: {result.stderr.strip()}"
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"failed to create .mcp.json: {e}"

    return None


def _create_remote_agent(hostname: str, host_info: dict, cwd: str,
                         agent_type: str, blocking: bool, team: str,
                         sender_agent_id: str) -> str:
    """Create an agent on a remote host via SSH.

    hostname is used directly as the SSH host alias (resolved via ~/.ssh/config).
    """
    # Step 0: Ensure agentura is installed on remote host
    err = _ensure_remote_agentura(hostname)
    if err:
        return f"Error: remote setup failed ({hostname}): {err}"

    # Step 0.5: Ensure .mcp.json in cwd
    err = _ensure_remote_mcp_config(hostname, cwd)
    if err:
        return f"Error: remote MCP config failed ({hostname}): {err}"

    # Step 1: Create delegation token
    if not _agent_token:
        return "Error: no AGENT_TOKEN available (agent not registered?)"

    try:
        resp = _post("/api/auth/delegate", {
            "target_host": hostname,
            "agent_token": _agent_token,
        })
        if resp.get("status") != "ok":
            return f"Error: failed to create delegation token: {resp.get('error', 'unknown')}"
        delegation_token = resp["delegation_token"]
    except Exception as e:
        return f"Error: delegation token creation failed: {e}"

    # Step 2: SSH to remote host and launch agent
    # Env vars must be inside the tmux window command (new shell)
    window_cmd = (
        f"AGENTURA_URL={shlex.quote(MONITOR_URL)} "
        f"AGENTURA_TOKEN={shlex.quote(delegation_token)} "
        f"exec {AGENT_RUN_PATH} --{agent_type}"
    )
    remote_cmd = (
        f"tmux new-window -c {shlex.quote(cwd)} -P -F '#{{pane_id}}' "
        f"-n {shlex.quote(agent_type)} {shlex.quote(window_cmd)}"
    )

    try:
        result = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", hostname,
             remote_cmd],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return "Error: ssh is not installed or not in PATH"
    except subprocess.TimeoutExpired:
        return f"Error: SSH to {hostname} timed out"

    if result.returncode != 0:
        return f"Error: SSH command failed: {result.stderr.strip()}"

    # Step 3: Wait for remote agent to register
    new_agent_id = None
    if blocking:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                data = _get("/agents")
                for a in data.get("agents", []):
                    if a.get("hostname") == hostname:
                        # Match by hostname
                        new_agent_id = a["agent_id"]
                        break
            except urllib.error.URLError:
                pass
            if new_agent_id:
                break

    team_msg = _handle_team_assignment(new_agent_id, team, sender_agent_id)

    if new_agent_id:
        data = _get("/agents")
        for a in data.get("agents", []):
            if a.get("agent_id") == new_agent_id:
                return f"Remote agent created on {hostname}{team_msg}:\n\n{_format_agent(a)}"

    if not blocking:
        return f"Remote agent '{agent_type}' launched on {hostname} (non-blocking, use list_agents to check)"

    return f"Warning: remote agent launched on {hostname} but not registered after 30s"


def _wait_for_registration(pane_id: str, blocking: bool) -> str | None:
    """Wait for a local agent to register. Returns agent_id or None."""
    if not blocking:
        return None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            data = _get("/agents")
            for a in data.get("agents", []):
                if a.get("pane_id") == pane_id:
                    return a["agent_id"]
        except urllib.error.URLError:
            pass
    return None


def _handle_team_assignment(new_agent_id: str | None, team: str,
                            sender_agent_id: str) -> str:
    """Handle team assignment for a newly created agent. Returns status message suffix."""
    team_msg = ""
    if not new_agent_id or not _agent_token:
        return team_msg

    try:
        team_name = team
        if not team_name and sender_agent_id:
            sender_teams = _get_agent_teams(sender_agent_id)
            if sender_teams:
                team_name = sender_teams[0]
            else:
                team_name = f"team-{int(time.time())}"
                _post("/teams", {"name": team_name, "agent_token": _agent_token})
                team_msg = f", new team '{team_name}' created"

        if team_name:
            try:
                resp = _post("/api/auth/agent-token", {"agent_id": new_agent_id})
                new_agent_token = resp.get("agent_token", "")
                if new_agent_token:
                    _post("/teams/request-join", {
                        "team": team_name,
                        "agent_token": new_agent_token,
                        "message": f"Auto-created by {sender_agent_id}",
                    })
                    _post("/teams/approve", {
                        "team": team_name,
                        "pending_agent_id": new_agent_id,
                        "agent_token": _agent_token,
                    })
                    if not team_msg:
                        team_msg = f", joined team '{team_name}'"
            except Exception:
                if not team_msg:
                    team_msg = f", team join pending for '{team_name}'"
    except Exception as e:
        team_msg = f", team assignment failed: {e}"

    return team_msg


def _get_agent_teams(agent_id: str) -> list[str]:
    """Get teams for an agent."""
    try:
        data = _get("/agents")
        for a in data.get("agents", []):
            if a.get("agent_id") == agent_id:
                return a.get("teams", [])
    except Exception:
        pass
    return []


def read_stream(agent_id: str) -> str:
    """Read new output from another agent's stream since the last read."""
    agent, err = _resolve_agent(agent_id)
    if err:
        return f"Error: {err}"
    pane_id = agent["pane_id"]

    offset = _cursors.get(agent_id, 0)
    encoded_pane = urllib.parse.quote(pane_id, safe="")
    try:
        resp = _get(f"/stream/{encoded_pane}?offset={offset}")
    except urllib.error.URLError:
        return "Error: agentura server is not running"

    content = resp.get("content", "")
    next_offset = resp.get("next_offset", offset)
    _cursors[agent_id] = next_offset

    if not content.strip():
        return "(no new content)"

    return content


def send_message(target_agent_id: str, message: str,
                 rsvp: bool = False) -> str:
    """Send a message to another agent via the server's message queue.
    The agent's sidecar delivers it to the tmux pane.
    Sender identity is always taken from AGENT_ID env (set by agent-run)."""
    if not _agent_id:
        return "Error: AGENT_ID env not set (not running under agent-run?)"

    agent, err = _resolve_agent(target_agent_id)
    if err:
        return f"Error: {err}"

    sender_agent_id = _agent_id

    full_message = f"[{sender_agent_id}] {message}"

    if agent.get("name") == "gemini":
        full_message = full_message.replace("!", ".")

    # All agents use message queue → sidecar injects via tmux send-keys
    if rsvp:
        full_message += f"\n/rsvp {sender_agent_id}"

    try:
        resp = _post("/sidecar/queue-message", {
            "agent_id": target_agent_id,
            "text": full_message,
            "sender": sender_agent_id,
        })
        if resp.get("status") != "ok":
            return f"Error queuing message: {resp.get('error', 'unknown')}"
    except Exception as e:
        return f"Error sending message: {e}"

    status = "sent"
    if rsvp:
        status += " (RSVP requested)"
    return f"Message {status} to {target_agent_id}"


def interrupt_agent(target_agent_id: str) -> str:
    """Interrupt an agent by sending Escape to its tmux pane.

    Cancels the agent's current operation (e.g. a hanging MCP tool call).
    The Escape is queued and delivered by the agent's sidecar.
    """
    agent, err = _resolve_agent(target_agent_id)
    if err:
        return f"Error: {err}"

    try:
        resp = _post("/sidecar/queue-message", {
            "agent_id": target_agent_id,
            "text": "\x1b",  # Escape character
            "sender": "interrupt",
        })
        if resp.get("status") != "ok":
            return f"Error: {resp.get('error', 'unknown')}"
    except Exception as e:
        return f"Error: {e}"

    return f"Escape sent to {target_agent_id}"


# --- Team tools ---

def list_teams() -> str:
    """List all agent teams."""
    try:
        data = _get("/teams")
    except urllib.error.URLError:
        return "Error: agentura server is not running"

    teams = data.get("teams", [])
    if not teams:
        return "No teams exist."

    lines = []
    for t in teams:
        members = ", ".join(t["members"])
        admins = t.get("admins", [])
        admins_str = ", ".join(admins) if admins else "none"
        pending_count = len(t.get("pending", {}))
        pending_str = f", {pending_count} pending" if pending_count else ""
        last_activity = t.get("last_owner_activity", "?")
        fsp = t.get("force_succession_pending")
        fsp_str = f"\n  ⚠ force-succession pending (by {fsp['requested_by']})" if fsp else ""
        lines.append(
            f"- **{t['name']}** (owner: {t['owner']}, {len(t['members'])} members{pending_str})\n"
            f"  members: {members}\n"
            f"  admins: {admins_str}\n"
            f"  last owner activity: {last_activity}{fsp_str}")

    return f"{len(teams)} team(s):\n\n" + "\n".join(lines)


def create_team(name: str) -> str:
    """Create a new team. Uses AGENT_TOKEN to identify the owner."""
    if not _agent_token:
        return "Error: no AGENT_TOKEN available (agent not registered?)"
    try:
        resp = _post("/teams", {"name": name, "agent_token": _agent_token})
    except urllib.error.URLError:
        return "Error: agentura server is not running"
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return f"Error: team '{name}' already exists"
        if e.code == 401:
            return "Error: agent_token expired or invalid"
        raise

    if resp.get("status") == "ok":
        return f"Team '{name}' created"
    return f"Error: {resp.get('error', 'unknown')}"


def request_join_team(team_name: str, message: str = "") -> str:
    """Request to join a team. The team owner must approve."""
    if not _agent_token:
        return "Error: no AGENT_TOKEN available (agent not registered?)"
    try:
        resp = _post("/teams/request-join", {
            "team": team_name,
            "agent_token": _agent_token,
            "message": message,
        })
    except urllib.error.URLError:
        return "Error: agentura server is not running"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"Error: team '{team_name}' not found"
        if e.code == 409:
            return f"Error: already a member of '{team_name}'"
        if e.code == 401:
            return "Error: agent_token expired or invalid"
        raise

    if resp.get("status") == "ok":
        return f"Join request sent to team '{team_name}'. Waiting for owner approval."
    return f"Error: {resp.get('error', 'unknown')}"


def approve_join(team_name: str, pending_agent_id: str) -> str:
    """Approve a pending join request (owner only)."""
    if not _agent_token:
        return "Error: no AGENT_TOKEN available (agent not registered?)"
    try:
        resp = _post("/teams/approve", {
            "team": team_name,
            "pending_agent_id": pending_agent_id,
            "agent_token": _agent_token,
        })
    except urllib.error.URLError:
        return "Error: agentura server is not running"
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return "Error: only the team owner can approve requests"
        if e.code == 404:
            return f"Error: no pending request from '{pending_agent_id}'"
        if e.code == 401:
            return "Error: agent_token expired or invalid"
        raise

    if resp.get("status") == "ok":
        return f"Approved: {pending_agent_id} is now a member of '{team_name}'"
    return f"Error: {resp.get('error', 'unknown')}"


def deny_join(team_name: str, pending_agent_id: str) -> str:
    """Deny a pending join request (owner only)."""
    if not _agent_token:
        return "Error: no AGENT_TOKEN available (agent not registered?)"
    try:
        resp = _post("/teams/deny", {
            "team": team_name,
            "pending_agent_id": pending_agent_id,
            "agent_token": _agent_token,
        })
    except urllib.error.URLError:
        return "Error: agentura server is not running"
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return "Error: only the team owner can deny requests"
        if e.code == 404:
            return f"Error: no pending request from '{pending_agent_id}'"
        if e.code == 401:
            return "Error: agent_token expired or invalid"
        raise

    if resp.get("status") == "ok":
        return f"Denied: {pending_agent_id} request for '{team_name}' rejected"
    return f"Error: {resp.get('error', 'unknown')}"


def list_pending_requests(team_name: str) -> str:
    """List pending join requests for a team."""
    try:
        resp = _get(f"/teams/{urllib.parse.quote(team_name, safe='')}/pending")
    except urllib.error.URLError:
        return "Error: agentura server is not running"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"Error: team '{team_name}' not found"
        raise

    pending = resp.get("pending", {})
    if not pending:
        return f"No pending requests for team '{team_name}'."

    lines = []
    for agent_id, info in pending.items():
        msg = info.get("message", "")
        at = info.get("requested_at", "?")
        msg_part = f" — \"{msg}\"" if msg else ""
        lines.append(f"- **{agent_id}** (requested {at}){msg_part}")

    return f"{len(pending)} pending request(s) for '{team_name}':\n\n" + "\n".join(lines)


def transfer_ownership(team_name: str, new_owner: str) -> str:
    """Transfer team ownership to another member (owner only)."""
    if not _agent_token:
        return "Error: no AGENT_TOKEN available (agent not registered?)"
    try:
        resp = _post("/teams/transfer", {
            "team": team_name,
            "new_owner": new_owner,
            "agent_token": _agent_token,
        })
    except urllib.error.URLError:
        return "Error: agentura server is not running"
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return "Error: only the team owner can transfer ownership"
        if e.code == 404:
            return f"Error: team '{team_name}' not found"
        if e.code == 400:
            body = json.loads(e.read().decode()) if e.readable() else {}
            return f"Error: {body.get('error', 'bad request')}"
        if e.code == 401:
            return "Error: agent_token expired or invalid"
        raise

    if resp.get("status") == "ok":
        return f"Ownership of '{team_name}' transferred to {new_owner}"
    return f"Error: {resp.get('error', 'unknown')}"


def leave_team(team_name: str) -> str:
    """Leave a team. If you are the owner, succession is triggered."""
    if not _agent_token:
        return "Error: no AGENT_TOKEN available (agent not registered?)"
    try:
        resp = _post("/teams/leave", {
            "team": team_name,
            "agent_token": _agent_token,
        })
    except urllib.error.URLError:
        return "Error: agentura server is not running"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"Error: team '{team_name}' not found"
        if e.code == 400:
            body = json.loads(e.read().decode()) if e.readable() else {}
            return f"Error: {body.get('error', 'bad request')}"
        if e.code == 401:
            return "Error: agent_token expired or invalid"
        raise

    if resp.get("status") == "ok":
        succession = resp.get("succession", False)
        msg = f"Left team '{team_name}'"
        if succession:
            msg += " (ownership was transferred to next member)"
        return msg
    return f"Error: {resp.get('error', 'unknown')}"


def add_admin(team_name: str, admin_agent_id: str) -> str:
    """Add an admin to the team (owner only). Admins can approve/deny join requests."""
    if not _agent_token:
        return "Error: no AGENT_TOKEN available (agent not registered?)"
    try:
        resp = _post("/teams/add-admin", {
            "team": team_name,
            "admin_agent_id": admin_agent_id,
            "agent_token": _agent_token,
        })
    except urllib.error.URLError:
        return "Error: agentura server is not running"
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return "Error: only the team owner can manage admins"
        if e.code == 404:
            return f"Error: team '{team_name}' not found"
        if e.code == 400:
            body = json.loads(e.read().decode()) if e.readable() else {}
            return f"Error: {body.get('error', 'bad request')}"
        if e.code == 409:
            return f"Error: '{admin_agent_id}' is already an admin"
        if e.code == 401:
            return "Error: agent_token expired or invalid"
        raise

    if resp.get("status") == "ok":
        return f"{admin_agent_id} is now an admin of '{team_name}'"
    return f"Error: {resp.get('error', 'unknown')}"


def remove_admin(team_name: str, admin_agent_id: str) -> str:
    """Remove an admin from the team (owner only)."""
    if not _agent_token:
        return "Error: no AGENT_TOKEN available (agent not registered?)"
    try:
        resp = _post("/teams/remove-admin", {
            "team": team_name,
            "admin_agent_id": admin_agent_id,
            "agent_token": _agent_token,
        })
    except urllib.error.URLError:
        return "Error: agentura server is not running"
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return "Error: only the team owner can manage admins"
        if e.code == 404:
            return f"Error: team '{team_name}' not found"
        if e.code == 400:
            body = json.loads(e.read().decode()) if e.readable() else {}
            return f"Error: {body.get('error', 'bad request')}"
        if e.code == 401:
            return "Error: agent_token expired or invalid"
        raise

    if resp.get("status") == "ok":
        return f"{admin_agent_id} is no longer an admin of '{team_name}'"
    return f"Error: {resp.get('error', 'unknown')}"


def force_succession(team_name: str, reason: str = "") -> str:
    """Request forced succession of team ownership (admin only).

    Starts a 60-second grace period. If the current owner doesn't respond
    with any team action within that time, ownership passes to the next member.
    """
    if not _agent_token:
        return "Error: no AGENT_TOKEN available (agent not registered?)"
    try:
        resp = _post("/teams/force-succession", {
            "team": team_name,
            "agent_token": _agent_token,
            "reason": reason,
        })
    except urllib.error.URLError:
        return "Error: agentura server is not running"
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return "Error: only admins can request force-succession"
        if e.code == 404:
            return f"Error: team '{team_name}' not found"
        if e.code == 400:
            body = json.loads(e.read().decode()) if e.readable() else {}
            return f"Error: {body.get('error', 'bad request')}"
        if e.code == 401:
            return "Error: agent_token expired or invalid"
        raise

    if resp.get("status") == "ok":
        return (f"Force-succession requested for '{team_name}'. "
                f"Owner has 60 seconds to respond with any team action to cancel.")
    return f"Error: {resp.get('error', 'unknown')}"
