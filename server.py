#!/usr/bin/env python3
"""
server.py — agentura background daemon that manages registered AI agents
via the sidecar protocol (heartbeat-based liveness, message queue, stream push).

Usage:
  docker compose up -d          (preferred — via docker-compose.yml)
  python3 server.py             (direct, in a dedicated tmux pane)

Architecture: asyncio single-process with:
  - HTTP API for IPC (register, list agents, read stream)
  - Heartbeat-based liveness loop for all agents (30s timeout)
  - Message queue for agent notifications
  - Stream push from sidecars (server never captures tmux panes)

Stream files: /data/streams/<agent>-<pane>.md
"""

import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent))
from auth import SSHKeyVerifier, AuthSessionStore

# --- Paths ---
DATA_DIR = Path(os.environ.get("AGENTURA_DATA_DIR", "/data"))
STREAMS_DIR = DATA_DIR / "streams"
REGISTRY_PATH = STREAMS_DIR / "_registry.json"
BOARDS_DIR = DATA_DIR / "boards"
HOSTS_PATH = DATA_DIR / "hosts.json"
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", "/app/skills"))
AUTH_KEYS_PATH = os.environ.get("AUTHORIZED_KEYS", "/app/secrets/authorized_keys")

# --- Server settings ---
HTTP_HOST = os.environ.get("AGENTURA_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("AGENTURA_PORT", "7850"))

# --- Timing ---
BASE_INTERVAL = 2.0     # seconds between heartbeat loop ticks


class AgentEntry:
    """Represents a registered agent."""

    __slots__ = (
        "name", "pane_id", "pid", "hostname", "cwd", "cmd", "bio",
        "stream_file", "started_at", "teams",
        "last_heartbeat", "message_queue",
    )

    def __init__(self, name: str, pane_id: str, pid: int, cmd: list[str],
                 hostname: str = "", cwd: str = "", bio: str = ""):
        self.name = name
        self.pane_id = pane_id
        self.pid = pid
        self.hostname = hostname
        self.cwd = cwd
        self.cmd = cmd
        self.bio = bio
        self.last_heartbeat: float = time.monotonic()
        self.message_queue: list[dict] = []
        pane_num = pane_id.lstrip("%")
        # Include hostname for multi-machine uniqueness
        host_part = hostname or "local"
        self.stream_file = STREAMS_DIR / f"{host_part}_{name}-{pane_num}-{pid}.md"
        self.started_at = datetime.now()
        self.teams: list[str] = []

    @property
    def agent_id(self) -> str:
        return f"{self.hostname}@{self.cwd}:{self.pid}"

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "pane_id": self.pane_id,
            "pid": self.pid,
            "hostname": self.hostname,
            "cwd": self.cwd,
            "cmd": self.cmd,
            "stream_file": str(self.stream_file),
            "bio": self.bio,
            "started_at": self.started_at.isoformat(),
            "teams": self.teams,
        }


