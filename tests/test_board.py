#!/usr/bin/env python3
"""
test_board.py — tests for team board (persistent shared context).

Prerequisites: docker compose up -d (agentura server running)
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

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


def main():
    global passed, failed

    print("=== test_board.py ===\n")

    try:
        get("/agents")
    except urllib.error.URLError:
        print("ERROR: agentura server not running. Start with: docker compose up -d")
        sys.exit(1)

    pane_a = pane_b = None
    agent_a = agent_b = None
    token_a = token_b = None
    team_name = f"board-test-{int(time.time())}"

    try:
        # --- Setup ---
        print("[setup] Launching agents...")
        pane_a, agent_a, token_a = launch_mock_agent("board-a")
        pane_b, agent_b, token_b = launch_mock_agent("board-b")

        check("agents registered", agent_a is not None and agent_b is not None)

        if not all([agent_a, agent_b, token_a, token_b]):
            print("ABORT: agents failed to register")
            sys.exit(1)

        # Create team, add both
        post("/teams", {"name": team_name, "agent_token": token_a})
        post("/teams/request-join", {"team": team_name, "agent_token": token_b})
        post("/teams/approve", {
            "team": team_name,
            "pending_agent_id": agent_b,
            "agent_token": token_a,
        })

        # --- Test: empty board ---
        print("\n[test] Empty board...")
        encoded = urllib.parse.quote(team_name, safe="")
        resp = get(f"/teams/board?team_name={encoded}")
        check("empty board returns ok", resp.get("status") == "ok")
        check("no entries", len(resp.get("entries", [])) == 0)
        check("total is 0", resp.get("total") == 0)

        # --- Test: post to board ---
        print("\n[test] Post to board...")
        resp = post("/teams/board", {
            "team_name": team_name,
            "text": "First note from A",
            "sender": agent_a,
        })
        check("post succeeds", resp.get("status") == "ok")

        resp = post("/teams/board", {
            "team_name": team_name,
            "text": "Second note from B",
            "sender": agent_b,
        })
        check("second post succeeds", resp.get("status") == "ok")

        # --- Test: read board ---
        print("\n[test] Read board...")
        resp = get(f"/teams/board?team_name={encoded}")
        entries = resp.get("entries", [])
        check("2 entries", len(entries) == 2, f"got {len(entries)}")
        check("first entry from A", entries[0].get("author") == agent_a if entries else False)
        check("first entry text", entries[0].get("text") == "First note from A" if entries else False)
        check("second entry from B", entries[1].get("author") == agent_b if len(entries) > 1 else False)
        check("total is 2", resp.get("total") == 2)

        # --- Test: read with since offset ---
        print("\n[test] Read with offset...")
        resp = get(f"/teams/board?team_name={encoded}&since=1")
        entries = resp.get("entries", [])
        check("1 entry with since=1", len(entries) == 1, f"got {len(entries)}")
        check("entry is from B", entries[0].get("author") == agent_b if entries else False)

        # --- Test: non-member post fails ---
        print("\n[test] Non-member post...")
        code, body = post_expect_error("/teams/board", {
            "team_name": team_name,
            "text": "unauthorized",
            "sender": "fake@agent:999",
        }, 403)
        check("non-member rejected", code == 403, f"code={code}")

        # --- Test: nonexistent team ---
        print("\n[test] Nonexistent team...")
        code, body = post_expect_error("/teams/board", {
            "team_name": "no-such-team",
            "text": "test",
            "sender": agent_a,
        }, 404)
        check("nonexistent team rejected", code == 404, f"code={code}")

    finally:
        print("\n[cleanup]")
        kill_pane(pane_a)
        kill_pane(pane_b)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
