#!/usr/bin/env python3
"""
test_teams_auth.py — tests for secure team join protocol (Stage 2).

Prerequisites: docker compose up -d (agentura server running)

Tests:
1. Registration returns agent_token
2. agent_token can create a team (owner verified via token)
3. create_team without agent_token fails
4. request-join puts agent in pending
5. owner can approve pending request
6. owner can deny pending request
7. non-owner cannot approve/deny
8. old /teams/join returns 410 Gone
9. succession notification on owner exit
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
    """POST expecting an HTTP error. Returns (code, body_dict)."""
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
            body = json.loads(resp.read().decode())
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode()) if e.readable() else {}
        return e.code, body


def capture_pane(pane_id, lines=30):
    result = subprocess.run(
        ["tmux", "capture-pane", "-pt", pane_id, "-S", f"-{lines}"],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout if result.returncode == 0 else ""


def launch_mock_agent(name, cwd="/tmp"):
    """Launch cat as a mock agent via agent-run."""
    result = subprocess.run(
        ["tmux", "new-window", "-P", "-F", "#{pane_id}", "-n", name,
         f"cd {cwd} && AGENTURA_URL={MONITOR_URL} exec {AGENT_RUN} cat"],
        capture_output=True, text=True, timeout=5,
    )
    pane_id = result.stdout.strip()
    if result.returncode != 0 or not pane_id:
        return None, None, None

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

    # Get agent_token by refreshing
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

    print("=== test_teams_auth.py ===\n")

    # 0. Check monitor
    try:
        get("/agents")
    except urllib.error.URLError:
        print("ERROR: agentura server not running. Start with: docker compose up -d")
        sys.exit(1)

    pane_a = pane_b = pane_c = pane_d = None
    agent_a = agent_b = agent_c = agent_d = None
    token_a = token_b = token_c = token_d = None
    team_name = f"test-team-{int(time.time())}"

    try:
        # --- Setup: launch agents ---
        print("[setup] Launching agent A...")
        pane_a, agent_a, token_a = launch_mock_agent("auth-a")
        check("agent A registered", agent_a is not None)
        check("agent A has agent_token", token_a is not None)

        print("[setup] Launching agent B...")
        pane_b, agent_b, token_b = launch_mock_agent("auth-b")
        check("agent B registered", agent_b is not None)
        check("agent B has agent_token", token_b is not None)

        if not all([agent_a, agent_b, token_a, token_b]):
            print("ABORT: agents failed to register with tokens")
            sys.exit(1)

        print(f"[setup] A: {agent_a}  B: {agent_b}")

        # --- Test: create team with agent_token ---
        print("\n[test] Create team with agent_token...")
        resp = post("/teams", {"name": team_name, "agent_token": token_a})
        check("create_team succeeds", resp.get("status") == "ok", str(resp))
        team_data = resp.get("team", {})
        check("owner is agent A", team_data.get("owner") == agent_a,
              f"owner={team_data.get('owner')}")
        check("team has pending field", "pending" in team_data, str(team_data))

        # --- Test: create team without agent_token fails ---
        print("\n[test] Create team without agent_token...")
        code, body = post_expect_error("/teams", {"name": "bad-team", "agent_token": "invalid-token"}, 401)
        check("invalid agent_token rejected", code == 401, f"code={code}")

        # --- Test: old /teams/join returns 410 ---
        print("\n[test] Old /teams/join is gone...")
        code, body = post_expect_error("/teams/join", {"team": team_name, "agent_id": agent_b}, 410)
        check("/teams/join returns 410", code == 410, f"code={code}")

        # --- Test: request-join ---
        print("\n[test] Request join...")
        resp = post("/teams/request-join", {
            "team": team_name,
            "agent_token": token_b,
            "message": "I want to collaborate",
        })
        check("request-join succeeds", resp.get("status") == "ok", str(resp))

        # Check pending
        resp = get(f"/teams/{team_name}/pending")
        pending = resp.get("pending", {})
        check("B is in pending", agent_b in pending, str(pending))
        check("pending has message", pending.get(agent_b, {}).get("message") == "I want to collaborate",
              str(pending))

        # Check B is NOT yet a member
        resp = get("/teams")
        for t in resp.get("teams", []):
            if t["name"] == team_name:
                check("B not yet in members", agent_b not in t["members"], str(t["members"]))
                break

        # Check owner got notification
        time.sleep(3)  # wait for sidecar poll
        pane_content_a = capture_pane(pane_a)
        check("owner sees [SYSTEM] notification",
              "[SYSTEM]" in pane_content_a and "Join request" in pane_content_a,
              pane_content_a[-200:])

        # --- Test: non-owner cannot approve ---
        print("\n[test] Non-owner cannot approve...")
        # Launch agent C
        print("[setup] Launching agent C...")
        pane_c, agent_c, token_c = launch_mock_agent("auth-c")
        check("agent C registered", agent_c is not None)

        if token_c:
            code, body = post_expect_error("/teams/approve", {
                "team": team_name,
                "pending_agent_id": agent_b,
                "agent_token": token_c,
            }, 403)
            check("non-owner approve rejected (403)", code == 403, f"code={code}")

        # --- Test: owner approves ---
        print("\n[test] Owner approves...")
        resp = post("/teams/approve", {
            "team": team_name,
            "pending_agent_id": agent_b,
            "agent_token": token_a,
        })
        check("approve succeeds", resp.get("status") == "ok", str(resp))

        # Check B is now a member
        resp = get("/teams")
        for t in resp.get("teams", []):
            if t["name"] == team_name:
                check("B is now in members", agent_b in t["members"], str(t["members"]))
                break

        # Check pending is empty
        resp = get(f"/teams/{team_name}/pending")
        check("pending is empty after approve", len(resp.get("pending", {})) == 0,
              str(resp.get("pending")))

        # Check B got approval notification
        time.sleep(3)  # wait for sidecar poll
        pane_content_b = capture_pane(pane_b)
        check("B sees APPROVED notification",
              "[SYSTEM]" in pane_content_b and "APPROVED" in pane_content_b,
              pane_content_b[-200:])

        # --- Test: deny flow ---
        print("\n[test] Deny flow...")
        if token_c:
            # C requests to join
            resp = post("/teams/request-join", {
                "team": team_name,
                "agent_token": token_c,
                "message": "Can I join?",
            })
            check("C request-join succeeds", resp.get("status") == "ok", str(resp))

            # A denies C
            resp = post("/teams/deny", {
                "team": team_name,
                "pending_agent_id": agent_c,
                "agent_token": token_a,
            })
            check("deny succeeds", resp.get("status") == "ok", str(resp))

            # Check C is not a member
            resp = get("/teams")
            for t in resp.get("teams", []):
                if t["name"] == team_name:
                    check("C not in members after deny", agent_c not in t["members"],
                          str(t["members"]))
                    break

            # Check C got denial notification
            time.sleep(3)  # wait for sidecar poll
            pane_content_c = capture_pane(pane_c)
            check("C sees DENIED notification",
                  "[SYSTEM]" in pane_content_c and "DENIED" in pane_content_c,
                  pane_content_c[-200:])

        # --- Test: admin delegation ---
        print("\n[test] Admin delegation...")
        # A adds B as admin
        resp = post("/teams/add-admin", {
            "team": team_name,
            "admin_agent_id": agent_b,
            "agent_token": token_a,
        })
        check("add_admin succeeds", resp.get("status") == "ok", str(resp))

        # Check B got admin notification
        time.sleep(3)  # wait for sidecar poll
        pane_content_b = capture_pane(pane_b)
        check("B sees ADMIN notification",
              "[SYSTEM]" in pane_content_b and "ADMIN:" in pane_content_b,
              pane_content_b[-200:])

        # Check admins field in team
        resp = get("/teams")
        for t in resp.get("teams", []):
            if t["name"] == team_name:
                check("B is in admins", agent_b in t.get("admins", []), str(t.get("admins")))
                check("last_owner_activity present", "last_owner_activity" in t, str(t))
                break

        # --- Test: admin can approve join request ---
        print("\n[test] Admin approves join request...")
        if token_c:
            # C requests to join (resubmit after earlier deny)
            resp = post("/teams/request-join", {
                "team": team_name,
                "agent_token": token_c,
                "message": "Second attempt",
            })
            check("C re-request-join succeeds", resp.get("status") == "ok", str(resp))

            # B (admin) approves C
            resp = post("/teams/approve", {
                "team": team_name,
                "pending_agent_id": agent_c,
                "agent_token": token_b,
            })
            check("admin B can approve", resp.get("status") == "ok", str(resp))

            # Check C is now a member
            resp = get("/teams")
            for t in resp.get("teams", []):
                if t["name"] == team_name:
                    check("C is now a member (approved by admin)", agent_c in t["members"],
                          str(t["members"]))
                    break

        # --- Test: owner cannot be added as admin ---
        print("\n[test] Owner cannot be admin...")
        code, body = post_expect_error("/teams/add-admin", {
            "team": team_name,
            "admin_agent_id": agent_a,
            "agent_token": token_a,
        }, 400)
        check("owner as admin rejected (400)", code == 400, f"code={code}")

        # --- Test: non-member cannot be admin ---
        print("\n[test] Non-member cannot be admin...")
        pane_d, agent_d, token_d = launch_mock_agent("auth-d")
        check("agent D registered", agent_d is not None)
        if agent_d:
            code, body = post_expect_error("/teams/add-admin", {
                "team": team_name,
                "admin_agent_id": agent_d,
                "agent_token": token_a,
            }, 400)
            check("non-member as admin rejected (400)", code == 400, f"code={code}")

        # --- Test: remove_admin ---
        print("\n[test] Remove admin...")
        resp = post("/teams/remove-admin", {
            "team": team_name,
            "admin_agent_id": agent_b,
            "agent_token": token_a,
        })
        check("remove_admin succeeds", resp.get("status") == "ok", str(resp))

        # Check B got admin revoked notification
        time.sleep(3)  # wait for sidecar poll
        pane_content_b = capture_pane(pane_b)
        check("B sees ADMIN_REVOKED notification",
              "[SYSTEM]" in pane_content_b and "ADMIN_REVOKED" in pane_content_b,
              pane_content_b[-200:])

        # Verify B is no longer in admins
        resp = get("/teams")
        for t in resp.get("teams", []):
            if t["name"] == team_name:
                check("B no longer in admins", agent_b not in t.get("admins", []),
                      str(t.get("admins")))
                break

        # --- Test: transfer ownership ---
        print("\n[test] Transfer ownership...")
        # Re-add B as admin first (to test transfer removes from admins)
        post("/teams/add-admin", {
            "team": team_name,
            "admin_agent_id": agent_b,
            "agent_token": token_a,
        })
        # Transfer from A to B
        resp = post("/teams/transfer", {
            "team": team_name,
            "new_owner": agent_b,
            "agent_token": token_a,
        })
        check("transfer succeeds", resp.get("status") == "ok", str(resp))

        # Verify new owner
        resp = get("/teams")
        for t in resp.get("teams", []):
            if t["name"] == team_name:
                check("B is now owner after transfer", t["owner"] == agent_b,
                      f"owner={t.get('owner')}")
                # B should be removed from admins when becoming owner
                check("B removed from admins on transfer", agent_b not in t.get("admins", []),
                      str(t.get("admins")))
                break

        # Check all members got TRANSFER notification
        time.sleep(3)  # wait for sidecar poll
        pane_content_a = capture_pane(pane_a)
        check("A sees TRANSFER notification",
              "[SYSTEM]" in pane_content_a and "TRANSFER" in pane_content_a,
              pane_content_a[-200:])

        # --- Test: non-owner cannot transfer ---
        print("\n[test] Non-owner cannot transfer...")
        code, body = post_expect_error("/teams/transfer", {
            "team": team_name,
            "new_owner": agent_a,
            "agent_token": token_a,  # A is no longer owner
        }, 403)
        check("non-owner transfer rejected (403)", code == 403, f"code={code}")

        # --- Test: leave_team (regular member) ---
        print("\n[test] leave_team (regular member)...")
        if token_d and agent_d:
            # First add D to the team
            resp = post("/teams/request-join", {
                "team": team_name,
                "agent_token": token_d,
            })
            # B (owner) approves
            resp = post("/teams/approve", {
                "team": team_name,
                "pending_agent_id": agent_d,
                "agent_token": token_b,
            })
            check("D joined team", resp.get("status") == "ok", str(resp))

            # D leaves
            resp = post("/teams/leave", {
                "team": team_name,
                "agent_token": token_d,
            })
            check("D leave succeeds", resp.get("status") == "ok", str(resp))
            check("D leave no succession", resp.get("succession") is False,
                  f"succession={resp.get('succession')}")

            # Verify D is no longer a member
            resp = get("/teams")
            for t in resp.get("teams", []):
                if t["name"] == team_name:
                    check("D not in members after leave", agent_d not in t["members"],
                          str(t["members"]))
                    break

        # --- Test: leave_team (owner triggers succession) ---
        print("\n[test] leave_team (owner triggers succession)...")
        # B is owner, B leaves → succession to next member
        resp_before = get("/teams")
        next_owner = None
        for t in resp_before.get("teams", []):
            if t["name"] == team_name:
                for m in t["members"]:
                    if m != agent_b:
                        next_owner = m
                        break
                break

        resp = post("/teams/leave", {
            "team": team_name,
            "agent_token": token_b,
        })
        check("B leave succeeds", resp.get("status") == "ok", str(resp))
        check("B leave triggers succession", resp.get("succession") is True,
              f"succession={resp.get('succession')}")

        # Verify new owner
        resp = get("/teams")
        for t in resp.get("teams", []):
            if t["name"] == team_name:
                check("succession after owner leave", t["owner"] == next_owner,
                      f"owner={t.get('owner')}, expected={next_owner}")
                break

        # --- Test: force-succession ---
        print("\n[test] Force-succession...")
        # Setup: current owner is next_owner (A or C), make A admin if possible
        # Let's use the current state. We need an admin.
        current_team = None
        resp = get("/teams")
        for t in resp.get("teams", []):
            if t["name"] == team_name:
                current_team = t
                break

        if current_team and len(current_team["members"]) >= 2:
            current_owner_id = current_team["owner"]
            # Find a member that is not the owner and has a token
            admin_candidate = None
            admin_token = None
            for m in current_team["members"]:
                if m != current_owner_id:
                    # Find their token
                    for aid, tok in [(agent_a, token_a), (agent_b, token_b), (agent_c, token_c)]:
                        if aid == m and tok:
                            admin_candidate = m
                            admin_token = tok
                            break
                    if admin_candidate:
                        break

            if admin_candidate and admin_token:
                # Find the owner's token
                owner_token = None
                for aid, tok in [(agent_a, token_a), (agent_b, token_b), (agent_c, token_c)]:
                    if aid == current_owner_id and tok:
                        owner_token = tok
                        break

                if owner_token:
                    # Add admin
                    resp = post("/teams/add-admin", {
                        "team": team_name,
                        "admin_agent_id": admin_candidate,
                        "agent_token": owner_token,
                    })
                    check("admin added for force-succession test", resp.get("status") == "ok", str(resp))

                    # Non-admin cannot force-succession
                    print("\n[test] Non-admin cannot force-succession...")
                    # Find a non-admin member
                    non_admin = None
                    non_admin_token = None
                    for m in current_team["members"]:
                        if m != current_owner_id and m != admin_candidate:
                            for aid, tok in [(agent_a, token_a), (agent_b, token_b), (agent_c, token_c)]:
                                if aid == m and tok:
                                    non_admin = m
                                    non_admin_token = tok
                                    break
                            if non_admin:
                                break

                    if non_admin_token:
                        code, body = post_expect_error("/teams/force-succession", {
                            "team": team_name,
                            "agent_token": non_admin_token,
                            "reason": "testing",
                        }, 403)
                        check("non-admin force-succession rejected (403)", code == 403, f"code={code}")

                    # --- Test: force-succession cancel (owner acts) ---
                    print("\n[test] Force-succession cancel (owner responds)...")
                    resp = post("/teams/force-succession", {
                        "team": team_name,
                        "agent_token": admin_token,
                        "reason": "owner seems stuck",
                    })
                    check("force-succession request accepted", resp.get("status") == "ok", str(resp))

                    # Owner acts (any successful team action cancels it)
                    # Remove then re-add admin to guarantee a successful action
                    post("/teams/remove-admin", {
                        "team": team_name,
                        "admin_agent_id": admin_candidate,
                        "agent_token": owner_token,
                    })
                    resp = post("/teams/add-admin", {
                        "team": team_name,
                        "admin_agent_id": admin_candidate,
                        "agent_token": owner_token,
                    })

                    # Check force_succession_pending is cleared
                    resp = get("/teams")
                    for t in resp.get("teams", []):
                        if t["name"] == team_name:
                            check("force_succession_pending cleared after owner action",
                                  t.get("force_succession_pending") is None,
                                  str(t.get("force_succession_pending")))
                            break

        # --- Test: backward compatibility (legacy team loading) ---
        print("\n[test] Backward compatibility...")
        # This is implicitly tested — load_from_disk backfills admins and last_owner_activity
        # We verify that existing teams have these fields
        resp = get("/teams")
        for t in resp.get("teams", []):
            if t["name"] == team_name:
                check("team has admins field", "admins" in t, str(t))
                check("team has last_owner_activity field", "last_owner_activity" in t, str(t))
                break

        # --- Test: succession on owner exit (original test) ---
        print("\n[test] Succession on owner exit...")
        # Get current state
        resp = get("/teams")
        current_owner_pane = None
        for t in resp.get("teams", []):
            if t["name"] == team_name:
                current_owner_id = t["owner"]
                # Find the pane for the current owner
                for pid, aid in [(pane_a, agent_a), (pane_b, agent_b), (pane_c, agent_c)]:
                    if aid == current_owner_id and pid:
                        current_owner_pane = pid
                        break
                break

        if current_owner_pane:
            kill_pane(current_owner_pane)
            # Prevent double-kill in finally
            if current_owner_pane == pane_a:
                pane_a = None
            elif current_owner_pane == pane_b:
                pane_b = None
            elif current_owner_pane == pane_c:
                pane_c = None

            # Wait for sidecar death heartbeat and server processing
            time.sleep(8)

            resp = get("/teams")
            found_team = False
            for t in resp.get("teams", []):
                if t["name"] == team_name:
                    found_team = True
                    check("owner changed after exit", t["owner"] != current_owner_id,
                          f"owner={t.get('owner')}")
                    break
            check("team still exists after owner exit", found_team)

    finally:
        print("\n[cleanup]")
        kill_pane(pane_a)
        kill_pane(pane_b)
        kill_pane(pane_c)
        kill_pane(pane_d)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
