#!/usr/bin/env python3
"""
test_chat.py — end-to-end tests for agent-to-agent messaging.

Prerequisites: docker compose up -d (agentura server running)

Tests:
1. send_message delivers text to target pane
2. Message is prefixed with [sender_agent_id]
3. rsvp=True sends /rsvp command after the message
4. sender_agent_id is required
5. Error on unknown target
6. Skill deployment via agent-run
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

MONITOR_URL = os.environ["AGENTURA_URL"]
AGENT_RUN = "agent-run"

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


def capture_pane(pane_id, lines=30):
    result = subprocess.run(
        ["tmux", "capture-pane", "-pt", pane_id, "-S", f"-{lines}"],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout if result.returncode == 0 else f"[CAPTURE FAILED rc={result.returncode} stderr={result.stderr}]"


def dump_pane(label, pane_id):
    """Print full pane content for diagnostics."""
    content = capture_pane(pane_id)
    # Strip trailing empty lines for readability
    stripped = content.rstrip("\n")
    print(f"  [{label}] pane {pane_id} content:")
    for line in stripped.split("\n")[-15:]:  # last 15 lines
        print(f"    | {line}")


def dump_all_panes():
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{window_name} #{pane_pid} dead=#{pane_dead}"],
        capture_output=True, text=True, timeout=5,
    )
    print(f"  [tmux panes] {result.stdout.strip()}")


def launch_mock_agent(name, cwd="/tmp"):
    """Launch cat as a mock agent (accepts input without executing it)."""
    result = subprocess.run(
        ["tmux", "new-window", "-P", "-F", "#{pane_id}", "-n", name,
         f"cd {cwd} && AGENTURA_URL={MONITOR_URL} exec {AGENT_RUN} cat"],
        capture_output=True, text=True, timeout=5,
    )
    pane_id = result.stdout.strip()
    if result.returncode != 0 or not pane_id:
        return None, None

    # Wait for registration
    agent_id = None
    for _ in range(10):
        time.sleep(1)
        data = get("/agents")
        for a in data.get("agents", []):
            if a.get("pane_id") == pane_id:
                agent_id = a["agent_id"]
                break
        if agent_id:
            break

    return pane_id, agent_id


def kill_pane(pane_id):
    if pane_id:
        subprocess.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True)


def main():
    global passed, failed
    import platform
    hostname = platform.node()

    print("=== test_chat.py ===\n")

    # 0. Check monitor
    try:
        get("/agents")
    except urllib.error.URLError:
        print("ERROR: agentura server not running. Start with: docker compose up -d")
        sys.exit(1)

    pane_a = pane_b = pane_g = None
    agent_a = agent_b = None

    try:
        # 1. Launch two bash agents
        print("[setup] Launching agent A...")
        pane_a, agent_a = launch_mock_agent("agent-a")
        check("agent A registered", agent_a is not None)

        print("[setup] Launching agent B...")
        pane_b, agent_b = launch_mock_agent("agent-b")
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
        os.environ["AGENT_ID"] = agent_a
        from mcp_backend import send_message
        import mcp_backend
        mcp_backend._agent_id = agent_a
        result = send_message(
            target_agent_id=agent_b,
            message="Hello from A",
        )
        check("send_message returns success", "sent" in result, result)

        time.sleep(3)  # wait for sidecar poll cycle (2s)
        dump_pane("B after basic send", pane_b)
        pane_content = capture_pane(pane_b)
        check("message appeared in B's pane",
              "Hello from A" in pane_content,
              pane_content[-200:])
        check("sender_id prefix present",
              f"[{agent_a}]" in pane_content,
              pane_content[-200:])

        # --- Test: rsvp mode ---
        print("\n[test] RSVP mode...")
        result = send_message(
            target_agent_id=agent_b,
            message="Need status update",
            rsvp=True,
        )
        check("rsvp send returns success", "RSVP" in result, result)

        time.sleep(3)  # wait for sidecar poll cycle
        dump_pane("B after rsvp send", pane_b)
        pane_content = capture_pane(pane_b)
        check("rsvp message appeared",
              "Need status update" in pane_content,
              pane_content[-200:])
        check("/rsvp command appeared",
              f"/rsvp {agent_a}" in pane_content,
              pane_content[-300:])

        # --- Test: AGENT_ID required ---
        print("\n[test] Validation...")
        saved_id = mcp_backend._agent_id
        mcp_backend._agent_id = None
        result = send_message(
            target_agent_id=agent_b,
            message="no sender",
        )
        check("missing AGENT_ID rejected", "Error" in result and "AGENT_ID" in result, result)
        mcp_backend._agent_id = saved_id

        # --- Test: unknown target ---
        result = send_message(
            target_agent_id="nobody@nowhere:0",
            message="hello",
        )
        check("unknown target rejected", "not found" in result, result)

        # --- Test: Gemini '!' sanitization ---
        print("\n[test] Gemini exclamation mark sanitization...")
        gemini_id = None
        result_g = subprocess.run(
            ["tmux", "new-window", "-P", "-F", "#{pane_id}", "-n", "gemini-test",
             f"cd /tmp && AGENTURA_URL={MONITOR_URL} exec {AGENT_RUN} cat"],
            capture_output=True, text=True, timeout=5,
        )
        pane_g = result_g.stdout.strip()
        print(f"  [gemini] created pane {pane_g}, rc={result_g.returncode}")
        if pane_g:
            time.sleep(3)
            dump_pane("gemini after create", pane_g)
            dump_all_panes()

            # Wait for agent to register
            gemini_pid = None
            pid_result = subprocess.run(
                ["tmux", "display-message", "-t", pane_g, "-p", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5,
            )
            gemini_pid = int(pid_result.stdout.strip()) if pid_result.returncode == 0 else 1
            print(f"  [gemini] pane_pid={gemini_pid}")

            # Register as "gemini" name by re-registering with correct name
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
            result = send_message(
                target_agent_id=gemini_id,
                message="Great job! Well done!",
            )
            check("send to gemini succeeds", "sent" in result, result)

            time.sleep(3)  # wait for sidecar poll cycle
            dump_pane("gemini after send", pane_g)
            pane_content = capture_pane(pane_g)
            check("exclamation marks replaced",
                  "!" not in pane_content.split("Great job")[-1]
                  if "Great job" in pane_content else False,
                  pane_content[-200:])
            check("message still readable",
                  "Great job" in pane_content,
                  pane_content[-200:])

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
