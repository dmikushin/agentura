#!/usr/bin/env python3
"""
test_read_stream.py — end-to-end test for cursor-based stream reading.

Prerequisites: docker compose up -d (agentura server running)

Steps:
1. Launch mock_agent via agent-run in a new tmux window
2. Wait for output to accumulate
3. read_stream → get initial content (offset 0 → N)
4. Wait for more output
5. read_stream → get ONLY new content (offset N → M)
6. read_stream immediately → "(no new content)"
7. Kill mock agent
8. Verify agent exit detected
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from urllib.error import URLError, HTTPError

# Add parent dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

MONITOR_URL = os.environ["AGENTURA_URL"]
MOCK_AGENT = os.path.join(os.path.dirname(__file__), "mock_agent.py")
AGENT_RUN = "agentura-run"

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


def main():
    global passed, failed

    print("=== test_read_stream.py ===\n")

    # 0. Check monitor is running
    try:
        get("/agents")
    except (URLError, HTTPError):
        print("ERROR: agentura server is not running. Start it with: docker compose up -d")
        sys.exit(1)

    # 1. Launch mock agent (fast: 1s interval, 15 lines)
    print("[setup] Launching mock agent...")
    result = subprocess.run(
        ["tmux", "new-window", "-P", "-F", "#{pane_id}", "-n", "mock",
         f"cd /tmp && AGENTURA_URL={MONITOR_URL} exec {AGENT_RUN} python3 {MOCK_AGENT} 1 15"],
        capture_output=True, text=True, timeout=5,
    )
    pane_id = result.stdout.strip()
    check("tmux window created", result.returncode == 0 and pane_id, result.stderr)

    # 2. Wait for agent to register
    print("[setup] Waiting for registration...")
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

    check("agent registered", agent_id is not None)
    if not agent_id:
        print("ABORT: agent did not register")
        subprocess.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True)
        sys.exit(1)

    print(f"[setup] Agent: {agent_id}\n")

    # 3. Wait for some output to accumulate
    print("[test] Waiting 5s for output...")
    time.sleep(5)

    # 4. First read — should get content, offset advances
    import urllib.parse
    encoded_pane = urllib.parse.quote(pane_id, safe="")
    resp1 = get(f"/stream/{encoded_pane}?offset=0")
    content1 = resp1.get("content", "")
    offset1 = resp1.get("next_offset", 0)

    check("first read has content", len(content1) > 0, f"got {len(content1)} chars")
    check("first read offset > 0", offset1 > 0, f"offset={offset1}")
    has_mock_lines = "[mock] Line" in content1
    check("content contains mock output", has_mock_lines, content1[:100])

    # 5. Wait for more output
    print("[test] Waiting 4s for more output...")
    time.sleep(4)

    # 6. Second read from previous offset — only new content
    resp2 = get(f"/stream/{encoded_pane}?offset={offset1}")
    content2 = resp2.get("content", "")
    offset2 = resp2.get("next_offset", offset1)

    check("second read has new content", len(content2) > 0, f"got {len(content2)} chars")
    check("offset advanced", offset2 > offset1, f"{offset1} → {offset2}")
    check("no overlap with first read", content2 not in content1 or len(content2) < 20)

    # 7. Immediate third read — no new content
    resp3 = get(f"/stream/{encoded_pane}?offset={offset2}")
    content3 = resp3.get("content", "")

    check("immediate re-read is empty", content3.strip() == "", f"got: {content3[:80]!r}")

    # 8. Kill mock agent
    print("\n[test] Killing mock agent...")
    # Send C-c to stop mock_agent, then kill the pane.
    # Go sidecar catches SIGHUP on kill-pane → sends death heartbeat.
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "C-c"], capture_output=True)
    time.sleep(2)
    subprocess.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True)

    # 9. Wait for monitor to detect exit
    # Go sidecar: child dies → next loop iteration sends child_alive=false heartbeat,
    # or SIGHUP signal handler sends final heartbeat on kill-pane.
    # Server removes agent immediately on child_alive=false.
    # If signal is missed, server heartbeat timeout is 30s.
    agent_gone = False
    for _ in range(15):
        time.sleep(2)
        data = get("/agents")
        if all(a.get("pane_id") != pane_id for a in data.get("agents", [])):
            agent_gone = True
            break
    check("agent removed after exit", agent_gone)

    # 10. Read final content — agent is removed from registry so HTTP 404.
    # Check the stream file inside the container for the exit marker.
    result = subprocess.run(
        ["docker", "exec", "agentura-server", "sh", "-c",
         "cat /data/streams/*python3-*.md 2>/dev/null || echo ''"],
        capture_output=True, text=True, timeout=5,
    )
    full_stream = result.stdout
    check("stream has exit marker", "*Agent exited" in full_stream, full_stream[-200:])

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
