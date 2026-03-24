"""auth.py — SSH challenge-response authentication for agentura.

Extracted from ~/forge/learn/learnlib.py (gambetta).

Server side: SSHKeyVerifier + AuthSessionStore
Client side: SSHAgentClient + authenticate()
"""

import base64
import json
import os
import secrets
import socket
import struct
import time
import urllib.request
import urllib.error


# ---------------------------------------------------------------------------
# SSHAgentClient (client-side: talks to ssh-agent)
# ---------------------------------------------------------------------------
class SSHAgentClient:
    """Communicate with ssh-agent via SSH_AUTH_SOCK (RFC 4253 agent protocol)."""

    _REQUEST_IDENTITIES = 11
    _IDENTITIES_ANSWER = 12
    _SIGN_REQUEST = 13
    _SIGN_RESPONSE = 14
    _RSA_SHA2_256 = 0x02

    def __init__(self):
        sock_path = os.environ.get("SSH_AUTH_SOCK")
        if not sock_path:
            raise RuntimeError("SSH_AUTH_SOCK not set — is ssh-agent running?")
        self._sock_path = sock_path

    def _communicate(self, msg: bytes) -> bytes:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self._sock_path)
            sock.sendall(struct.pack(">I", len(msg)) + msg)
            raw_len = b""
            while len(raw_len) < 4:
                chunk = sock.recv(4 - len(raw_len))
                if not chunk:
                    raise RuntimeError("SSH agent closed connection")
                raw_len += chunk
            resp_len = struct.unpack(">I", raw_len)[0]
            resp = b""
            while len(resp) < resp_len:
                chunk = sock.recv(resp_len - len(resp))
                if not chunk:
                    raise RuntimeError("SSH agent closed connection")
                resp += chunk
            return resp
        finally:
            sock.close()

    @staticmethod
    def _read_string(data: bytes, offset: int) -> tuple:
        slen = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        return data[offset:offset + slen], offset + slen

    def list_keys(self) -> list:
        resp = self._communicate(bytes([self._REQUEST_IDENTITIES]))
        if resp[0] != self._IDENTITIES_ANSWER:
            raise RuntimeError(f"Unexpected agent response: {resp[0]}")
        nkeys = struct.unpack_from(">I", resp, 1)[0]
        keys = []
        offset = 5
        for _ in range(nkeys):
            blob, offset = self._read_string(resp, offset)
            comment, offset = self._read_string(resp, offset)
            keys.append((blob, comment.decode("utf-8", errors="replace")))
        return keys

    def sign(self, key_blob: bytes, data: bytes) -> tuple:
        key_type, _ = self._read_string(key_blob, 0)
        flags = self._RSA_SHA2_256 if key_type == b"ssh-rsa" else 0

        msg = struct.pack("B", self._SIGN_REQUEST)
        msg += struct.pack(">I", len(key_blob)) + key_blob
        msg += struct.pack(">I", len(data)) + data
        msg += struct.pack(">I", flags)

        resp = self._communicate(msg)
        if resp[0] != self._SIGN_RESPONSE:
            raise RuntimeError(f"Agent refused to sign: response type {resp[0]}")

        sig_blob, _ = self._read_string(resp, 1)
        sig_type, off = self._read_string(sig_blob, 0)
        sig_data, _ = self._read_string(sig_blob, off)
        return sig_type.decode(), sig_data


