#!/usr/bin/env python3
"""
agent.py — prefix launcher that registers an agent with agentura server.

Usage:
  agent-run --claude           (launch Claude with bypass permissions)
  agent-run --gemini           (launch Gemini with auto-accept)
  agent-run <command> [args]   (launch arbitrary command)

Unified flow (all agents — local and remote):
1. Parse args, check $TMUX_PANE
2. Authenticate: try SSH key first (local), fall back to AGENTURA_TOKEN env
   (remote delegation token)
3. Register with server (bearer → /register, delegation → /sidecar/register)
4. Get agent_token from response
5. Deploy skills
6. Clear nesting guards, trust cwd for Claude
7. Fork: child does execvp (becomes agent), parent runs RemoteSidecar
8. Sidecar uses agent_token for /sidecar/* calls (heartbeat, stream-push,
   messages)
"""

import json
import os
import platform
import shutil
import sys
import urllib.request
import urllib.error
from pathlib import Path


def _load_dotenv():
    """Load .env from cwd if it exists (key=value lines, skip comments)."""
    env_path = Path.cwd() / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

_load_dotenv()

MONITOR_URL = None  # set in main() from AGENTURA_URL env
REGISTER_TIMEOUT = 2.0

# Agent presets: --flag → (binary, [args...])
AGENT_PRESETS = {
    "--claude": ("claude", [
        "--dangerously-skip-permissions",
        "--permission-mode", "bypassPermissions",
    ]),
    "--gemini": ("gemini", ["-y"]),
}


def main():
    presets = " | ".join(AGENT_PRESETS)

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(f"Usage: agent-run {{{presets} | <command> [args...]}}")
        print()
        print("Agentura agent launcher — registers with the server, deploys")
        print("skills, then forks: child becomes agent, parent runs sidecar.")
        print()
        print("Environment:")
        print("  AGENTURA_URL    Server URL (required, e.g. https://agents.example.com)")
        print("  AGENTURA_TOKEN  Delegation token (set automatically for remote agents)")
        sys.exit(0 if len(sys.argv) >= 2 else 1)

    global MONITOR_URL
    MONITOR_URL = os.environ.get("AGENTURA_URL")
    if not MONITOR_URL:
        print("Error: AGENTURA_URL environment variable is required", file=sys.stderr)
        print("  Set it in env or create a .env file in the working directory", file=sys.stderr)
        sys.exit(1)

    # --- Resolve command ---
    first_arg = sys.argv[1]

    if first_arg in AGENT_PRESETS:
        binary, preset_args = AGENT_PRESETS[first_arg]
        extra_args = sys.argv[2:]  # allow appending extra flags
        cmd = binary
        args = [binary] + preset_args + extra_args
    else:
        cmd = first_arg
        args = sys.argv[1:]

    # --- Check TMUX_PANE ---
    pane_id = os.environ.get("TMUX_PANE")
    if not pane_id:
        print("Error: not inside a tmux session ($TMUX_PANE not set)", file=sys.stderr)
        sys.exit(1)

    # --- Check command exists ---
    cmd_path = shutil.which(cmd)
    if not cmd_path:
        print(f"Error: command '{cmd}' not found in PATH", file=sys.stderr)
        sys.exit(1)

    # --- Authenticate ---
    # Try SSH key first (local), fall back to AGENTURA_TOKEN (remote delegation)
    bearer_token = None
    delegation_token = os.environ.get("AGENTURA_TOKEN")

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from auth import authenticate
        bearer_token = authenticate(MONITOR_URL)
        if bearer_token:
            print("[agent-run] Authenticated with SSH key", file=sys.stderr)
    except Exception as e:
        print(f"[agent-run] Warning: SSH auth failed: {e}", file=sys.stderr)

    if not bearer_token and delegation_token:
        print("[agent-run] Using delegation token (AGENTURA_TOKEN)", file=sys.stderr)
    elif not bearer_token and not delegation_token:
        print("[agent-run] Warning: no auth available, proceeding without monitoring",
              file=sys.stderr)

    # --- Register with server ---
    agent_name = os.path.basename(cmd)
    hostname = platform.node()
    cwd = os.getcwd()

    payload = json.dumps({
        "agent_name": agent_name,
        "pane_id": pane_id,
        "pid": os.getpid(),
        "hostname": hostname,
        "cwd": cwd,
        "cmd": args,
    }).encode()

    agent_token = None
    agent_id = None

    if bearer_token:
        # Local agent: bearer token → /register
        register_url = f"{MONITOR_URL}/register"
        auth_headers = {"Authorization": f"Bearer {bearer_token}"}
    elif delegation_token:
        # Remote agent: delegation token → /sidecar/register
        register_url = f"{MONITOR_URL}/sidecar/register"
        auth_headers = {"Authorization": f"Bearer {delegation_token}"}
    else:
        register_url = None
        auth_headers = {}

    if register_url:
        try:
            headers = {"Content-Type": "application/json"}
            headers.update(auth_headers)
            req = urllib.request.Request(
                register_url,
                data=payload,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=REGISTER_TIMEOUT) as resp:
                body = json.loads(resp.read().decode())
                if body.get("status") == "ok":
                    agent_id = body.get("agent_id", f"{hostname}@{cwd}:{os.getpid()}")
                    stream_file = body.get("stream_file", "?")
                    print(f"[agent-run] Registered as '{agent_name}' (pane {pane_id}), "
                          f"stream: {stream_file}", file=sys.stderr)
                    agent_token = body.get("agent_token")
                    if agent_token:
                        os.environ["AGENT_TOKEN"] = agent_token
                        print("[agent-run] Agent token saved to AGENT_TOKEN env",
                              file=sys.stderr)
                else:
                    print(f"[agent-run] Warning: server responded with {body}",
                          file=sys.stderr)
        except urllib.error.URLError:
            print("[agent-run] Warning: agentura server not running, "
                  "proceeding without monitoring", file=sys.stderr)
        except Exception as e:
            print(f"[agent-run] Warning: registration failed: {e}", file=sys.stderr)

    if not agent_id:
        agent_id = f"{hostname}@{cwd}:{os.getpid()}"

    # --- Deploy skills ---
    _deploy_skills(auth_headers)

    # --- Clear nesting guards ---
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        os.environ.pop(var, None)

    # --- Pre-trust cwd for Claude ---
    if cmd == "claude":
        _ensure_claude_trust(cwd)

    # --- Fork: child=agent, parent=sidecar ---
    child_pid = os.fork()
    if child_pid == 0:
        # Child process: become the agent
        os.execvp(cmd_path, args)
    else:
        # Parent process: run sidecar
        print(f"[agent-run] Forked: child PID {child_pid} (agent), "
              f"parent PID {os.getpid()} (sidecar)", file=sys.stderr)

        if agent_token:
            from sidecar import RemoteSidecar
            sidecar = RemoteSidecar(
                monitor_url=MONITOR_URL,
                token=agent_token,
                agent_id=agent_id,
                pane_id=pane_id,
                child_pid=child_pid,
            )
            sidecar.run()
        else:
            # No agent_token — can't run sidecar, just wait for child
            print("[agent-run] No agent_token, sidecar disabled. "
                  "Waiting for child to exit.", file=sys.stderr)
            try:
                _, status = os.waitpid(child_pid, 0)
                code = os.waitstatus_to_exitcode(status)
                print(f"[agent-run] Child exited with code {code}", file=sys.stderr)
            except ChildProcessError:
                pass

        sys.exit(0)


