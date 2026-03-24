#!/usr/bin/env python3
"""
test_read_stream.py — end-to-end test for cursor-based stream reading.

Prerequisites: docker compose up -d (agentura server running)
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import wait_for_file, wait_for_pane_contains, wait_for_agent_gone, capture_pane

MONITOR_URL = os.environ["AGENTURA_URL"]
MOCK_AGENT = os.path.join(os.path.dirname(__file__), "mock_agent.py")

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

    try:
        get("/agents")
    except (urllib.error.URLError, urllib.error.HTTPError):
        print("ERROR: agentura server is not running. Start it with: docker compose up -d")
        sys.exit(1)

    # 1. Launch mock agent via ready-file synchronization
    print("[setup] Launching mock agent...")
    ready_file = tempfile.mktemp(prefix="agentura-ready-", suffix=".txt")
    result = subprocess.run(
        ["tmux", "new-window", "-P", "-F", "#{pane_id}", "-n", "mock",
         f"cd /tmp && AGENTURA_URL={MONITOR_URL} AGENTURA_READY_FILE={ready_file} "
         f"exec agentura-run python3 {MOCK_AGENT} 1 15"],
        capture_output=True, text=True, timeout=5,
    )
    pane_id = result.stdout.strip()
    check("tmux window created", result.returncode == 0 and pane_id, result.stderr)

    # 2. Wait for registration via ready-file
    print("[setup] Waiting for registration...")
    agent_id = wait_for_file(ready_file, timeout=15)
    try:
        os.unlink(ready_file)
    except FileNotFoundError:
        pass

    check("agent registered", agent_id is not None)
    if not agent_id:
        print("ABORT: agent did not register")
        subprocess.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True)
        sys.exit(1)

    print(f"[setup] Agent: {agent_id}\n")

    # 3. Wait for mock output to appear in stream (poll instead of sleep)
    print("[test] Waiting for output...")
    encoded_pane = urllib.parse.quote(pane_id, safe="")
    content1 = ""
    offset1 = 0
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        resp1 = get(f"/stream/{encoded_pane}?offset=0")
        content1 = resp1.get("content", "")
        offset1 = resp1.get("next_offset", 0)
        if "[mock] Line" in content1:
            break
        time.sleep(0.5)

    check("first read has content", len(content1) > 0, f"got {len(content1)} chars")
    check("first read offset > 0", offset1 > 0, f"offset={offset1}")
    check("content contains mock output", "[mock] Line" in content1, content1[:100])

    # 4. Wait for MORE output to appear beyond current offset
    print("[test] Waiting for more output...")
    content2 = ""
    offset2 = offset1
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        resp2 = get(f"/stream/{encoded_pane}?offset={offset1}")
        content2 = resp2.get("content", "")
        offset2 = resp2.get("next_offset", offset1)
        if len(content2) > 0 and offset2 > offset1:
            break
        time.sleep(0.5)

    check("second read has new content", len(content2) > 0, f"got {len(content2)} chars")
    check("offset advanced", offset2 > offset1, f"{offset1} → {offset2}")
    check("no overlap with first read", content2 not in content1 or len(content2) < 20)

    # 5. Immediate re-read — no new content
    resp3 = get(f"/stream/{encoded_pane}?offset={offset2}")
    content3 = resp3.get("content", "")
    check("immediate re-read is empty", content3.strip() == "", f"got: {content3[:80]!r}")

    # 6. Kill mock agent
    print("\n[test] Killing mock agent...")
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "C-c"], capture_output=True)
    time.sleep(1)
    subprocess.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True)

    # 7. Wait for agent removal (poll)
    agent_gone = wait_for_agent_gone(get, pane_id)
    check("agent removed after exit", agent_gone)

    # 8. Check stream file for exit marker
    result = subprocess.run(
        ["docker", "exec", "agentura-server", "sh", "-c",
         "cat /data/streams/*python3-*.md 2>/dev/null || echo ''"],
        capture_output=True, text=True, timeout=5,
    )
    check("stream has exit marker", "*Agent exited" in result.stdout or "*Agent heartbeat timeout" in result.stdout,
          result.stdout[-200:])

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