class AgentRegistry:
    """Registry of active agents, keyed by agent_id (hostname@cwd:pid)."""

    def __init__(self):
        self.agents: dict[str, AgentEntry] = {}  # key = agent_id
        self._pane_index: dict[str, str] = {}    # pane_id → agent_id (backward compat)

    def register(self, name: str, pane_id: str, pid: int, cmd: list[str],
                 hostname: str = "", cwd: str = "", bio: str = "") -> AgentEntry:
        entry = AgentEntry(name, pane_id, pid, cmd, hostname=hostname,
                           cwd=cwd, bio=bio)
        agent_id = entry.agent_id

        # Check for re-registration (same agent_id or same pane_id)
        old = self.agents.get(agent_id)
        if not old:
            old_id = self._pane_index.get(pane_id)
            if old_id:
                old = self.agents.get(old_id)

        if old:
            _append_to_stream(old.stream_file,
                              f"\n---\n*Agent re-registered: {name} (PID {pid}) at {_now()}*\n")
            entry.stream_file = old.stream_file
            # Clean up old entry
            self.agents.pop(old.agent_id, None)
            self._pane_index.pop(old.pane_id, None)
        else:
            _init_stream(entry)

        entry.last_heartbeat = time.monotonic()

        self.agents[agent_id] = entry
        self._pane_index[pane_id] = agent_id
        self._save()
        return entry

    def remove(self, agent_id: str) -> AgentEntry | None:
        entry = self.agents.pop(agent_id, None)
        if entry:
            self._pane_index.pop(entry.pane_id, None)
            self._save()
        return entry

    def get_by_pane(self, pane_id: str) -> AgentEntry | None:
        """Look up agent by pane_id (backward compat for stream endpoint)."""
        agent_id = self._pane_index.get(pane_id)
        if agent_id:
            return self.agents.get(agent_id)
        return None

    def list_all(self) -> list[dict]:
        return [e.to_dict() for e in self.agents.values()]

    def _save(self):
        data = [e.to_dict() for e in self.agents.values()]
        try:
            REGISTRY_PATH.write_text(json.dumps(data, indent=2))
        except OSError:
            pass

    def load_from_disk(self):
        if not REGISTRY_PATH.exists():
            return
        try:
            data = json.loads(REGISTRY_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return

        recovered = 0
        for item in data:
            pid = item.get("pid", 0)
            entry = AgentEntry(
                item["name"], item["pane_id"], pid, item.get("cmd", []),
                hostname=item.get("hostname", ""),
                cwd=item.get("cwd", ""),
            )
            entry.started_at = datetime.fromisoformat(item.get("started_at", _now()))
            entry.teams = item.get("teams", [])
            sf = item.get("stream_file")
            if sf:
                entry.stream_file = Path(sf)
            # Agents recovered from disk will heartbeat-timeout if truly dead
            entry.last_heartbeat = time.monotonic()
            self.agents[entry.agent_id] = entry
            self._pane_index[entry.pane_id] = entry.agent_id
            recovered += 1

        if recovered:
            print(f"[agentura] Recovered {recovered} agent(s) from registry")
            self._save()


# ---------------------------------------------------------------------------
# TeamRegistry
# ---------------------------------------------------------------------------
TEAMS_PATH = STREAMS_DIR / "_teams.json"


class TeamRegistry:
    """Registry of agent teams (analogous to UNIX groups)."""

    def __init__(self):
        self.teams: dict[str, dict] = {}  # {team_name: {"owner": agent_id, "members": [agent_id, ...], "created_at": str}}

    def create(self, name: str, owner_agent_id: str) -> dict | None:
        if name in self.teams:
            return None  # already exists
        now = _now()
        self.teams[name] = {
            "owner": owner_agent_id,
            "admins": [],
            "members": [owner_agent_id],
            "pending": {},
            "created_at": now,
            "last_owner_activity": now,
        }
        self._save()
        return self.teams[name]

    def add_member(self, team_name: str, agent_id: str) -> bool:
        team = self.teams.get(team_name)
        if not team:
            return False
        if agent_id not in team["members"]:
            team["members"].append(agent_id)
            self._save()
        return True

    def remove_member(self, team_name: str, agent_id: str) -> dict | None:
        """Remove agent from team. Returns succession_info if owner changed."""
        team = self.teams.get(team_name)
        if not team:
            return None
        if agent_id in team["members"]:
            team["members"].remove(agent_id)
        # Also remove from admins if present
        if agent_id in team.get("admins", []):
            team["admins"].remove(agent_id)
        succession_info = None
        # If owner left, promote oldest remaining member
        if team["owner"] == agent_id and team["members"]:
            new_owner = team["members"][0]
            team["owner"] = new_owner
            team["last_owner_activity"] = _now()
            succession_info = {
                "team": team_name,
                "old_owner": agent_id,
                "new_owner": new_owner,
            }
        # Delete empty teams
        if not team["members"]:
            del self.teams[team_name]
        self._save()
        return succession_info

    def add_pending(self, team_name: str, agent_id: str, message: str = "") -> bool:
        team = self.teams.get(team_name)
        if not team:
            return False
        if "pending" not in team:
            team["pending"] = {}
        if agent_id in team["members"]:
            return False  # already a member
        team["pending"][agent_id] = {
            "requested_at": _now(),
            "message": message,
        }
        self._save()
        return True

    def approve_pending(self, team_name: str, agent_id: str) -> bool:
        team = self.teams.get(team_name)
        if not team:
            return False
        pending = team.get("pending", {})
        if agent_id not in pending:
            return False
        del pending[agent_id]
        if agent_id not in team["members"]:
            team["members"].append(agent_id)
        self._save()
        return True

    def deny_pending(self, team_name: str, agent_id: str) -> bool:
        team = self.teams.get(team_name)
        if not team:
            return False
        pending = team.get("pending", {})
        if agent_id not in pending:
            return False
        del pending[agent_id]
        self._save()
        return True

    def get_pending(self, team_name: str) -> dict:
        team = self.teams.get(team_name)
        if not team:
            return {}
        return team.get("pending", {})

    def get_teams_for(self, agent_id: str) -> list[str]:
        return [name for name, t in self.teams.items() if agent_id in t["members"]]

    def list_all(self) -> list[dict]:
        return [{"name": name, **info} for name, info in self.teams.items()]

    def get(self, name: str) -> dict | None:
        return self.teams.get(name)

    def transfer_ownership(self, team_name: str, new_owner: str, current_owner: str) -> bool:
        """Voluntarily transfer ownership. Returns True on success."""
        team = self.teams.get(team_name)
        if not team:
            return False
        if team["owner"] != current_owner:
            return False
        if new_owner not in team["members"]:
            return False
        team["owner"] = new_owner
        team["last_owner_activity"] = _now()
        # Remove new_owner from admins if they were an admin
        if new_owner in team.get("admins", []):
            team["admins"].remove(new_owner)
        self._save()
        return True

    def add_admin(self, team_name: str, admin_id: str, by_owner: str) -> bool:
        """Add an admin. Only owner can do this."""
        team = self.teams.get(team_name)
        if not team:
            return False
        if team["owner"] != by_owner:
            return False
        if admin_id not in team["members"]:
            return False
        if admin_id == team["owner"]:
            return False  # owner already has all rights
        if admin_id in team.get("admins", []):
            return False  # already an admin
        team.setdefault("admins", []).append(admin_id)
        self._save()
        return True

    def remove_admin(self, team_name: str, admin_id: str, by_owner: str) -> bool:
        """Remove an admin. Only owner can do this."""
        team = self.teams.get(team_name)
        if not team:
            return False
        if team["owner"] != by_owner:
            return False
        admins = team.get("admins", [])
        if admin_id not in admins:
            return False
        admins.remove(admin_id)
        self._save()
        return True

    def set_force_succession(self, team_name: str, requested_by: str, reason: str) -> bool:
        """Begin force-succession grace period. Only admins can request."""
        team = self.teams.get(team_name)
        if not team:
            return False
        if requested_by not in team.get("admins", []):
            return False
        # Must have other members to succeed to
        if len(team["members"]) < 2:
            return False
        team["force_succession_pending"] = {
            "requested_by": requested_by,
            "requested_at": _now(),
            "reason": reason,
        }
        self._save()
        return True

    def clear_force_succession(self, team_name: str):
        """Cancel force-succession grace period."""
        team = self.teams.get(team_name)
        if team and "force_succession_pending" in team:
            del team["force_succession_pending"]
            self._save()

    def touch_owner_activity(self, team_name: str):
        """Update last_owner_activity timestamp."""
        team = self.teams.get(team_name)
        if team:
            team["last_owner_activity"] = _now()
            # If owner acted during force_succession grace period, cancel it
            if "force_succession_pending" in team:
                del team["force_succession_pending"]
            self._save()

    def handle_agent_exit(self, agent_id: str) -> list[dict]:
        """Remove agent from all teams, promote owners as needed.
        Returns list of succession_info dicts."""
        successions = []
        for name in list(self.teams.keys()):
            info = self.remove_member(name, agent_id)
            if info:
                successions.append(info)
        return successions

    def _save(self):
        try:
            TEAMS_PATH.write_text(json.dumps(self.teams, indent=2))
        except OSError:
            pass

    def load_from_disk(self):
        if not TEAMS_PATH.exists():
            return
        try:
            self.teams = json.loads(TEAMS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return
        # Backfill fields for legacy teams
        for team in self.teams.values():
            if "pending" not in team:
                team["pending"] = {}
            if "admins" not in team:
                team["admins"] = []
            if "last_owner_activity" not in team:
                team["last_owner_activity"] = team.get("created_at", _now())


# --- Helpers ---

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _init_stream(entry: AgentEntry):
    entry.stream_file.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# {entry.agent_id}\n"
        f"*Started: {_now()} | Pane: {entry.pane_id} | cmd: {' '.join(entry.cmd)}*\n\n---\n"
    )
    entry.stream_file.write_text(header)


def _append_to_stream(path: Path, text: str):
    try:
        with open(path, "a") as f:
            f.write(text)
    except OSError as e:
        print(f"[agentura] Warning: cannot write to {path}: {e}", file=sys.stderr)


# --- Capture loop (heartbeat-based liveness) ---

async def _notify_succession(registry: AgentRegistry, team_registry: TeamRegistry, info: dict):
    """Send extended succession notifications to all team members."""
    team_name = info["team"]
    old_owner = info["old_owner"]
    new_owner = info["new_owner"]
    team = team_registry.get(team_name)

    # Build info for new owner
    pending_count = len(team.get("pending", {})) if team else 0
    admins = team.get("admins", []) if team else []
    admins_str = ", ".join(admins) if admins else "none"
    pending_str = f"{pending_count} pending join request(s)" if pending_count else "no pending requests"

    await _notify_agent(
        registry, new_owner,
        f"SUCCESSION: You are now the owner of team '{team_name}' "
        f"(previous owner {old_owner} exited). "
        f"{pending_str}. Admins: [{admins_str}]. "
        f"Use list_pending_requests to review.")

    # Notify other members
    if team:
        for member in team["members"]:
            if member != new_owner:
                await _notify_agent(
                    registry, member,
                    f"SUCCESSION: Team '{team_name}' ownership transferred from {old_owner} to {new_owner}")


async def capture_loop(registry: AgentRegistry, team_registry: TeamRegistry, shutdown_event: asyncio.Event):
    while not shutdown_event.is_set():
        if not registry.agents:
            await asyncio.sleep(BASE_INTERVAL)
            continue

        agents = list(registry.agents.values())

        for entry in agents:
            # All agents: check heartbeat timeout (30s without heartbeat = dead)
            elapsed = time.monotonic() - entry.last_heartbeat
            if elapsed > 30:
                print(f"[agentura] Agent '{entry.name}' heartbeat timeout ({elapsed:.0f}s)")
                _append_to_stream(entry.stream_file,
                                  f"\n---\n*Agent heartbeat timeout at {_now()}*\n")
                _notify_agent_exit(registry, entry)
                successions = team_registry.handle_agent_exit(entry.agent_id)
                registry.remove(entry.agent_id)
                for info in successions:
                    await _notify_succession(registry, team_registry, info)

        # Check force-succession grace periods
        for team_name in list(team_registry.teams.keys()):
            team = team_registry.get(team_name)
            if not team:
                continue
            fsp = team.get("force_succession_pending")
            if not fsp:
                continue
            requested_at = datetime.strptime(fsp["requested_at"], "%Y-%m-%d %H:%M:%S")
            elapsed = (datetime.now() - requested_at).total_seconds()
            if elapsed >= 60:
                # Grace period expired — execute succession
                old_owner = team["owner"]
                # Remove owner from team, triggering succession
                info = team_registry.remove_member(team_name, old_owner)
                if info:
                    # Also remove agent from registry team list
                    for entry in registry.agents.values():
                        if entry.agent_id == old_owner and team_name in entry.teams:
                            entry.teams.remove(team_name)
                    registry._save()
                    await _notify_succession(registry, team_registry, info)
                    print(f"[agentura] Force-succession executed for team '{team_name}': {old_owner} -> {info['new_owner']}")
                else:
                    team_registry.clear_force_succession(team_name)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=BASE_INTERVAL)
        except asyncio.TimeoutError:
            pass


