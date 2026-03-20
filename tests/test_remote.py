#!/usr/bin/env python3
"""
test_remote.py — tests for remote agent support (Stage 4).

Prerequisites: docker compose up -d (agentura server running)

Tests:
1. Delegation token CRUD (create, validate, refresh)
2. Sidecar registration creates remote agent
3. Stream push writes to stream file
4. Heartbeat keeps agent alive
5. Message queue: queue + poll + clear
6. Heartbeat timeout removes agent
7. Child-dead heartbeat removes agent + triggers succession
8. Auth middleware accepts delegation tokens for /sidecar/*
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


def get(path, headers=None):
    _ensure_auth()
    h = dict(_auth_headers)
    if headers:
        h.update(headers)
    req = urllib.request.Request(f"{MONITOR_URL}{path}", headers=h)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def post(path, data, headers=None):
    _ensure_auth()
    h = {"Content-Type": "application/json"}
    h.update(_auth_headers)
    if headers:
        h.update(headers)
    req = urllib.request.Request(
        f"{MONITOR_URL}{path}",
        data=json.dumps(data).encode(),
        headers=h,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def post_with_auth(path, data, token):
    """POST with a specific bearer token (delegation or regular)."""
    h = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(
        f"{MONITOR_URL}{path}",
        data=json.dumps(data).encode(),
        headers=h,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def get_with_auth(path, token):
    """GET with a specific bearer token."""
    h = {"Authorization": f"Bearer {token}"}
    req = urllib.request.Request(f"{MONITOR_URL}{path}", headers=h)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def post_no_auth(path, data):
    """POST without any auth header."""
    h = {"Content-Type": "application/json"}
    req = urllib.request.Request(
        f"{MONITOR_URL}{path}",
        data=json.dumps(data).encode(),
        headers=h,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def http_error_code(method, path, data=None, headers=None):
    """Make a request, return HTTP status code (or 200 on success)."""
    _ensure_auth()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    url = f"{MONITOR_URL}{path}"
    req = urllib.request.Request(url, headers=h, method=method)
    if data:
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


# =========================================================================
# Setup: create a local agent to get an agent_token
# =========================================================================

def setup_local_agent():
    """Register a mock local agent and return (agent_id, agent_token)."""
    import platform
    hostname = platform.node()
    cwd = os.getcwd()

    # Launch a mock agent in tmux
    mock_path = os.path.join(os.path.dirname(__file__), "mock_agent.py")
    shell_cmd = f"cd {cwd} && exec python3 {mock_path} 2 100"
    result = subprocess.run(
        ["tmux", "new-window", "-d", "-P", "-F", "#{pane_id}", "-n", "test-remote",
         shell_cmd],
        capture_output=True, text=True, timeout=5,
    )
    pane_id = result.stdout.strip()
    time.sleep(1)

    # Get PID from tmux
    result = subprocess.run(
        ["tmux", "list-panes", "-t", pane_id, "-F", "#{pane_pid}"],
        capture_output=True, text=True, timeout=5,
    )
    pid = int(result.stdout.strip()) if result.stdout.strip() else 99999

    # Register with monitor
    resp = post("/register", {
        "agent_name": "test-remote-local",
        "pane_id": pane_id,
        "pid": pid,
        "hostname": hostname,
        "cwd": cwd,
        "cmd": ["python3", "mock_agent.py"],
    })
    agent_id = f"{hostname}@{cwd}:{pid}"
    agent_token = resp.get("agent_token", "")
    return agent_id, agent_token, pane_id


# =========================================================================
# Tests
# =========================================================================

def test_delegation_tokens():
    """Test delegation token create/validate/refresh lifecycle."""
    print("\n=== Test: Delegation Token Lifecycle ===")

    agent_id, agent_token, pane_id = setup_local_agent()

    # 1. Create delegation token
    resp = post("/api/auth/delegate", {
        "target_host": "test-remote-host",
        "agent_token": agent_token,
    })
    check("delegate creates token", resp.get("status") == "ok",
          f"got: {resp}")
    delegation_token = resp.get("delegation_token", "")
    check("delegation_token is non-empty", len(delegation_token) > 0)
    check("delegate returns TTL", resp.get("expires_in", 0) > 0)

    # 2. Create with invalid agent_token should fail
    code = http_error_code("POST", "/api/auth/delegate",
                           {"target_host": "x", "agent_token": "invalid"})
    check("delegate with bad agent_token returns 401", code == 401,
          f"got {code}")

    # 3. Refresh delegation token
    resp = post_no_auth("/api/auth/delegate-refresh", {
        "delegation_token": delegation_token,
    })
    check("delegate-refresh works", resp.get("status") == "ok",
          f"got: {resp}")
    new_token = resp.get("delegation_token", "")
    check("refresh returns new token", len(new_token) > 0 and new_token != delegation_token)

    # 4. Old token should be invalid after refresh
    code = http_error_code("POST", "/api/auth/delegate-refresh",
                           {"delegation_token": delegation_token})
    check("old delegation token invalid after refresh", code == 401,
          f"got: {code}")

    # 5. Refresh with invalid token should fail
    code = http_error_code("POST", "/api/auth/delegate-refresh",
                           {"delegation_token": "bogus"})
    check("refresh with invalid token returns 401", code == 401,
          f"got {code}")

    # Cleanup
    subprocess.run(["tmux", "kill-pane", "-t", pane_id],
                    capture_output=True, timeout=5)
    return new_token  # return valid token for further tests


def test_sidecar_registration():
    """Test sidecar/register creates a remote agent entry."""
    print("\n=== Test: Sidecar Registration ===")

    agent_id, agent_token, pane_id = setup_local_agent()

    # Create delegation token
    resp = post("/api/auth/delegate", {
        "target_host": "remote-test-box",
        "agent_token": agent_token,
    })
    delegation_token = resp["delegation_token"]

    # Register via sidecar endpoint
    resp = post_with_auth("/sidecar/register", {
        "agent_name": "remote-claude",
        "pane_id": "%99",
        "pid": 12345,
        "hostname": "remote-test-box",
        "cwd": "/home/user/project",
        "cmd": ["claude", "--dangerously-skip-permissions"],
    }, delegation_token)

    check("sidecar register succeeds", resp.get("status") == "ok",
          f"got: {resp}")
    remote_agent_id = resp.get("agent_id", "")
    check("sidecar returns agent_id",
          "remote-test-box@" in remote_agent_id,
          f"got: {remote_agent_id}")

    # Verify agent appears in list
    agents = get("/agents").get("agents", [])
    remote_agents = [a for a in agents if a.get("agent_id") == remote_agent_id]
    check("agent in agent list", len(remote_agents) == 1,
          f"found {len(remote_agents)} matching agents")

    # Cleanup
    subprocess.run(["tmux", "kill-pane", "-t", pane_id],
                    capture_output=True, timeout=5)
    return delegation_token, remote_agent_id


def test_stream_push():
    """Test sidecar/stream-push writes content to stream file."""
    print("\n=== Test: Stream Push ===")

    delegation_token, remote_agent_id = test_sidecar_registration()

    # Push some content
    resp = post_with_auth("/sidecar/stream-push", {
        "agent_id": remote_agent_id,
        "content": "Hello from remote agent!\nLine 2 of output.",
    }, delegation_token)
    check("stream push succeeds", resp.get("status") == "ok",
          f"got: {resp}")

    # Read the stream to verify content
    # First find the pane_id used
    agents = get("/agents").get("agents", [])
    remote = [a for a in agents if a.get("agent_id") == remote_agent_id]
    if remote:
        pane_id = remote[0]["pane_id"]
        import urllib.parse
        encoded_pane = urllib.parse.quote(pane_id, safe="")
        stream_resp = get(f"/stream/{encoded_pane}")
        content = stream_resp.get("content", "")
        check("pushed content in stream", "Hello from remote agent" in content,
              f"content: {content[:200]}")
    else:
        check("pushed content in stream", False, "remote agent not found")


def test_heartbeat():
    """Test sidecar/heartbeat keeps agent alive."""
    print("\n=== Test: Heartbeat ===")

    agent_id, agent_token, pane_id = setup_local_agent()

    # Create delegation token and register remote agent
    resp = post("/api/auth/delegate", {
        "target_host": "hb-test-box",
        "agent_token": agent_token,
    })
    delegation_token = resp["delegation_token"]

    resp = post_with_auth("/sidecar/register", {
        "agent_name": "hb-agent",
        "pane_id": "%98",
        "pid": 11111,
        "hostname": "hb-test-box",
        "cwd": "/tmp",
        "cmd": ["claude"],
    }, delegation_token)
    remote_agent_id = resp.get("agent_id", "")

    # Send heartbeat
    resp = post_with_auth("/sidecar/heartbeat", {
        "agent_id": remote_agent_id,
        "child_alive": True,
    }, delegation_token)
    check("heartbeat succeeds", resp.get("status") == "ok",
          f"got: {resp}")

    # Agent should still be in list
    agents = get("/agents").get("agents", [])
    alive = [a for a in agents if a.get("agent_id") == remote_agent_id]
    check("agent alive after heartbeat", len(alive) == 1)

    # Send heartbeat with child_alive=False
    resp = post_with_auth("/sidecar/heartbeat", {
        "agent_id": remote_agent_id,
        "child_alive": False,
    }, delegation_token)
    check("child-dead heartbeat removes agent",
          resp.get("action") == "removed",
          f"got: {resp}")

    # Agent should be gone
    agents = get("/agents").get("agents", [])
    dead = [a for a in agents if a.get("agent_id") == remote_agent_id]
    check("agent removed after child-dead heartbeat", len(dead) == 0,
          f"found {len(dead)}")

    # Cleanup
    subprocess.run(["tmux", "kill-pane", "-t", pane_id],
                    capture_output=True, timeout=5)


def test_message_queue():
    """Test message queue: queue, poll, clear."""
    print("\n=== Test: Message Queue ===")

    agent_id, agent_token, pane_id = setup_local_agent()

    # Create and register remote agent
    resp = post("/api/auth/delegate", {
        "target_host": "msg-test-box",
        "agent_token": agent_token,
    })
    delegation_token = resp["delegation_token"]

    resp = post_with_auth("/sidecar/register", {
        "agent_name": "msg-agent",
        "pane_id": "%97",
        "pid": 22222,
        "hostname": "msg-test-box",
        "cwd": "/tmp",
        "cmd": ["claude"],
    }, delegation_token)
    remote_agent_id = resp.get("agent_id", "")

    # Queue a message (using bearer auth, not delegation)
    resp = post("/sidecar/queue-message", {
        "agent_id": remote_agent_id,
        "text": "Hello remote agent!",
        "sender": agent_id,
    })
    check("queue message succeeds", resp.get("status") == "ok",
          f"got: {resp}")

    # Queue another message
    post("/sidecar/queue-message", {
        "agent_id": remote_agent_id,
        "text": "Second message",
        "sender": agent_id,
    })

    # Poll messages (sidecar polls with delegation token)
    import urllib.parse
    encoded_id = urllib.parse.quote(remote_agent_id, safe="")
    resp = get_with_auth(f"/sidecar/messages?agent_id={encoded_id}",
                         delegation_token)
    messages = resp.get("messages", [])
    check("poll returns 2 messages", len(messages) == 2,
          f"got {len(messages)}")
    if messages:
        check("first message text correct",
              messages[0].get("text") == "Hello remote agent!",
              f"got: {messages[0].get('text')}")

    # Poll again — queue should be empty
    resp = get_with_auth(f"/sidecar/messages?agent_id={encoded_id}",
                         delegation_token)
    messages = resp.get("messages", [])
    check("queue cleared after poll", len(messages) == 0,
          f"got {len(messages)}")

    # Cleanup
    subprocess.run(["tmux", "kill-pane", "-t", pane_id],
                    capture_output=True, timeout=5)


def test_auth_middleware_sidecar():
    """Test that /sidecar/* endpoints require delegation or bearer tokens."""
    print("\n=== Test: Auth Middleware for Sidecar ===")

    # No auth → 401
    code = http_error_code("POST", "/sidecar/register",
                           {"agent_name": "x", "pane_id": "%0", "pid": 1})
    check("/sidecar/register without auth returns 401", code == 401,
          f"got {code}")

    # Invalid token → 401
    code = http_error_code("POST", "/sidecar/heartbeat",
                           {"agent_id": "x", "child_alive": True},
                           headers={"Authorization": "Bearer invalid_token_here"})
    check("/sidecar/heartbeat with bad token returns 401", code == 401,
          f"got {code}")


def test_queue_message_any_agent():
    """Test that queue-message works for any agent (unified model)."""
    print("\n=== Test: Queue Message Any Agent ===")

    agent_id, agent_token, pane_id = setup_local_agent()

    # Queue message to any agent should succeed
    resp = post("/sidecar/queue-message", {
        "agent_id": agent_id,
        "text": "hello from test",
        "sender": "test",
    })
    check("queue-message to any agent succeeds",
          resp.get("status") == "ok",
          f"got: {resp}")

    # Cleanup
    subprocess.run(["tmux", "kill-pane", "-t", pane_id],
                    capture_output=True, timeout=5)


# =========================================================================
# Main
# =========================================================================

if __name__ == "__main__":
    print("Remote Agent Tests")
    print("=" * 60)

    try:
        test_delegation_tokens()
        test_sidecar_registration()
        test_stream_push()
        test_heartbeat()
        test_message_queue()
        test_auth_middleware_sidecar()
        test_queue_message_any_agent()
    except Exception as e:
        print(f"\n  FATAL: {e}")
        import traceback
        traceback.print_exc()
        failed += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
