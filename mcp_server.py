#!/usr/bin/env python3
"""
mcp_server.py — MCP frontend (stable, never needs restart).

Thin FastMCP shell that delegates every tool call to mcp_backend.py
after reloading it. Edit mcp_backend.py freely — changes take effect
on the next tool invocation without restarting the MCP connection.
"""

import importlib
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent))

mcp = FastMCP("agentura")


def _call(func_name: str, **kwargs) -> str:
    """Reload backend + its deps, then call the named function."""
    # Reload our modules so code changes take effect immediately
    for mod_name in ("auth", "tmux_tools", "mcp_backend"):
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
        else:
            __import__(mod_name)
    import mcp_backend
    return getattr(mcp_backend, func_name)(**kwargs)


# --- Tool definitions (stable signatures, delegate to backend) ---

@mcp.tool()
def list_agents() -> str:
    """List all AI agents currently registered with agentura.

    Each agent is identified by hostname@cwd:PID where:
    - hostname: machine where the agent runs
    - cwd: the starting working directory of the agent
    - PID: process ID of the agent

    Returns a formatted list of connected agents with their details.
    """
    return _call("list_agents")


@mcp.tool()
def list_hosts() -> str:
    """List available hosts — local machine and remote SSH-accessible hosts.

    Remote hosts are configured in hosts.json with SSH address, tags,
    GPU/CPU info, and notes. Returns all hosts where agents can be deployed.
    """
    return _call("list_hosts")


@mcp.tool()
def create_agent(hostname: str, cwd: str, agent_type: str,
                 blocking: bool = True, team: str = "",
                 sender_agent_id: str = "") -> str:
    """Create a new AI agent in a tmux window on a local or remote host.

    For remote hosts, the agent is launched via SSH with a delegation token.
    A sidecar process on the remote host handles stream capture, heartbeats,
    and message delivery back to the central server.

    Args:
        hostname: target host (local hostname or a remote host from list_hosts)
        cwd: starting working directory for the agent
        agent_type: "claude" or "gemini"
        blocking: if True, wait for the agent to register and return its info
        team: team name to assign the new agent to (optional)
        sender_agent_id: your own agent_id — if provided and you have no teams,
                         a new team is auto-created for both of you

    Returns:
        Agent info with team assignment.
    """
    return _call("create_agent", hostname=hostname, cwd=cwd,
                 agent_type=agent_type, blocking=blocking,
                 team=team, sender_agent_id=sender_agent_id)


@mcp.tool()
def read_stream(agent_id: str) -> str:
    """Read new output from another agent's stream since the last read.

    Uses a cursor internally — first call returns all accumulated output,
    subsequent calls return only what appeared since the previous read.

    Args:
        agent_id: target agent identifier (hostname@cwd:PID from list_agents)

    Returns:
        New markdown content since last read, or "(no new content)".
    """
    return _call("read_stream", agent_id=agent_id)


@mcp.tool()
def send_message(target_agent_id: str, message: str,
                 sender_agent_id: str = "", rsvp: bool = False) -> str:
    """Send a message to another agent.

    For local agents, delivered via tmux send-keys into the terminal input.
    For remote agents, delivered via the server's message queue and polled
    by the remote sidecar process.

    Args:
        target_agent_id: recipient agent (hostname@cwd:PID from list_agents)
        message: text to send
        sender_agent_id: your own agent_id (always required, prepended as [id] prefix)
        rsvp: if True, append /rsvp command requesting immediate reply

    Returns:
        Delivery confirmation or error.
    """
    return _call("send_message", target_agent_id=target_agent_id,
                 message=message, sender_agent_id=sender_agent_id, rsvp=rsvp)


@mcp.tool()
def interrupt_agent(target_agent_id: str) -> str:
    """Interrupt an agent by sending Escape to its tmux pane.

    Use this to cancel a hanging operation (e.g. a stuck MCP tool call).
    The agent's current action is aborted and it returns to the prompt.

    Args:
        target_agent_id: agent to interrupt (hostname@cwd:PID from list_agents)
    """
    return _call("interrupt_agent", target_agent_id=target_agent_id)


@mcp.tool()
def list_teams() -> str:
    """List all agent teams with their owners and members."""
    return _call("list_teams")


@mcp.tool()
def create_team(name: str) -> str:
    """Create a new team. You become the owner.

    Uses your AGENT_TOKEN (set automatically at registration) to prove identity.

    Args:
        name: team name (must be unique)
    """
    return _call("create_team", name=name)


@mcp.tool()
def request_join_team(team_name: str, message: str = "") -> str:
    """Request to join an existing team. The team owner must approve.

    Uses your AGENT_TOKEN to prove identity.

    Args:
        team_name: name of the team to join
        message: optional message to the team owner explaining why you want to join
    """
    return _call("request_join_team", team_name=team_name, message=message)


@mcp.tool()
def approve_join(team_name: str, pending_agent_id: str) -> str:
    """Approve a pending join request (team owner only).

    Args:
        team_name: team name
        pending_agent_id: agent_id of the requester to approve
    """
    return _call("approve_join", team_name=team_name, pending_agent_id=pending_agent_id)


@mcp.tool()
def deny_join(team_name: str, pending_agent_id: str) -> str:
    """Deny a pending join request (team owner only).

    Args:
        team_name: team name
        pending_agent_id: agent_id of the requester to deny
    """
    return _call("deny_join", team_name=team_name, pending_agent_id=pending_agent_id)


@mcp.tool()
def list_pending_requests(team_name: str) -> str:
    """List pending join requests for a team.

    Args:
        team_name: name of the team
    """
    return _call("list_pending_requests", team_name=team_name)


@mcp.tool()
def transfer_ownership(team_name: str, new_owner: str) -> str:
    """Transfer team ownership to another member (owner only).

    Args:
        team_name: name of the team
        new_owner: agent_id of the member to become the new owner
    """
    return _call("transfer_ownership", team_name=team_name, new_owner=new_owner)


@mcp.tool()
def leave_team(team_name: str) -> str:
    """Leave a team voluntarily. If you are the owner, succession is triggered.

    Args:
        team_name: name of the team to leave
    """
    return _call("leave_team", team_name=team_name)


@mcp.tool()
def add_admin(team_name: str, admin_agent_id: str) -> str:
    """Add an admin to the team (owner only). Admins can approve/deny join requests.

    Args:
        team_name: name of the team
        admin_agent_id: agent_id of the member to promote to admin
    """
    return _call("add_admin", team_name=team_name, admin_agent_id=admin_agent_id)


@mcp.tool()
def remove_admin(team_name: str, admin_agent_id: str) -> str:
    """Remove an admin from the team (owner only).

    Args:
        team_name: name of the team
        admin_agent_id: agent_id of the admin to demote
    """
    return _call("remove_admin", team_name=team_name, admin_agent_id=admin_agent_id)


@mcp.tool()
def force_succession(team_name: str, reason: str = "") -> str:
    """Request forced succession of team ownership (admin only).

    Starts a 60-second grace period. If the current owner doesn't respond
    with any team action within that time, ownership passes to the next member.

    Args:
        team_name: name of the team
        reason: optional reason for the force-succession request
    """
    return _call("force_succession", team_name=team_name, reason=reason)


if __name__ == "__main__":
    mcp.run()
