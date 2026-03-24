#!/usr/bin/env python3
"""
test_chat.py — end-to-end tests for agent-to-agent messaging.

Prerequisites: docker compose up -d (agentura server running)
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import launch_agent, wait_for_pane_contains, capture_pane

MONITOR_URL = os.environ["AGENTURA_URL"]

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name} — {detail}")


_auth_headers = {}

def _ensure_auth():
    global _auth_headers
    if _auth_headers:
        return
    try:
        from auth import authenticate
        token = authenticate(MONITOR_URL)
        if token:
            _auth_headers["Authorization"] = f"Bearer {token}"
    except Exception:
        pass

def get(path):
    _ensure_auth()
    req = urllib.request.Request(f"{MONITOR_URL}{path}", headers=_auth_headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def post(path, data):
    _ensure_auth()
    headers = {"Content-Type": "application/json"}
    headers.update(_auth_headers)
    req = urllib.request.Request(
        f"{MONITOR_URL}{path}",
        data=json.dumps(data).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def dump_pane(label, pane_id):
    content = capture_pane(pane_id) or ""
    stripped = content.rstrip("\n")
    print(f"  [{label}] pane {pane_id} content:")
    for line in stripped.split("\n")[-15:]:
        print(f"    | {line}")


def dump_all_panes():
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{window_name} #{pane_pid} dead=#{pane_dead}"],
        capture_output=True, text=True, timeout=5,
    )
    print(f"  [tmux panes] {result.stdout.strip()}")


def kill_pane(pane_id):
    if pane_id:
        subprocess.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True)


def backend_call(tool, args, agent_id=None, env_override=None):
    """Call Go agentura-mcp-backend as subprocess."""
    env = env_override if env_override else os.environ.copy()
    if agent_id:
        env["AGENT_ID"] = agent_id
    env["AGENTURA_URL"] = MONITOR_URL
    p = subprocess.run(
        ["agentura-mcp-backend", tool],
        input=json.dumps(args).encode(),
        capture_output=True, timeout=30, env=env,
    )
    if p.returncode != 0:
        return f"Error: backend exit {p.returncode}: {p.stderr.decode()}"
    resp = json.loads(p.stdout.decode())
    return resp.get("result", resp.get("error", ""))


def send_message(agent_id, target_agent_id, message, rsvp=False):
    return backend_call("send_message", {
        "target_agent_id": target_agent_id,
        "message": message,
        "rsvp": rsvp,
    }, agent_id=agent_id)


def main():
    global passed, failed
    import platform
    hostname = platform.node()

    print("=== test_chat.py ===\n")

    try:
        get("/agents")
    except urllib.error.URLError:
        print("ERROR: agentura server not running. Start with: docker compose up -d")
        sys.exit(1)

    pane_a = pane_b = pane_g = None
    agent_a = agent_b = None

    try:
        # 1. Launch two agents (synchronized via ready-file)
        print("[setup] Launching agent A...")
        pane_a, agent_a = launch_agent("agent-a", MONITOR_URL)
        check("agent A registered", agent_a is not None)

        print("[setup] Launching agent B...")
        pane_b, agent_b = launch_agent("agent-b", MONITOR_URL)
        check("agent B registered", agent_b is not None)

        if not agent_a or not agent_b:
            print("ABORT: agents failed to register")
            dump_all_panes()
            sys.exit(1)

        print(f"[setup] A: {agent_a}  B: {agent_b}")
        dump_pane("A after setup", pane_a)
        dump_pane("B after setup", pane_b)

        # --- Test: send_message basic ---
        print("\n[test] Basic send_message...")
        result = send_message(agent_a, agent_b, "Hello from A")
        check("send_message returns success", "sent" in result, result)

        # Wait for message to appear in B's pane (poll, not sleep)
        pane_content = wait_for_pane_contains(pane_b, "Hello from A")
        dump_pane("B after basic send", pane_b)
        check("message appeared in B's pane", pane_content is not None,
              (capture_pane(pane_b) or "")[-200:])
        check("sender_id prefix present",
              pane_content and f"Agent {agent_a} says to you:" in pane_content,
              (pane_content or "")[-200:])

        # --- Test: rsvp mode ---
        print("\n[test] RSVP mode...")
        result = send_message(agent_a, agent_b, "Need status update", rsvp=True)
        check("rsvp send returns success", "RSVP" in result, result)

        pane_content = wait_for_pane_contains(pane_b, "/rsvp")
        dump_pane("B after rsvp send", pane_b)
        check("rsvp message appeared",
              pane_content and "Need status update" in pane_content,
              (pane_content or "")[-200:])
        check("/rsvp command appeared",
              pane_content and f"/rsvp {agent_a}" in pane_content,
              (pane_content or "")[-300:])

        # --- Test: AGENT_ID required ---
        print("\n[test] Validation...")
        env_no_id = os.environ.copy()
        env_no_id["AGENTURA_URL"] = MONITOR_URL
        env_no_id.pop("AGENT_ID", None)
        result = backend_call("send_message",
                              {"target_agent_id": agent_b, "message": "no sender"},
                              env_override=env_no_id)
        check("missing AGENT_ID rejected", "Error" in result and "AGENT_ID" in result, result)

        # --- Test: unknown target ---
        result = send_message(agent_a, "nobody@nowhere:0", "hello")
        check("unknown target rejected", "not found" in result, result)

        # --- Test: Gemini '!' sanitization ---
        print("\n[test] Gemini exclamation mark sanitization...")
        gemini_id = None
        result_g = subprocess.run(
            ["tmux", "new-window", "-P", "-F", "#{pane_id}", "-n", "gemini-test",
             f"cd /tmp && AGENTURA_URL={MONITOR_URL} exec agentura-run cat"],
            capture_output=True, text=True, timeout=5,
        )
        pane_g = result_g.stdout.strip()
        print(f"  [gemini] created pane {pane_g}, rc={result_g.returncode}")
        if pane_g:
            # Wait for sidecar to start (poll pane for "[sidecar]" message)
            wait_for_pane_contains(pane_g, "[sidecar]")
            dump_pane("gemini after create", pane_g)
            dump_all_panes()

            pid_result = subprocess.run(
                ["tmux", "display-message", "-t", pane_g, "-p", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5,
            )
            gemini_pid = int(pid_result.stdout.strip()) if pid_result.returncode == 0 else 1
            print(f"  [gemini] pane_pid={gemini_pid}")

            # Register as "gemini" name
            post("/register", {
                "agent_name": "gemini",
                "pane_id": pane_g,
                "pid": gemini_pid,
                "hostname": hostname,
                "cwd": "/tmp",
                "cmd": ["gemini"],
            })
            data = get("/agents")
            for a in data.get("agents", []):
                if a.get("pane_id") == pane_g and a.get("name") == "gemini":
                    gemini_id = a["agent_id"]

        check("gemini agent registered", gemini_id is not None)

        if gemini_id:
            result = send_message(agent_a, gemini_id, "Great job! Well done!")
            check("send to gemini succeeds", "sent" in result, result)

            pane_content = wait_for_pane_contains(pane_g, "Great job")
            dump_pane("gemini after send", pane_g)
            check("exclamation marks replaced",
                  pane_content and "!" not in pane_content.split("Great job")[-1]
                  if pane_content and "Great job" in pane_content else False,
                  (pane_content or "")[-200:])
            check("message still readable",
                  pane_content and "Great job" in pane_content,
                  (pane_content or "")[-200:])

        # --- Test: skill deployment ---
        print("\n[test] Skill deployment...")
        check("rsvp.md deployed to agent cwd",
              os.path.isfile("/tmp/.claude/commands/rsvp.md"))

    finally:
        print("\n[cleanup]")
        dump_all_panes()
        kill_pane(pane_a)
        kill_pane(pane_b)
        kill_pane(pane_g)
        subprocess.run(["rm", "-rf", "/tmp/.claude"], capture_output=True)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
