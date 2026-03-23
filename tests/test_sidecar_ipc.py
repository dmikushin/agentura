#!/usr/bin/env python3
"""
test_sidecar_ipc.py — unit tests for sidecar IPC (Unix socket protocol).

Tests the SidecarListener + sidecar_request() pair in isolation,
without needing the full agentura server.
"""

import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sidecar_ipc import SidecarListener, sidecar_request, SidecarUnavailable

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


def test_basic_get():
    """Test basic GET request through IPC."""
    print("\n=== Test: Basic GET ===")

    sock_path = tempfile.mktemp(suffix=".sock")
    listener = SidecarListener(sock_path)

    def proxy_fn(method, path, data):
        return {"agents": [{"name": "test"}], "method_was": method, "path_was": path}

    # Run listener in background thread
    def serve():
        listener.process_pending(proxy_fn, timeout=5)

    t = threading.Thread(target=serve, daemon=True)
    t.start()

    # Give listener time to start
    time.sleep(0.1)

    result = sidecar_request(sock_path, "GET", "/agents")
    check("GET returns body", "agents" in result, f"got: {result}")
    check("method preserved", result.get("method_was") == "GET")
    check("path preserved", result.get("path_was") == "/agents")

    listener.close()
    t.join(timeout=2)


def test_basic_post():
    """Test basic POST request with data through IPC."""
    print("\n=== Test: Basic POST ===")

    sock_path = tempfile.mktemp(suffix=".sock")
    listener = SidecarListener(sock_path)

    received = {}

    def proxy_fn(method, path, data):
        received["method"] = method
        received["path"] = path
        received["data"] = data
        return {"status": "ok"}

    def serve():
        listener.process_pending(proxy_fn, timeout=5)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    time.sleep(0.1)

    result = sidecar_request(sock_path, "POST", "/teams", {"name": "test-team"})
    check("POST returns body", result.get("status") == "ok", f"got: {result}")
    check("method is POST", received.get("method") == "POST")
    check("data received", received.get("data", {}).get("name") == "test-team",
          f"got: {received.get('data')}")

    listener.close()
    t.join(timeout=2)


def test_inject_agent_token():
    """Test _inject_agent_token replacement."""
    print("\n=== Test: Inject Agent Token ===")

    sock_path = tempfile.mktemp(suffix=".sock")
    listener = SidecarListener(sock_path)

    received_data = {}

    # Simulate sidecar's _proxy which handles _inject_agent_token
    def proxy_fn(method, path, data):
        if data and data.pop("_inject_agent_token", False):
            data["agent_token"] = "REAL_TOKEN_123"
        received_data.update(data or {})
        return {"status": "ok"}

    def serve():
        listener.process_pending(proxy_fn, timeout=5)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    time.sleep(0.1)

    result = sidecar_request(sock_path, "POST", "/teams",
                             {"name": "my-team", "_inject_agent_token": True})
    check("token injected", received_data.get("agent_token") == "REAL_TOKEN_123",
          f"got: {received_data}")
    check("flag removed", "_inject_agent_token" not in received_data,
          f"got: {received_data}")
    check("other data preserved", received_data.get("name") == "my-team")

    listener.close()
    t.join(timeout=2)


def test_unavailable_socket():
    """Test SidecarUnavailable when socket doesn't exist."""
    print("\n=== Test: Unavailable Socket ===")

    try:
        sidecar_request("/tmp/nonexistent-agentura.sock", "GET", "/agents")
        check("raises on missing socket", False, "no exception raised")
    except SidecarUnavailable:
        check("raises on missing socket", True)


def test_multiple_requests():
    """Test multiple sequential requests."""
    print("\n=== Test: Multiple Requests ===")

    sock_path = tempfile.mktemp(suffix=".sock")
    listener = SidecarListener(sock_path)
    call_count = [0]

    def proxy_fn(method, path, data):
        call_count[0] += 1
        return {"call": call_count[0]}

    def serve():
        for _ in range(3):
            listener.process_pending(proxy_fn, timeout=2)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    time.sleep(0.1)

    r1 = sidecar_request(sock_path, "GET", "/agents")
    time.sleep(0.1)
    r2 = sidecar_request(sock_path, "GET", "/teams")
    time.sleep(0.1)
    r3 = sidecar_request(sock_path, "POST", "/teams", {"name": "x"})

    check("first call", r1.get("call") == 1, f"got: {r1}")
    check("second call", r2.get("call") == 2, f"got: {r2}")
    check("third call", r3.get("call") == 3, f"got: {r3}")

    listener.close()
    t.join(timeout=2)


def test_cleanup():
    """Test socket file is cleaned up on close."""
    print("\n=== Test: Cleanup ===")

    sock_path = tempfile.mktemp(suffix=".sock")
    listener = SidecarListener(sock_path)
    check("socket file created", os.path.exists(sock_path))
    listener.close()
    check("socket file removed", not os.path.exists(sock_path))


if __name__ == "__main__":
    print("Sidecar IPC Tests")
    print("=" * 60)

    try:
        test_basic_get()
        test_basic_post()
        test_inject_agent_token()
        test_unavailable_socket()
        test_multiple_requests()
        test_cleanup()
    except Exception as e:
        print(f"\n  FATAL: {e}")
        import traceback
        traceback.print_exc()
        failed += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
