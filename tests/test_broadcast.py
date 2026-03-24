#!/usr/bin/env python3
"""
test_broadcast.py — tests for team broadcast messaging.

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

from helpers import launch_agent as _launch_agent

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


def post_expect_error(path, data, expected_code):
    _ensure_auth()
    headers = {"Content-Type": "application/json"}
    headers.update(_auth_headers)
    req = urllib.request.Request(
        f"{MONITOR_URL}{path}",
        data=json.dumps(data).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode()) if e.readable() else {}
        return e.code, body


def launch_mock_agent(name, cwd="/tmp"):
    pane_id, agent_id = _launch_agent(name, MONITOR_URL, cmd="cat", cwd=cwd)
    agent_token = None
    if agent_id:
        try:
            resp = post("/api/auth/agent-token", {"agent_id": agent_id})
            agent_token = resp.get("agent_token")
        except Exception:
            pass
    return pane_id, agent_id, agent_token


def kill_pane(pane_id):
    if pane_id:
        subprocess.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True)


def poll_messages(agent_id):
    """Poll and return messages for an agent."""
    try:
        resp = get(f"/sidecar/messages?agent_id={urllib.parse.quote(agent_id, safe='')}")
        return resp.get("messages", [])
    except Exception:
        return []


import urllib.parse


def main():
    global passed, failed

    print("=== test_broadcast.py ===\n")

    try:
        get("/agents")
    except urllib.error.URLError:
        print("ERROR: agentura server not running. Start with: docker compose up -d")
        sys.exit(1)

    pane_a = pane_b = pane_c = None
    agent_a = agent_b = agent_c = None
    token_a = token_b = token_c = None
    team_name = f"broadcast-test-{int(time.time())}"

    try:
        # --- Setup: launch 3 agents ---
        print("[setup] Launching agents A, B, C...")
        pane_a, agent_a, token_a = launch_mock_agent("bcast-a")
        pane_b, agent_b, token_b = launch_mock_agent("bcast-b")
        pane_c, agent_c, token_c = launch_mock_agent("bcast-c")

        check("agent A registered", agent_a is not None)
        check("agent B registered", agent_b is not None)
        check("agent C registered", agent_c is not None)

        if not all([agent_a, agent_b, agent_c, token_a, token_b, token_c]):
            print("ABORT: agents failed to register")
            sys.exit(1)

        # --- Setup: create team, add all 3 ---
        print("\n[setup] Creating team and adding members...")
        resp = post("/teams", {"name": team_name, "agent_token": token_a})
        check("team created", resp.get("status") == "ok", str(resp))

        # B requests join, A approves
        post("/teams/request-join", {"team": team_name, "agent_token": token_b})
        resp = post("/teams/approve", {
            "team": team_name,
            "pending_agent_id": agent_b,
            "agent_token": token_a,
        })
        check("B joined team", resp.get("status") == "ok", str(resp))

        # C requests join, A approves
        post("/teams/request-join", {"team": team_name, "agent_token": token_c})
        resp = post("/teams/approve", {
            "team": team_name,
            "pending_agent_id": agent_c,
            "agent_token": token_a,
        })
        check("C joined team", resp.get("status") == "ok", str(resp))

        # Drain any join notification messages
        poll_messages(agent_a)
        poll_messages(agent_b)
        poll_messages(agent_c)

        # --- Test: broadcast from A ---
        print("\n[test] Broadcast from A...")
        resp = post("/teams/broadcast", {
            "team_name": team_name,
            "text": f"Agent {agent_a} says to team: Hello everyone",
            "sender": agent_a,
        })
        check("broadcast succeeds", resp.get("status") == "ok", str(resp))
        check("2 recipients", resp.get("recipients") == 2, f"recipients={resp.get('recipients')}")

        # Check B received the message
        msgs_b = poll_messages(agent_b)
        check("B received broadcast", any("Hello everyone" in m.get("text", "") for m in msgs_b),
              str(msgs_b))

        # Check C received the message
        msgs_c = poll_messages(agent_c)
        check("C received broadcast", any("Hello everyone" in m.get("text", "") for m in msgs_c),
              str(msgs_c))

        # Check A did NOT receive own message
        msgs_a = poll_messages(agent_a)
        check("A did not receive own broadcast",
              not any("Hello everyone" in m.get("text", "") for m in msgs_a),
              str(msgs_a))

        # --- Test: non-member broadcast fails ---
        print("\n[test] Non-member broadcast...")
        code, body = post_expect_error("/teams/broadcast", {
            "team_name": team_name,
            "text": "unauthorized",
            "sender": "fake@agent:999",
        }, 403)
        check("non-member broadcast rejected", code == 403, f"code={code}")

        # --- Test: broadcast to nonexistent team fails ---
        print("\n[test] Nonexistent team broadcast...")
        code, body = post_expect_error("/teams/broadcast", {
            "team_name": "no-such-team",
            "text": "test",
            "sender": agent_a,
        }, 404)
        check("nonexistent team rejected", code == 404, f"code={code}")

    finally:
        print("\n[cleanup]")
        kill_pane(pane_a)
        kill_pane(pane_b)
        kill_pane(pane_c)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