# --- HTTP handlers ---

def _notify_agent_exit(registry: AgentRegistry, exiting: AgentEntry):
    """Notify all team members that an agent has disconnected."""
    for team_name in exiting.teams:
        team = registry  # just need to iterate agents
        for entry in registry.agents.values():
            if entry.agent_id == exiting.agent_id:
                continue
            if team_name in entry.teams:
                entry.message_queue.append({
                    "text": f"Agentura notification: Agent {exiting.agent_id} ({exiting.name}) has disconnected from team '{team_name}'.",
                    "sender": "agentura",
                    "timestamp": _now(),
                })


async def _notify_agent(registry: AgentRegistry, agent_id: str, message: str):
    """Queue a notification for an agent (delivered by sidecar)."""
    entry = registry.agents.get(agent_id)
    if not entry:
        for e in registry.agents.values():
            if e.agent_id == agent_id:
                entry = e
                break
    if not entry:
        return

    entry.message_queue.append({
        "text": f"Agentura notification: {message}",
        "sender": "agentura",
        "timestamp": _now(),
    })


async def handle_register(request: web.Request) -> web.Response:
    registry: AgentRegistry = request.app["registry"]
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        entry = registry.register(
            name=body["agent_name"],
            pane_id=body["pane_id"],
            pid=body["pid"],
            cmd=body.get("cmd", []),
            hostname=body.get("hostname", ""),
            cwd=body.get("cwd", ""),
            bio=body.get("bio", ""),
        )
        agent_token, agent_token_ttl = auth_store.create_agent_token(entry.agent_id)
        print(f"[agentura] Registered '{entry.agent_id}' ({entry.name}) pane={entry.pane_id}")

        # Auto-join team if specified in registration payload
        team_name = body.get("team", "")
        if team_name:
            team_reg: TeamRegistry = request.app["team_registry"]
            team = team_reg.get(team_name)
            if team and entry.agent_id not in team["members"]:
                team["members"].append(entry.agent_id)
                team_reg._save()
                entry.teams.append(team_name)
                print(f"[agentura] Auto-joined '{entry.agent_id}' to team '{team_name}'")

        return web.json_response({
            "status": "ok",
            "stream_file": str(entry.stream_file),
            "agent_token": agent_token,
            "agent_token_expires_in": agent_token_ttl,
        })
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_agents(request: web.Request) -> web.Response:
    registry: AgentRegistry = request.app["registry"]
    return web.json_response({"status": "ok", "agents": registry.list_all()})