# ---------------------------------------------------------------------------
# SSHKeyVerifier (server-side: verifies signatures)
# ---------------------------------------------------------------------------
class SSHKeyVerifier:
    """Verify SSH signatures using the cryptography library."""

    def __init__(self, authorized_keys_path: str):
        self._keys = {}
        from pathlib import Path
        path = Path(authorized_keys_path)
        if not path.is_file():
            raise FileNotFoundError(f"authorized_keys not found: {path}")
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            key_type, key_b64 = parts[0], parts[1]
            comment = parts[2] if len(parts) > 2 else ""
            try:
                key_blob = base64.b64decode(key_b64)
            except Exception:
                continue
            self._keys[key_blob] = (key_type, comment)

    def is_authorized(self, key_blob: bytes) -> bool:
        return key_blob in self._keys

    def get_comment(self, key_blob: bytes) -> str:
        entry = self._keys.get(key_blob)
        return entry[1] if entry else ""

    def verify(self, key_blob: bytes, data: bytes,
               sig_type: str, sig_data: bytes) -> bool:
        from cryptography.hazmat.primitives.serialization import load_ssh_public_key
        from cryptography.hazmat.primitives.asymmetric import ec, padding, utils
        from cryptography.hazmat.primitives import hashes
        from cryptography.exceptions import InvalidSignature

        entry = self._keys.get(key_blob)
        if not entry:
            return False
        key_type_str = entry[0]
        key_line = f"{key_type_str} {base64.b64encode(key_blob).decode()}"

        try:
            pubkey = load_ssh_public_key(key_line.encode())
        except Exception:
            return False

        try:
            if sig_type == "ssh-ed25519":
                pubkey.verify(sig_data, data)
            elif sig_type in ("rsa-sha2-256", "rsa-sha2-512"):
                hash_algo = hashes.SHA256() if "256" in sig_type else hashes.SHA512()
                pubkey.verify(sig_data, data, padding.PKCS1v15(), hash_algo)
            elif sig_type.startswith("ecdsa-sha2-"):
                r, off = self._read_mpint(sig_data, 0)
                s, _ = self._read_mpint(sig_data, off)
                der_sig = utils.encode_dss_signature(r, s)
                curve_name = sig_type.split("-")[-1]
                hash_map = {
                    "nistp256": hashes.SHA256(),
                    "nistp384": hashes.SHA384(),
                    "nistp521": hashes.SHA512(),
                }
                hash_algo = hash_map.get(curve_name, hashes.SHA256())
                pubkey.verify(der_sig, data, ec.ECDSA(hash_algo))
            else:
                return False
        except InvalidSignature:
            return False

        return True

    @staticmethod
    def _read_mpint(data: bytes, offset: int) -> tuple:
        slen = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        val = int.from_bytes(data[offset:offset + slen], "big")
        return val, offset + slen