def _deploy_skills(auth_headers: dict):
    """Fetch and deploy skills from agentura server."""
    cwd_skills = Path.cwd() / ".claude" / "commands"
    try:
        req = urllib.request.Request(f"{MONITOR_URL}/skills", headers=auth_headers)
        with urllib.request.urlopen(req, timeout=REGISTER_TIMEOUT) as resp:
            skills = json.loads(resp.read().decode()).get("skills", [])
        if skills:
            cwd_skills.mkdir(parents=True, exist_ok=True)
        for skill_name in skills:
            dst = cwd_skills / skill_name
            if not dst.exists():
                req = urllib.request.Request(f"{MONITOR_URL}/skills/{skill_name}", headers=auth_headers)
                with urllib.request.urlopen(req, timeout=REGISTER_TIMEOUT) as resp:
                    content = json.loads(resp.read().decode()).get("content", "")
                dst.write_text(content)
                print(f"[agent-run] Deployed skill: {skill_name}", file=sys.stderr)
    except urllib.error.URLError:
        pass  # server not running, skip skills
    except Exception as e:
        print(f"[agent-run] Warning: skill deployment failed: {e}", file=sys.stderr)


def _ensure_claude_trust(cwd: str):
    """Mark a directory as trusted in ~/.claude.json so Claude skips the trust dialog."""
    config_path = Path.home() / ".claude.json"
    try:
        if config_path.exists():
            data = json.loads(config_path.read_text())
        else:
            data = {}

        projects = data.setdefault("projects", {})
        project = projects.setdefault(cwd, {})

        if project.get("hasTrustDialogAccepted"):
            return  # already trusted

        project["hasTrustDialogAccepted"] = True

        # Atomic write: write to temp file, then rename
        tmp_path = config_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2))
        tmp_path.rename(config_path)
        print(f"[agent-run] Trusted directory: {cwd}", file=sys.stderr)
    except Exception as e:
        print(f"[agent-run] Warning: failed to set trust for {cwd}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
