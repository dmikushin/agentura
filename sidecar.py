#!/usr/bin/env python3
"""
sidecar.py — process-sidecar for remote agents.

Runs on the remote host alongside the agent process. Captures the local
tmux pane, pushes stream content to the agentura server, sends heartbeats,
and polls/injects messages.

The sidecar is the parent process after fork() in agent.py remote mode.
The child becomes the actual agent via execvp().
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmux_tools import tui_to_md


HEARTBEAT_INTERVAL = 2  # seconds between loop iterations
TOKEN_REFRESH_INTERVAL = 2700  # refresh token every 45 min (agent_token TTL = 1h)


class RemoteSidecar:
    """Sidecar process that monitors a local agent and communicates with the central server."""

    def __init__(self, monitor_url: str, token: str, agent_id: str,
                 pane_id: str, child_pid: int, socket_path: str = ""):
        self.monitor_url = monitor_url.rstrip("/")
        self.token = token
        self.agent_id = agent_id
        self.pane_id = pane_id
        self.child_pid = child_pid
        self.socket_path = socket_path
        self.prev_hashes: set[str] = set()
        self._last_token_refresh = time.monotonic()
        self._listener = None

    def run(self):
        """Main sidecar loop: capture, push, heartbeat, poll messages."""
        import signal

        def _shutdown(signum, frame):
            print(f"[sidecar] Signal {signum}, sending final heartbeat", file=sys.stderr)
            self._heartbeat(child_alive=False)
            if self._listener:
                self._listener.close()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGHUP, _shutdown)

        # Start IPC listener for MCP backend
        if self.socket_path:
            from sidecar_ipc import SidecarListener
            self._listener = SidecarListener(self.socket_path)
            print(f"[sidecar] IPC listening on {self.socket_path}", file=sys.stderr)

        print(f"[sidecar] Started for agent {self.agent_id} (child PID {self.child_pid})",
              file=sys.stderr)
        try:
            while True:
                child_alive = self._pid_alive(self.child_pid)

                # Capture and push stream content
                content = self._capture_local_pane()
                if content:
                    new = self._dedup(content)
                    if new:
                        cleaned = tui_to_md(new)
                        if cleaned and cleaned.strip():
                            self._push_stream(cleaned)

                # Heartbeat
                self._heartbeat(child_alive)

                # Poll and inject messages
                for msg in self._poll_messages():
                    self._inject(msg["text"])

                # Token refresh
                self._maybe_refresh_token()

                if not child_alive:
                    print(f"[sidecar] Child PID {self.child_pid} exited, shutting down",
                          file=sys.stderr)
                    break

                # Process IPC requests from MCP (also serves as sleep)
                if self._listener:
                    self._listener.process_pending(self._proxy, timeout=HEARTBEAT_INTERVAL)
                else:
                    time.sleep(HEARTBEAT_INTERVAL)
        except KeyboardInterrupt:
            print("[sidecar] Interrupted", file=sys.stderr)
        finally:
            # Always send death heartbeat on exit
            try:
                self._heartbeat(child_alive=False)
            except Exception:
                pass
            # Close IPC listener
            if self._listener:
                self._listener.close()
                self._listener = None

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _capture_local_pane(self) -> str | None:
        """Capture content from the local tmux pane."""
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-pt", self.pane_id, "-S", "-200"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout
            return None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    def _dedup(self, content: str) -> str | None:
        """Deduplicate content against previous captures. Returns new lines or None."""
        lines = content.split("\n")
        hashes = []
        new_lines = []
        for i, line in enumerate(lines):
            context = lines[i - 1] if i > 0 else ""
            h = hashlib.md5(f"{context}|{line}".encode()).hexdigest()[:12]
            hashes.append(h)
            if h not in self.prev_hashes:
                new_lines.append(line)

        self.prev_hashes = set(hashes[-500:])

        if not new_lines:
            return None
        return "\n".join(new_lines)

    def _push_stream(self, content: str):
        """Push stream content to the central server."""
        self._post("/sidecar/stream-push", {
            "agent_id": self.agent_id,
            "content": content,
        })

    def _heartbeat(self, child_alive: bool):
        """Send heartbeat to the central server."""
        self._post("/sidecar/heartbeat", {
            "agent_id": self.agent_id,
            "child_alive": child_alive,
        })

    def _poll_messages(self) -> list[dict]:
        """Poll for messages from the central server."""
        try:
            resp = self._get(f"/sidecar/messages?agent_id={urllib.parse.quote(self.agent_id)}")
            return resp.get("messages", [])
        except Exception:
            return []

    def _inject(self, text: str):
        """Inject text into the local tmux pane.

        Uses load-buffer + paste-buffer for reliable text input (avoids
        send-keys issues with special characters and nodejs input buffering).
        """
        try:
            if text == "\x1b":
                # Escape: send-keys for control sequences
                subprocess.run(
                    ["tmux", "send-keys", "-t", self.pane_id, "Escape"],
                    capture_output=True, timeout=5,
                )
            else:
                # Paste text via tmux buffer (reliable for text content)
                subprocess.run(
                    ["tmux", "load-buffer", "-"],
                    input=text.encode(), capture_output=True, timeout=5,
                )
                subprocess.run(
                    ["tmux", "paste-buffer", "-t", self.pane_id],
                    capture_output=True, timeout=5,
                )
                # Press Enter separately (like xdotool press_keys)
                time.sleep(0.05)
                subprocess.run(
                    ["tmux", "send-keys", "-t", self.pane_id, "Enter"],
                    capture_output=True, timeout=5,
                )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    def _proxy(self, method: str, path: str, data: dict | None = None) -> dict:
        """Proxy an IPC request from MCP backend to the central server."""
        # Inject agent_token into body if requested
        if data and data.pop("_inject_agent_token", False):
            data["agent_token"] = self.token

        if method == "POST" and data is not None:
            return self._post(path, data)
        else:
            return self._get(path)

    def _maybe_refresh_token(self):
        """Refresh token before it expires. Tries agent-token refresh first,
        then delegation token refresh as fallback."""
        elapsed = time.monotonic() - self._last_token_refresh
        if elapsed < TOKEN_REFRESH_INTERVAL:
            return

        # Try refreshing via agent-token endpoint (works for both local and remote)
        try:
            resp = self._post("/api/auth/agent-token", {
                "agent_id": self.agent_id,
            })
            if resp.get("status") == "ok":
                self.token = resp["agent_token"]
                self._last_token_refresh = time.monotonic()
                print("[sidecar] Agent token refreshed", file=sys.stderr)
                return
        except Exception:
            pass

        # Fallback: try delegation token refresh
        try:
            resp = self._post_raw("/api/auth/delegate-refresh", {
                "delegation_token": self.token,
            })
            if resp.get("status") == "ok":
                self.token = resp["delegation_token"]
                self._last_token_refresh = time.monotonic()
                print("[sidecar] Delegation token refreshed", file=sys.stderr)
        except Exception as e:
            print(f"[sidecar] Token refresh failed: {e}", file=sys.stderr)

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(
            f"{self.monitor_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def _post(self, path: str, data: dict) -> dict:
        req = urllib.request.Request(
            f"{self.monitor_url}{path}",
            data=json.dumps(data).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.readable() else ""
            print(f"[sidecar] POST {path} failed ({e.code}): {body}", file=sys.stderr)
            return {}
        except urllib.error.URLError as e:
            print(f"[sidecar] POST {path} connection error: {e}", file=sys.stderr)
            return {}

    def _post_raw(self, path: str, data: dict) -> dict:
        """POST without auth header (for delegate-refresh)."""
        req = urllib.request.Request(
            f"{self.monitor_url}{path}",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