# ---------------------------------------------------------------------------
# AuthSessionStore (server-side: nonces + tokens)
# ---------------------------------------------------------------------------
class AuthSessionStore:
    NONCE_TTL = 60
    TOKEN_TTL = 300
    AGENT_TOKEN_TTL = 3600  # 1 hour
    DELEGATION_TOKEN_TTL = 86400  # 24 hours

    def __init__(self):
        self._nonces = {}
        self._sessions = {}
        self._agent_tokens = {}  # {token: {"agent_id", "expires"}}
        self._delegation_tokens = {}  # {token: {"creator", "target_host", "expires"}}

    def create_nonce(self) -> str:
        self._cleanup_nonces()
        nonce = secrets.token_bytes(32)
        nonce_b64 = base64.b64encode(nonce).decode()
        self._nonces[nonce_b64] = time.time()
        return nonce_b64

    def consume_nonce(self, nonce_b64: str) -> bool:
        self._cleanup_nonces()
        ts = self._nonces.pop(nonce_b64, None)
        if ts is None:
            return False
        return (time.time() - ts) < self.NONCE_TTL

    def create_token(self) -> tuple:
        self._cleanup_sessions()
        token = secrets.token_hex(32)
        self._sessions[token] = {
            "expires": time.time() + self.TOKEN_TTL,
        }
        return token, self.TOKEN_TTL

    def validate_token(self, token: str) -> bool | None:
        self._cleanup_sessions()
        session = self._sessions.get(token)
        if session is None:
            return None
        if time.time() > session["expires"]:
            del self._sessions[token]
            return None
        return True

    def _cleanup_nonces(self):
        now = time.time()
        expired = [k for k, ts in self._nonces.items()
                   if now - ts > self.NONCE_TTL]
        for k in expired:
            del self._nonces[k]

    # --- Agent tokens ---

    def create_agent_token(self, agent_id: str) -> tuple[str, int]:
        """Create a token bound to agent_id. Returns (token, ttl)."""
        self._cleanup_agent_tokens()
        token = secrets.token_hex(32)
        self._agent_tokens[token] = {
            "agent_id": agent_id,
            "expires": time.time() + self.AGENT_TOKEN_TTL,
        }
        return token, self.AGENT_TOKEN_TTL

    def validate_agent_token(self, token: str) -> str | None:
        """Validate agent token. Returns agent_id or None."""
        self._cleanup_agent_tokens()
        entry = self._agent_tokens.get(token)
        if entry is None:
            return None
        if time.time() > entry["expires"]:
            del self._agent_tokens[token]
            return None
        return entry["agent_id"]

    def refresh_agent_token(self, agent_id: str) -> tuple[str, int]:
        """Issue an additional agent token (existing tokens remain valid)."""
        self._cleanup_agent_tokens()
        return self.create_agent_token(agent_id)

    def _cleanup_agent_tokens(self):
        now = time.time()
        expired = [k for k, v in self._agent_tokens.items()
                   if now > v["expires"]]
        for k in expired:
            del self._agent_tokens[k]

    def _cleanup_sessions(self):
        now = time.time()
        expired = [k for k, s in self._sessions.items()
                   if now > s["expires"]]
        for k in expired:
            del self._sessions[k]

    # --- Delegation tokens ---

    def create_delegation_token(self, creator: str, target_host: str, team: str = "") -> tuple[str, int]:
        """Create a delegation token for a remote agent.

        Args:
            creator: agent_id of the creating agent
            target_host: hostname where the remote agent will run
            team: optional team to auto-join on registration

        Returns:
            (token, ttl) tuple
        """
        self._cleanup_delegation_tokens()
        token = secrets.token_hex(32)
        self._delegation_tokens[token] = {
            "creator": creator,
            "target_host": target_host,
            "team": team,
            "expires": time.time() + self.DELEGATION_TOKEN_TTL,
        }
        return token, self.DELEGATION_TOKEN_TTL

    def validate_delegation_token(self, token: str) -> dict | None:
        """Validate a delegation token. Returns token info dict or None."""
        self._cleanup_delegation_tokens()
        entry = self._delegation_tokens.get(token)
        if entry is None:
            return None
        if time.time() > entry["expires"]:
            del self._delegation_tokens[token]
            return None
        return entry

    def refresh_delegation_token(self, old_token: str) -> tuple[str, int] | None:
        """Refresh a delegation token. Returns (new_token, ttl) or None."""
        self._cleanup_delegation_tokens()
        entry = self._delegation_tokens.pop(old_token, None)
        if entry is None:
            return None
        if time.time() > entry["expires"]:
            return None
        return self.create_delegation_token(
            entry["creator"], entry["target_host"])

    def _cleanup_delegation_tokens(self):
        now = time.time()
        expired = [k for k, v in self._delegation_tokens.items()
                   if now > v["expires"]]
        for k in expired:
            del self._delegation_tokens[k]


# ---------------------------------------------------------------------------
# Client-side authenticate() helper
# ---------------------------------------------------------------------------
def authenticate(monitor_url: str) -> str | None:
    """Perform SSH challenge-response auth against the monitor.

    Returns a bearer token string, or None if auth is not required
    (server returned 404 on challenge endpoint — auth not enabled).

    Raises RuntimeError on auth failure.
    """
    # Step 1: get nonce
    try:
        req = urllib.request.Request(f"{monitor_url}/api/auth/challenge")
        with urllib.request.urlopen(req, timeout=5) as resp:
            nonce_b64 = json.loads(resp.read().decode())["nonce"]
    except urllib.error.URLError:
        return None  # server not running

    nonce_bytes = base64.b64decode(nonce_b64)

    # Step 2: sign with ssh-agent
    agent = SSHAgentClient()
    keys = agent.list_keys()
    if not keys:
        raise RuntimeError("No keys in SSH agent. Run ssh-add first.")

    # Step 3: try each key
    for key_blob, _comment in keys:
        sig_type, sig_data = agent.sign(key_blob, nonce_bytes)

        verify_body = json.dumps({
            "nonce": nonce_b64,
            "key_blob": base64.b64encode(key_blob).decode(),
            "signature": base64.b64encode(sig_data).decode(),
            "sig_type": sig_type,
        }).encode()

        req = urllib.request.Request(
            f"{monitor_url}/api/auth/verify",
            data=verify_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            return data["token"]
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                continue  # try next key
            raise

    raise RuntimeError("No SSH key was accepted by the server")