async def handle_stream(request: web.Request) -> web.Response:
    registry: AgentRegistry = request.app["registry"]
    pane_id = request.match_info["pane_id"]

    entry = registry.get_by_pane(pane_id)
    if not entry or not entry.stream_file.exists():
        return web.json_response(
            {"status": "error", "error": f"No stream for pane {pane_id}"}, status=404)

    offset_str = request.query.get("offset")
    if offset_str is not None:
        # Cursor-based read: return content from byte offset to EOF
        offset = int(offset_str)
        file_size = entry.stream_file.stat().st_size
        if offset >= file_size:
            return web.json_response({
                "status": "ok", "content": "", "next_offset": file_size})
        with open(entry.stream_file, "r") as f:
            f.seek(offset)
            content = f.read()
        return web.json_response({
            "status": "ok", "content": content, "next_offset": file_size})

    # Legacy: tail-based read
    tail_lines = int(request.query.get("tail", "50"))
    content = entry.stream_file.read_text()
    lines = content.split("\n")
    tail = "\n".join(lines[-tail_lines:])
    return web.json_response({"status": "ok", "content": tail})


# --- Auth endpoints ---

async def handle_auth_challenge(request: web.Request) -> web.Response:
    auth_store: AuthSessionStore = request.app["auth_store"]
    nonce = auth_store.create_nonce()
    return web.json_response({"nonce": nonce})


async def handle_auth_verify(request: web.Request) -> web.Response:
    auth_store: AuthSessionStore = request.app["auth_store"]
    verifier: SSHKeyVerifier = request.app["key_verifier"]

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"status": "error", "error": "invalid JSON"}, status=400)

    for field in ("nonce", "key_blob", "signature", "sig_type"):
        if field not in body:
            return web.json_response(
                {"status": "error", "error": f"missing field: {field}"}, status=400)

    import base64
    nonce_b64 = body["nonce"]
    if not auth_store.consume_nonce(nonce_b64):
        return web.json_response({"status": "error", "error": "invalid or expired nonce"}, status=401)

    try:
        key_blob = base64.b64decode(body["key_blob"])
        sig_data = base64.b64decode(body["signature"])
    except Exception:
        return web.json_response({"status": "error", "error": "invalid base64"}, status=400)

    nonce_bytes = base64.b64decode(nonce_b64)

    if not verifier.is_authorized(key_blob):
        return web.json_response({"status": "error", "error": "key not authorized"}, status=403)

    if not verifier.verify(key_blob, nonce_bytes, body["sig_type"], sig_data):
        return web.json_response({"status": "error", "error": "signature verification failed"}, status=401)

    token, expires_in = auth_store.create_token()
    comment = verifier.get_comment(key_blob)
    print(f"[agentura] Authenticated key: {comment}")
    return web.json_response({"token": token, "expires_in": expires_in})


# --- Auth middleware ---

@web.middleware
async def auth_middleware(request: web.Request, handler):
    # Skip auth for auth endpoints (challenge, verify, delegate-refresh)
    if request.path.startswith("/api/auth/"):
        return await handler(request)

    auth_header = request.headers.get("Authorization", "")
    auth_store: AuthSessionStore = request.app["auth_store"]

    # All endpoints accept bearer tokens, delegation tokens, and agent tokens
    if not auth_header.startswith("Bearer "):
        return web.json_response(
            {"status": "error", "error": "missing Authorization header"}, status=401)

    token = auth_header[7:]

    # Try delegation token
    delegation_info = auth_store.validate_delegation_token(token)
    if delegation_info is not None:
        request["delegation_info"] = delegation_info
        return await handler(request)

    # Try bearer token (from SSH auth)
    if auth_store.validate_token(token) is not None:
        return await handler(request)

    # Try agent token
    if auth_store.validate_agent_token(token) is not None:
        return await handler(request)

    return web.json_response(
        {"status": "error", "error": "invalid or expired token"}, status=401)


# --- Team endpoints ---

async def handle_teams_list(request: web.Request) -> web.Response:
    team_reg: TeamRegistry = request.app["team_registry"]
    return web.json_response({"status": "ok", "teams": team_reg.list_all()})


