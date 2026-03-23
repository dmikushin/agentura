"""
sidecar_ipc.py — Unix socket IPC between sidecar and MCP backend.

The sidecar is the sole network gateway. MCP backend connects to sidecar
over a local Unix socket instead of making HTTPS calls directly.

Protocol: JSON-lines over Unix domain socket (one request + one response per connection).

Request:  {"method": "GET"|"POST", "path": "/agents", "data": {...}}\n
Response: {"status": 200, "body": {...}}\n
"""

import json
import os
import select
import socket


class SidecarUnavailable(Exception):
    """Raised when the sidecar IPC socket is not available."""
    pass


class SidecarListener:
    """Unix socket server integrated into sidecar's main loop.

    Non-blocking: process_pending() uses select() with a timeout,
    doubling as the sidecar's sleep between heartbeat cycles.
    """

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        # Remove stale socket file
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.setblocking(False)
        self._sock.bind(socket_path)
        self._sock.listen(5)

    def process_pending(self, proxy_fn, timeout: float = 2.0):
        """Process pending IPC requests, blocking up to timeout seconds.

        proxy_fn(method, path, data) -> dict  — called for each request.
        Returns the response body dict.

        This replaces time.sleep() in the sidecar loop: it waits for
        IPC requests OR times out after the heartbeat interval.
        """
        readable, _, _ = select.select([self._sock], [], [], timeout)
        if not readable:
            return

        # Accept all pending connections
        while True:
            try:
                conn, _ = self._sock.accept()
            except BlockingIOError:
                break
            try:
                self._handle_connection(conn, proxy_fn)
            except Exception:
                pass
            finally:
                conn.close()

    def _handle_connection(self, conn: socket.socket, proxy_fn):
        """Read one JSON request, proxy it, write one JSON response."""
        conn.setblocking(True)
        conn.settimeout(5.0)

        # Read until newline
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk

        request = json.loads(data.decode().strip())
        method = request.get("method", "GET")
        path = request.get("path", "/")
        body = request.get("data")

        try:
            result = proxy_fn(method, path, body)
            response = {"status": 200, "body": result}
        except Exception as e:
            response = {"status": 500, "body": {"error": str(e)}}

        conn.sendall(json.dumps(response).encode() + b"\n")

    def close(self):
        """Close the listener and remove the socket file."""
        try:
            self._sock.close()
        except Exception:
            pass
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


def sidecar_request(sock_path: str, method: str, path: str,
                    data: dict | None = None) -> dict:
    """Send a request to the sidecar via Unix socket IPC.

    Returns the response body dict.
    Raises SidecarUnavailable on any connection/protocol error.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30.0)
        sock.connect(sock_path)

        request = {"method": method, "path": path}
        if data is not None:
            request["data"] = data
        sock.sendall(json.dumps(request).encode() + b"\n")

        # Read response
        response_data = b""
        while b"\n" not in response_data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            response_data += chunk

        sock.close()

        if not response_data.strip():
            raise SidecarUnavailable("empty response from sidecar")

        response = json.loads(response_data.decode().strip())
        status = response.get("status", 500)

        if status >= 400:
            # Propagate server errors
            body = response.get("body", {})
            from urllib.error import HTTPError
            import io
            error_body = json.dumps(body).encode()
            raise HTTPError(
                url=path,
                code=status,
                msg=body.get("error", "server error"),
                hdrs={},
                fp=io.BytesIO(error_body),
            )

        return response.get("body", {})

    except SidecarUnavailable:
        raise
    except (socket.error, ConnectionRefusedError, FileNotFoundError,
            json.JSONDecodeError) as e:
        raise SidecarUnavailable(f"sidecar IPC failed: {e}") from e
