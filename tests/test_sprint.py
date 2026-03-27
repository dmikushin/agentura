#!/usr/bin/env python3
"""
test_sprint.py — tests for timezone endpoint and sprint bookkeeping.

Prerequisites: docker compose up -d (agentura server running)
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

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
    with urllib.request.urlopen(req, timeout=10) as resp:
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
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def main():
    global passed, failed

    print("=== test_sprint.py ===\n")

    try:
        get("/agents")
    except urllib.error.URLError:
        print("ERROR: agentura server not running. Start with: docker compose up -d")
        sys.exit(1)

    # --- Test: /timezone endpoint ---
    print("[test] Timezone endpoint...")
    resp = get("/timezone")
    check("timezone returns ok", resp.get("status") == "ok", str(resp))
    check("timezone has value", resp.get("timezone", "") != "", f"got: {resp}")
    tz = resp.get("timezone", "")
    check("timezone is valid string", "/" in tz or tz == "UTC", f"got: {tz}")
    print(f"  (server timezone: {tz})")

    # --- Test: /sprint GET before any sprint set ---
    print("\n[test] Sprint GET (no sprint)...")
    team_name = f"sprint-test-{int(time.time())}"
    encoded = urllib.parse.quote(team_name, safe="")
    resp = get(f"/sprint?team_name={encoded}")
    check("sprint get returns ok", resp.get("status") == "ok", str(resp))
    check("no sprint initially", resp.get("sprint") is None, str(resp))

    # --- Test: /sprint GET without team_name ---
    print("\n[test] Sprint GET (no team_name)...")
    resp = get("/sprint?team_name=")
    check("no sprint for empty team", resp.get("sprint") is None, str(resp))

    # --- Test: /sprint POST (set sprint) ---
    print("\n[test] Sprint POST (set sprint)...")
    resp = post("/sprint", {"team_name": team_name, "duration_sec": 1800})
    check("sprint set returns ok", resp.get("status") == "ok", str(resp))
    sprint = resp.get("sprint", {})
    check("sprint has start", "start" in sprint, str(sprint))
    check("sprint has duration", sprint.get("duration_sec") == 1800, str(sprint))
    check("sprint start is recent",
          abs(sprint.get("start", 0) - time.time()) < 5,
          f"start={sprint.get('start')}, now={time.time()}")

    # --- Test: /sprint GET (after set) ---
    print("\n[test] Sprint GET (after set)...")
    resp = get(f"/sprint?team_name={encoded}")
    check("sprint get returns ok", resp.get("status") == "ok", str(resp))
    sprint = resp.get("sprint")
    check("sprint exists", sprint is not None, str(resp))
    if sprint:
        check("sprint start matches", abs(sprint["start"] - time.time()) < 10)
        check("sprint duration matches", sprint["duration_sec"] == 1800)

    # --- Test: /sprint POST (overwrite) ---
    print("\n[test] Sprint POST (overwrite)...")
    resp = post("/sprint", {"team_name": team_name, "duration_sec": 900})
    check("overwrite returns ok", resp.get("status") == "ok", str(resp))
    resp = get(f"/sprint?team_name={encoded}")
    sprint = resp.get("sprint")
    check("duration updated", sprint and sprint["duration_sec"] == 900, str(sprint))

    # --- Test: /sprint POST (default duration) ---
    print("\n[test] Sprint POST (default duration)...")
    resp = post("/sprint", {"team_name": f"{team_name}-default"})
    sprint = resp.get("sprint", {})
    check("default duration is 1800", sprint.get("duration_sec") == 1800, str(sprint))

    # --- Test: /sprint POST (missing team_name) ---
    print("\n[test] Sprint POST (missing team_name)...")
    try:
        resp = post("/sprint", {"duration_sec": 600})
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    check("missing team_name returns 400", code == 400, f"code={code}")

    # --- Test: different teams have independent sprints ---
    print("\n[test] Independent team sprints...")
    team_a = f"sprint-a-{int(time.time())}"
    team_b = f"sprint-b-{int(time.time())}"
    post("/sprint", {"team_name": team_a, "duration_sec": 600})
    post("/sprint", {"team_name": team_b, "duration_sec": 3600})
    resp_a = get(f"/sprint?team_name={urllib.parse.quote(team_a, safe='')}")
    resp_b = get(f"/sprint?team_name={urllib.parse.quote(team_b, safe='')}")
    check("team A has 600s sprint",
          resp_a.get("sprint", {}).get("duration_sec") == 600)
    check("team B has 3600s sprint",
          resp_b.get("sprint", {}).get("duration_sec") == 3600)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
