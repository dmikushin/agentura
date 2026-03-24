"""Shared test helpers — synchronization primitives for e2e tests."""

import os
import subprocess
import time


def wait_for_file(path, timeout=15, interval=0.1):
    """Wait until a file exists and return its contents. Returns None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            content = open(path).read().strip()
            if content:
                return content
        except (FileNotFoundError, IOError):
            pass
        time.sleep(interval)
    return None


def wait_for_pane_contains(pane_id, text, timeout=15, interval=0.3):
    """Poll tmux pane until it contains the expected text. Returns pane content or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        content = capture_pane(pane_id)
        if content and text in content:
            return content
        time.sleep(interval)
    return None


def wait_for_agent_gone(get_fn, pane_id, timeout=35, interval=1):
    """Poll /agents until the agent with this pane_id is gone. Returns True/False."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = get_fn("/agents")
            if all(a.get("pane_id") != pane_id for a in data.get("agents", [])):
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def capture_pane(pane_id, lines=30):
    """Raw tmux capture-pane."""
    result = subprocess.run(
        ["tmux", "capture-pane", "-pt", pane_id, "-S", f"-{lines}"],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout if result.returncode == 0 else None


def launch_agent(name, monitor_url, cmd="cat", cwd="/tmp", timeout=15):
    """Launch an agent via agentura-run and wait for registration via ready-file.

    Returns (pane_id, agent_id) or (pane_id, None) on registration timeout.
    """
    import tempfile
    ready_file = tempfile.mktemp(prefix="agentura-ready-", suffix=".txt")

    shell_cmd = (
        f"cd {cwd} && "
        f"AGENTURA_URL={monitor_url} "
        f"AGENTURA_READY_FILE={ready_file} "
        f"exec agentura-run {cmd}"
    )

    result = subprocess.run(
        ["tmux", "new-window", "-P", "-F", "#{pane_id}", "-n", name, shell_cmd],
        capture_output=True, text=True, timeout=5,
    )
    pane_id = result.stdout.strip()
    if result.returncode != 0 or not pane_id:
        return None, None

    # Wait for ready-file (written by agentura-run after registration)
    agent_id = wait_for_file(ready_file, timeout=timeout)

    # Clean up ready file
    try:
        os.unlink(ready_file)
    except FileNotFoundError:
        pass

    return pane_id, agent_id