async def handle_team_create(request: web.Request) -> web.Response:
    team_reg: TeamRegistry = request.app["team_registry"]
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        name = body["name"]
        agent_token = body["agent_token"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    owner = auth_store.validate_agent_token(agent_token)
    if owner is None:
        return web.json_response({"status": "error", "error": "invalid or expired agent_token"}, status=401)

    team = team_reg.create(name, owner)
    if team is None:
        return web.json_response({"status": "error", "error": f"team '{name}' already exists"}, status=409)

    # Also tag the owner agent
    agent_reg: AgentRegistry = request.app["registry"]
    for entry in agent_reg.agents.values():
        if entry.agent_id == owner and name not in entry.teams:
            entry.teams.append(name)
    agent_reg._save()

    print(f"[agentura] Team '{name}' created by {owner}")
    return web.json_response({"status": "ok", "team": {"name": name, **team}})


async def handle_team_join_gone(request: web.Request) -> web.Response:
    """Old join endpoint — removed in favor of request-join/approve flow."""
    return web.json_response(
        {"status": "error", "error": "POST /teams/join is removed. Use POST /teams/request-join instead."},
        status=410)


async def handle_team_request_join(request: web.Request) -> web.Response:
    team_reg: TeamRegistry = request.app["team_registry"]
    agent_reg: AgentRegistry = request.app["registry"]
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        team_name = body["team"]
        agent_token = body["agent_token"]
        message = body.get("message", "")
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    agent_id = auth_store.validate_agent_token(agent_token)
    if agent_id is None:
        return web.json_response({"status": "error", "error": "invalid or expired agent_token"}, status=401)

    team = team_reg.get(team_name)
    if not team:
        return web.json_response({"status": "error", "error": f"team '{team_name}' not found"}, status=404)

    if agent_id in team["members"]:
        return web.json_response({"status": "error", "error": "already a member"}, status=409)

    team_reg.add_pending(team_name, agent_id, message)

    # Notify owner
    owner_id = team["owner"]
    msg_part = f": {message}" if message else ""
    await _notify_agent(agent_reg, owner_id,
                        f"Join request for team '{team_name}' from {agent_id}{msg_part}")

    print(f"[agentura] Join request: {agent_id} -> team '{team_name}'")
    return web.json_response({"status": "ok", "pending": True})


async def handle_team_approve(request: web.Request) -> web.Response:
    team_reg: TeamRegistry = request.app["team_registry"]
    agent_reg: AgentRegistry = request.app["registry"]
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        team_name = body["team"]
        pending_agent_id = body["pending_agent_id"]
        agent_token = body["agent_token"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    approver_id = auth_store.validate_agent_token(agent_token)
    if approver_id is None:
        return web.json_response({"status": "error", "error": "invalid or expired agent_token"}, status=401)

    team = team_reg.get(team_name)
    if not team:
        return web.json_response({"status": "error", "error": f"team '{team_name}' not found"}, status=404)

    if approver_id != team["owner"] and approver_id not in team.get("admins", []):
        return web.json_response({"status": "error", "error": "only the team owner or admins can approve"}, status=403)

    if not team_reg.approve_pending(team_name, pending_agent_id):
        return web.json_response({"status": "error", "error": f"no pending request from '{pending_agent_id}'"}, status=404)

    # Touch owner activity if approver is owner
    if approver_id == team["owner"]:
        team_reg.touch_owner_activity(team_name)

    # Tag the approved agent
    for entry in agent_reg.agents.values():
        if entry.agent_id == pending_agent_id and team_name not in entry.teams:
            entry.teams.append(team_name)
    agent_reg._save()

    # Notify the approved agent
    await _notify_agent(agent_reg, pending_agent_id,
                        f"APPROVED: You have been accepted into team '{team_name}'")

    print(f"[agentura] Approved: {pending_agent_id} -> team '{team_name}' (by {approver_id})")
    return web.json_response({"status": "ok"})


async def handle_team_deny(request: web.Request) -> web.Response:
    team_reg: TeamRegistry = request.app["team_registry"]
    agent_reg: AgentRegistry = request.app["registry"]
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        team_name = body["team"]
        pending_agent_id = body["pending_agent_id"]
        agent_token = body["agent_token"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    denier_id = auth_store.validate_agent_token(agent_token)
    if denier_id is None:
        return web.json_response({"status": "error", "error": "invalid or expired agent_token"}, status=401)

    team = team_reg.get(team_name)
    if not team:
        return web.json_response({"status": "error", "error": f"team '{team_name}' not found"}, status=404)

    if denier_id != team["owner"] and denier_id not in team.get("admins", []):
        return web.json_response({"status": "error", "error": "only the team owner or admins can deny"}, status=403)

    if not team_reg.deny_pending(team_name, pending_agent_id):
        return web.json_response({"status": "error", "error": f"no pending request from '{pending_agent_id}'"}, status=404)

    # Touch owner activity if denier is owner
    if denier_id == team["owner"]:
        team_reg.touch_owner_activity(team_name)

    # Notify the denied agent
    await _notify_agent(agent_reg, pending_agent_id,
                        f"DENIED: Your request to join team '{team_name}' was denied")

    print(f"[agentura] Denied: {pending_agent_id} from team '{team_name}' (by {denier_id})")
    return web.json_response({"status": "ok"})


async def handle_team_pending(request: web.Request) -> web.Response:
    team_reg: TeamRegistry = request.app["team_registry"]
    team_name = request.match_info["name"]
    team = team_reg.get(team_name)
    if not team:
        return web.json_response({"status": "error", "error": f"team '{team_name}' not found"}, status=404)
    return web.json_response({"status": "ok", "pending": team.get("pending", {})})


async def handle_team_transfer(request: web.Request) -> web.Response:
    team_reg: TeamRegistry = request.app["team_registry"]
    agent_reg: AgentRegistry = request.app["registry"]
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        team_name = body["team"]
        new_owner = body["new_owner"]
        agent_token = body["agent_token"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    current_owner = auth_store.validate_agent_token(agent_token)
    if current_owner is None:
        return web.json_response({"status": "error", "error": "invalid or expired agent_token"}, status=401)

    team = team_reg.get(team_name)
    if not team:
        return web.json_response({"status": "error", "error": f"team '{team_name}' not found"}, status=404)

    if team["owner"] != current_owner:
        return web.json_response({"status": "error", "error": "only the team owner can transfer ownership"}, status=403)

    if new_owner not in team["members"]:
        return web.json_response({"status": "error", "error": f"'{new_owner}' is not a member of the team"}, status=400)

    if not team_reg.transfer_ownership(team_name, new_owner, current_owner):
        return web.json_response({"status": "error", "error": "transfer failed"}, status=500)

    # Notify all members
    for member in team["members"]:
        await _notify_agent(agent_reg, member,
                            f"TRANSFER: Ownership of team '{team_name}' transferred from {current_owner} to {new_owner}")

    print(f"[agentura] Transfer: team '{team_name}' {current_owner} -> {new_owner}")
    return web.json_response({"status": "ok"})


async def handle_team_leave(request: web.Request) -> web.Response:
    team_reg: TeamRegistry = request.app["team_registry"]
    agent_reg: AgentRegistry = request.app["registry"]
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        team_name = body["team"]
        agent_token = body["agent_token"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    agent_id = auth_store.validate_agent_token(agent_token)
    if agent_id is None:
        return web.json_response({"status": "error", "error": "invalid or expired agent_token"}, status=401)

    team = team_reg.get(team_name)
    if not team:
        return web.json_response({"status": "error", "error": f"team '{team_name}' not found"}, status=404)

    if agent_id not in team["members"]:
        return web.json_response({"status": "error", "error": "not a member of this team"}, status=400)

    # Get members list before removal for notifications
    members_before = list(team["members"])

    succession_info = team_reg.remove_member(team_name, agent_id)

    # Remove team from agent's team list
    for entry in agent_reg.agents.values():
        if entry.agent_id == agent_id and team_name in entry.teams:
            entry.teams.remove(team_name)
    agent_reg._save()

    if succession_info:
        # Owner left, succession happened
        await _notify_succession(agent_reg, team_reg, succession_info)
    else:
        # Regular member left — notify remaining members
        remaining_team = team_reg.get(team_name)
        if remaining_team:
            for member in remaining_team["members"]:
                await _notify_agent(agent_reg, member,
                                    f"LEFT: {agent_id} has left team '{team_name}'")

    print(f"[agentura] Leave: {agent_id} left team '{team_name}'")
    return web.json_response({"status": "ok", "succession": succession_info is not None})


async def handle_team_add_admin(request: web.Request) -> web.Response:
    team_reg: TeamRegistry = request.app["team_registry"]
    agent_reg: AgentRegistry = request.app["registry"]
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        team_name = body["team"]
        admin_agent_id = body["admin_agent_id"]
        agent_token = body["agent_token"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    owner_id = auth_store.validate_agent_token(agent_token)
    if owner_id is None:
        return web.json_response({"status": "error", "error": "invalid or expired agent_token"}, status=401)

    team = team_reg.get(team_name)
    if not team:
        return web.json_response({"status": "error", "error": f"team '{team_name}' not found"}, status=404)

    if team["owner"] != owner_id:
        return web.json_response({"status": "error", "error": "only the team owner can manage admins"}, status=403)

    if admin_agent_id == owner_id:
        return web.json_response({"status": "error", "error": "owner already has all admin rights"}, status=400)

    if admin_agent_id not in team["members"]:
        return web.json_response({"status": "error", "error": f"'{admin_agent_id}' is not a member"}, status=400)

    if not team_reg.add_admin(team_name, admin_agent_id, owner_id):
        return web.json_response({"status": "error", "error": "already an admin or add failed"}, status=409)

    team_reg.touch_owner_activity(team_name)

    # Notify the new admin
    await _notify_agent(agent_reg, admin_agent_id,
                        f"ADMIN: You are now an admin of team '{team_name}'")

    print(f"[agentura] Admin added: {admin_agent_id} in team '{team_name}' (by {owner_id})")
    return web.json_response({"status": "ok"})


async def handle_team_remove_admin(request: web.Request) -> web.Response:
    team_reg: TeamRegistry = request.app["team_registry"]
    agent_reg: AgentRegistry = request.app["registry"]
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        team_name = body["team"]
        admin_agent_id = body["admin_agent_id"]
        agent_token = body["agent_token"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    owner_id = auth_store.validate_agent_token(agent_token)
    if owner_id is None:
        return web.json_response({"status": "error", "error": "invalid or expired agent_token"}, status=401)

    team = team_reg.get(team_name)
    if not team:
        return web.json_response({"status": "error", "error": f"team '{team_name}' not found"}, status=404)

    if team["owner"] != owner_id:
        return web.json_response({"status": "error", "error": "only the team owner can manage admins"}, status=403)

    if not team_reg.remove_admin(team_name, admin_agent_id, owner_id):
        return web.json_response({"status": "error", "error": f"'{admin_agent_id}' is not an admin"}, status=400)

    team_reg.touch_owner_activity(team_name)

    # Notify the removed admin
    await _notify_agent(agent_reg, admin_agent_id,
                        f"ADMIN_REVOKED: You are no longer an admin of team '{team_name}'")

    print(f"[agentura] Admin removed: {admin_agent_id} from team '{team_name}' (by {owner_id})")
    return web.json_response({"status": "ok"})


async def handle_team_force_succession(request: web.Request) -> web.Response:
    team_reg: TeamRegistry = request.app["team_registry"]
    agent_reg: AgentRegistry = request.app["registry"]
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        team_name = body["team"]
        agent_token = body["agent_token"]
        reason = body.get("reason", "")
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    requester_id = auth_store.validate_agent_token(agent_token)
    if requester_id is None:
        return web.json_response({"status": "error", "error": "invalid or expired agent_token"}, status=401)

    team = team_reg.get(team_name)
    if not team:
        return web.json_response({"status": "error", "error": f"team '{team_name}' not found"}, status=404)

    if requester_id not in team.get("admins", []):
        return web.json_response({"status": "error", "error": "only admins can request force-succession"}, status=403)

    if len(team["members"]) < 2:
        return web.json_response({"status": "error", "error": "team must have other members for succession"}, status=400)

    if not team_reg.set_force_succession(team_name, requester_id, reason):
        return web.json_response({"status": "error", "error": "force-succession setup failed"}, status=500)

    # Notify owner with warning
    owner_id = team["owner"]
    reason_part = f" Reason: {reason}" if reason else ""
    await _notify_agent(agent_reg, owner_id,
                        f"WARNING: Forced succession requested by {requester_id} for team '{team_name}'.{reason_part} "
                        f"Respond with any team action within 60s to cancel.")

    print(f"[agentura] Force-succession requested for team '{team_name}' by {requester_id}")
    return web.json_response({"status": "ok", "grace_period_seconds": 60})


async def handle_agent_token_refresh(request: web.Request) -> web.Response:
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        agent_id = body["agent_id"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    token, ttl = auth_store.refresh_agent_token(agent_id)
    return web.json_response({
        "status": "ok",
        "agent_token": token,
        "agent_token_expires_in": ttl,
    })


# --- Delegation + Sidecar endpoints ---

async def handle_delegate(request: web.Request) -> web.Response:
    """Create a delegation token for a remote agent. Requires Bearer auth."""
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        target_host = body["target_host"]
        agent_token = body["agent_token"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    # Validate the agent_token to get the creator agent_id
    creator = auth_store.validate_agent_token(agent_token)
    if creator is None:
        return web.json_response(
            {"status": "error", "error": "invalid or expired agent_token"}, status=401)

    team = body.get("team", "")
    token, ttl = auth_store.create_delegation_token(creator, target_host, team=team)
    print(f"[agentura] Delegation token created by {creator} for host {target_host}" +
          (f" (team: {team})" if team else ""))
    return web.json_response({
        "status": "ok",
        "delegation_token": token,
        "expires_in": ttl,
    })


async def handle_delegate_refresh(request: web.Request) -> web.Response:
    """Refresh a delegation token. No auth required (token in body)."""
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        old_token = body["delegation_token"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    result = auth_store.refresh_delegation_token(old_token)
    if result is None:
        return web.json_response(
            {"status": "error", "error": "invalid or expired delegation token"}, status=401)

    new_token, ttl = result
    return web.json_response({
        "status": "ok",
        "delegation_token": new_token,
        "expires_in": ttl,
    })


async def handle_sidecar_register(request: web.Request) -> web.Response:
    """Register an agent via sidecar protocol."""
    registry: AgentRegistry = request.app["registry"]
    auth_store: AuthSessionStore = request.app["auth_store"]
    try:
        body = await request.json()
        entry = registry.register(
            name=body["agent_name"],
            pane_id=body["pane_id"],
            pid=body["pid"],
            cmd=body.get("cmd", []),
            hostname=body.get("hostname", ""),
            cwd=body.get("cwd", ""),
            bio=body.get("bio", ""),
        )
        agent_token, agent_token_ttl = auth_store.create_agent_token(entry.agent_id)
        print(f"[agentura] Agent registered: '{entry.agent_id}' ({entry.name})")

        # Auto-join team if delegation token carried one
        delegation_info = request.get("delegation_info")
        if delegation_info and delegation_info.get("team"):
            team_name = delegation_info["team"]
            team_reg: TeamRegistry = request.app["team_registry"]
            team = team_reg.get(team_name)
            if team and entry.agent_id not in team["members"]:
                team["members"].append(entry.agent_id)
                team_reg._save()
                entry.teams.append(team_name)
                print(f"[agentura] Auto-joined '{entry.agent_id}' to team '{team_name}'")

        return web.json_response({
            "status": "ok",
            "agent_id": entry.agent_id,
            "stream_file": str(entry.stream_file),
            "agent_token": agent_token,
            "agent_token_expires_in": agent_token_ttl,
        })
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_sidecar_stream_push(request: web.Request) -> web.Response:
    """Push stream content from sidecar."""
    registry: AgentRegistry = request.app["registry"]
    try:
        body = await request.json()
        agent_id = body["agent_id"]
        content = body["content"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    entry = registry.agents.get(agent_id)
    if not entry:
        return web.json_response(
            {"status": "error", "error": f"agent '{agent_id}' not found"}, status=404)

    if content.strip():
        _append_to_stream(entry.stream_file, f"[{_ts()}] {content}\n")
    return web.json_response({"status": "ok"})


async def handle_sidecar_heartbeat(request: web.Request) -> web.Response:
    """Heartbeat from sidecar."""
    registry: AgentRegistry = request.app["registry"]
    team_registry: TeamRegistry = request.app["team_registry"]
    try:
        body = await request.json()
        agent_id = body["agent_id"]
        child_alive = body.get("child_alive", True)
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    entry = registry.agents.get(agent_id)
    if not entry:
        return web.json_response(
            {"status": "error", "error": f"agent '{agent_id}' not found"}, status=404)

    entry.last_heartbeat = time.monotonic()

    if not child_alive:
        # Agent process died
        print(f"[agentura] Agent '{entry.name}' reported child dead")
        _append_to_stream(entry.stream_file,
                          f"\n---\n*Agent exited at {_now()}*\n")
        _notify_agent_exit(registry, entry)
        successions = team_registry.handle_agent_exit(entry.agent_id)
        registry.remove(entry.agent_id)
        for info in successions:
            await _notify_succession(registry, team_registry, info)
        return web.json_response({"status": "ok", "action": "removed"})

    return web.json_response({"status": "ok"})


async def handle_sidecar_messages(request: web.Request) -> web.Response:
    """Poll messages for an agent. Returns and clears the queue."""
    registry: AgentRegistry = request.app["registry"]
    agent_id = request.query.get("agent_id", "")
    if not agent_id:
        return web.json_response(
            {"status": "error", "error": "agent_id query parameter required"}, status=400)

    entry = registry.agents.get(agent_id)
    if not entry:
        return web.json_response(
            {"status": "error", "error": f"agent '{agent_id}' not found"}, status=404)

    messages = list(entry.message_queue)
    entry.message_queue.clear()
    return web.json_response({"status": "ok", "messages": messages})


async def handle_sidecar_queue_message(request: web.Request) -> web.Response:
    """Queue a message for an agent. Requires Bearer auth."""
    registry: AgentRegistry = request.app["registry"]
    try:
        body = await request.json()
        agent_id = body["agent_id"]
        text = body["text"]
        sender = body["sender"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    entry = registry.agents.get(agent_id)
    if not entry:
        return web.json_response(
            {"status": "error", "error": f"agent '{agent_id}' not found"}, status=404)

    entry.message_queue.append({
        "text": text,
        "sender": sender,
        "timestamp": _now(),
    })
    return web.json_response({"status": "ok"})


async def handle_team_broadcast(request: web.Request) -> web.Response:
    """Broadcast a message to all members of a team. Requires Bearer auth."""
    registry: AgentRegistry = request.app["registry"]
    team_reg: TeamRegistry = request.app["team_registry"]
    try:
        body = await request.json()
        team_name = body["team_name"]
        text = body["text"]
        sender = body["sender"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    team = team_reg.get(team_name)
    if team is None:
        return web.json_response(
            {"status": "error", "error": f"team '{team_name}' not found"}, status=404)

    if sender not in team["members"]:
        return web.json_response(
            {"status": "error", "error": f"'{sender}' is not a member of team '{team_name}'"}, status=403)

    recipients = 0
    for member_id in team["members"]:
        if member_id == sender:
            continue
        entry = registry.agents.get(member_id)
        if entry is None:
            continue
        entry.message_queue.append({
            "text": text,
            "sender": sender,
            "timestamp": _now(),
        })
        recipients += 1

    print(f"[agentura] Broadcast to team '{team_name}' from {sender}: {recipients} recipient(s)")
    return web.json_response({"status": "ok", "recipients": recipients})


# --- Hosts endpoint ---

async def handle_hosts(request: web.Request) -> web.Response:
    """Return the host registry."""
    if not HOSTS_PATH.is_file():
        return web.json_response({"status": "ok", "hosts": {}})
    try:
        hosts = json.loads(HOSTS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return web.json_response({"status": "ok", "hosts": {}})
    return web.json_response({"status": "ok", "hosts": hosts})


# --- Team board endpoints ---

async def handle_board_post(request: web.Request) -> web.Response:
    """Append a note to the team board. Requires Bearer auth."""
    team_reg: TeamRegistry = request.app["team_registry"]
    try:
        body = await request.json()
        team_name = body["team_name"]
        text = body["text"]
        sender = body["sender"]
    except (KeyError, json.JSONDecodeError) as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)

    team = team_reg.get(team_name)
    if team is None:
        return web.json_response(
            {"status": "error", "error": f"team '{team_name}' not found"}, status=404)

    if sender not in team["members"]:
        return web.json_response(
            {"status": "error", "error": f"'{sender}' is not a member of team '{team_name}'"}, status=403)

    BOARDS_DIR.mkdir(parents=True, exist_ok=True)
    board_file = BOARDS_DIR / f"{team_name}.jsonl"
    entry = json.dumps({"author": sender, "text": text, "timestamp": _now()})
    with open(board_file, "a") as f:
        f.write(entry + "\n")

    return web.json_response({"status": "ok"})


async def handle_board_read(request: web.Request) -> web.Response:
    """Read team board entries. Requires Bearer auth."""
    team_reg: TeamRegistry = request.app["team_registry"]
    team_name = request.query.get("team_name", "")
    since = int(request.query.get("since", "0"))

    if not team_name:
        return web.json_response(
            {"status": "error", "error": "team_name is required"}, status=400)

    team = team_reg.get(team_name)
    if team is None:
        return web.json_response(
            {"status": "error", "error": f"team '{team_name}' not found"}, status=404)

    board_file = BOARDS_DIR / f"{team_name}.jsonl"
    entries = []
    if board_file.is_file():
        lines = board_file.read_text().splitlines()
        for line in lines[since:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        total = len(lines)
    else:
        total = 0

    return web.json_response({
        "status": "ok",
        "entries": entries,
        "total": total,
    })


# --- Skill endpoints ---

async def handle_skills_list(request: web.Request) -> web.Response:
    skills = [f.name for f in SKILLS_DIR.glob("*.md")] if SKILLS_DIR.is_dir() else []
    return web.json_response({"status": "ok", "skills": skills})


async def handle_skill(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if not name.endswith(".md"):
        name += ".md"
    path = SKILLS_DIR / name
    if not path.is_file() or not path.resolve().is_relative_to(SKILLS_DIR.resolve()):
        return web.json_response(
            {"status": "error", "error": f"Skill '{name}' not found"}, status=404)
    return web.json_response({"status": "ok", "name": name, "content": path.read_text()})


# --- Main ---

async def main():
    STREAMS_DIR.mkdir(parents=True, exist_ok=True)

    registry = AgentRegistry()
    registry.load_from_disk()

    team_registry = TeamRegistry()
    team_registry.load_from_disk()

    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    # Auth setup — mandatory, server refuses to start without authorized_keys
    auth_store = AuthSessionStore()
    key_verifier = SSHKeyVerifier(AUTH_KEYS_PATH)
    print(f"[agentura] Auth: {AUTH_KEYS_PATH} ({len(key_verifier._keys)} key(s))")

    app = web.Application(middlewares=[auth_middleware])
    app["registry"] = registry
    app["team_registry"] = team_registry
    app["auth_store"] = auth_store
    app["key_verifier"] = key_verifier
    app.router.add_get("/api/auth/challenge", handle_auth_challenge)
    app.router.add_post("/api/auth/verify", handle_auth_verify)
    app.router.add_post("/api/auth/delegate", handle_delegate)
    app.router.add_post("/api/auth/delegate-refresh", handle_delegate_refresh)
    app.router.add_post("/register", handle_register)
    app.router.add_get("/agents", handle_agents)
    app.router.add_get("/hosts", handle_hosts)
    app.router.add_get("/stream/{pane_id}", handle_stream)
    app.router.add_get("/skills", handle_skills_list)
    app.router.add_get("/skills/{name}", handle_skill)
    app.router.add_get("/teams", handle_teams_list)
    app.router.add_post("/teams", handle_team_create)
    app.router.add_post("/teams/join", handle_team_join_gone)
    app.router.add_post("/teams/request-join", handle_team_request_join)
    app.router.add_post("/teams/approve", handle_team_approve)
    app.router.add_post("/teams/deny", handle_team_deny)
    app.router.add_get("/teams/{name}/pending", handle_team_pending)
    app.router.add_post("/teams/transfer", handle_team_transfer)
    app.router.add_post("/teams/leave", handle_team_leave)
    app.router.add_post("/teams/add-admin", handle_team_add_admin)
    app.router.add_post("/teams/remove-admin", handle_team_remove_admin)
    app.router.add_post("/teams/force-succession", handle_team_force_succession)
    app.router.add_post("/api/auth/agent-token", handle_agent_token_refresh)
    # Sidecar endpoints (all agents)
    app.router.add_post("/sidecar/register", handle_sidecar_register)
    app.router.add_post("/sidecar/stream-push", handle_sidecar_stream_push)
    app.router.add_post("/sidecar/heartbeat", handle_sidecar_heartbeat)
    app.router.add_get("/sidecar/messages", handle_sidecar_messages)
    app.router.add_post("/sidecar/queue-message", handle_sidecar_queue_message)
    app.router.add_post("/teams/broadcast", handle_team_broadcast)
    app.router.add_post("/teams/board", handle_board_post)
    app.router.add_get("/teams/board", handle_board_read)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()

    print(f"[agentura] Starting at {_now()}")
    print(f"[agentura] Listening on http://{HTTP_HOST}:{HTTP_PORT}")
    print(f"[agentura] Streams directory: {STREAMS_DIR}")

    # Run heartbeat loop until shutdown
    await capture_loop(registry, team_registry, shutdown_event)

    await runner.cleanup()
    print(f"[agentura] Shutting down at {_now()}")


if __name__ == "__main__":
    asyncio.run(main())
