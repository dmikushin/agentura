#!/usr/bin/env python3
"""
tmux_tools.py — smart abstractions over tmux for AI-to-AI communication.

Three tools:
  1. reliable_send  — send text to a pane with delivery verification
  2. stream_read    — capture pane as a continuous deduplicated stream
  3. tui_to_md      — clean TUI output into readable markdown

Can be used as:
  - MCP server (via FastMCP, for Claude Code)
  - CLI tool (for Gemini CLI shell calls)
  - Python library (import and call directly)
"""

import subprocess
import time
import hashlib
import json
import os
import re
from pathlib import Path

# --- State files (created lazily, only needed by stream_read) ---
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "ai_chat"


# =============================================================================
# Tool 1: RELIABLE SEND
# =============================================================================

def reliable_send(pane_id: str, text: str, timeout: float = 5.0) -> dict:
    """Send text to a tmux pane with delivery verification.

    1. Captures pane state before send
    2. Sends text via send-keys (without Enter)
    3. Sends Enter separately
    4. Verifies text appeared in pane
    5. Returns success/failure with details

    Args:
        pane_id: tmux pane ID (e.g. "%0", "%1")
        text: text to send
        timeout: max seconds to wait for verification

    Returns:
        dict with 'success', 'message', and optionally 'error'
    """
    # 1. Capture state before send
    before = _capture_raw(pane_id, lines=50)
    if before is None:
        return {"success": False, "error": f"Cannot capture pane {pane_id}"}

    # 2. Send text (without Enter)
    rc = subprocess.run(
        ["tmux", "send-keys", "-t", pane_id, text],
        capture_output=True, timeout=5
    ).returncode
    if rc != 0:
        return {"success": False, "error": f"send-keys failed with rc={rc}"}

    # 3. Small delay then send Enter
    time.sleep(0.1)
    rc = subprocess.run(
        ["tmux", "send-keys", "-t", pane_id, "Enter"],
        capture_output=True, timeout=5
    ).returncode
    if rc != 0:
        return {"success": False, "error": f"send-keys Enter failed with rc={rc}"}

    # 4. Verify delivery — check that text appeared in pane
    # Use a short prefix of the text for matching (first 40 chars)
    check_text = text[:40].strip()
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        after = _capture_raw(pane_id, lines=50)
        if after and check_text in after and after != before:
            return {
                "success": True,
                "message": f"Delivered to {pane_id}: {text[:80]}{'...' if len(text) > 80 else ''}"
            }

    return {
        "success": False,
        "error": f"Text not verified in pane {pane_id} within {timeout}s (may still have been delivered)"
    }


# =============================================================================
# Tool 2: STREAM READ
# =============================================================================

def stream_read(pane_id: str, lines: int = 200) -> dict:
    """Capture pane content and return only NEW lines since last call.

    Maintains per-pane state to deduplicate content between captures.
    Returns only lines that haven't been seen before.

    Args:
        pane_id: tmux pane ID
        lines: number of history lines to capture

    Returns:
        dict with 'new_content' (new lines as string) and 'total_lines'
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f".stream_{pane_id.replace('%', 'p')}"

    # Load previous state
    prev_hashes = set()
    if state_file.exists():
        try:
            prev_hashes = set(json.loads(state_file.read_text()))
        except (json.JSONDecodeError, TypeError):
            prev_hashes = set()

    # Capture current pane
    raw = _capture_raw(pane_id, lines=lines)
    if raw is None:
        return {"new_content": "", "total_lines": 0, "error": f"Cannot capture pane {pane_id}"}

    current_lines = raw.split("\n")

    # Find new lines by hashing each line with its neighbors for context
    new_lines = []
    current_hashes = []
    for i, line in enumerate(current_lines):
        # Hash includes line + position context (prev line) to reduce false dedup
        context = current_lines[i - 1] if i > 0 else ""
        h = hashlib.md5(f"{context}|{line}".encode()).hexdigest()[:12]
        current_hashes.append(h)
        if h not in prev_hashes:
            new_lines.append(line)

    # Save current state (keep only recent hashes to prevent unbounded growth)
    state_file.write_text(json.dumps(current_hashes[-500:]))

    new_content = "\n".join(new_lines)
    return {
        "new_content": new_content,
        "total_lines": len(new_lines)
    }


# =============================================================================
# Tool 3: TUI TO MARKDOWN
# =============================================================================

# Box-drawing characters (light+heavy+double+rounded)
_BOX_CHARS = set("─━│┃┄┅┆┇┈┉┊┋┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┝┞┟┠┡┢┣┤┥┦┧┨┩┪┫┬┭┮┯┰┱┲┳┴┵┶┷┸┹┺┻┼┽┾┿╀╁╂╃╄╅╆╇╈╉╊╋╌╍╎╏═║╒╓╔╕╖╗╘╙╚╛╜╝╞╟╠╡╢╣╤╥╦╧╨╩╪╫╬╭╮╯╰╴╵╶╷╸╹╺╻╼╽╾╿")

# ANSI escape sequence pattern
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[@-~]')

# Noise patterns — lines matching these are removed entirely.
# PRINCIPLE: better to let some noise through than to lose real content.
_NOISE_PATTERNS = [
    # === Spinners (both CLIs) ===
    re.compile(r'^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠛⠽⠺⠰⠶⠾⠿⣿⣽⣻⣯⣟⡿⢿⣾⣷⣶⣤⣀⡀⠄⠂⠁⠈⠐⠠⢀⣀⣤⣶⣷⣾⠿⡿⣟⣯⣻⣽⣿]'),
    re.compile(r'^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠛⠽⠺⠰⠶⠾]\s+\w+.*\(\d+[sm]'),  # "⠹ Thinking... (5s)"
    re.compile(r'^\s*[✶✻✽✸✷✹✺]\s+\w+.*\(\d+[sm]'),  # Gemini "✶ Observing... (10s)"
    re.compile(r'^\s*[✶✻✽✸✷✹✺]\s+\w+…'),  # "✻ Churned…" (no time)

    # === Claude Code UI ===
    re.compile(r'^\s*⏵⏵\s+bypass permissions'),  # permission mode status
    re.compile(r'^\s*… \+\d+ lines \(ctrl\+o'),  # collapsed blocks
    re.compile(r'^\s*\(ctrl\+b ctrl\+b.*to run in background\)'),  # background hint
    re.compile(r'^\s*Tip:'),  # tips in spinner
    re.compile(r'^\s*Running…'),  # "Running..." status
    re.compile(r'^\s*⎿\s*\{'),  # tool output JSON start "⎿  {"
    re.compile(r'^\s*"(success|message|result)"'),  # JSON fields from tool output
    re.compile(r'^\s*\}'),  # JSON closing brace

    # === Gemini CLI UI ===
    re.compile(r'^\s*YOLO mode \(ctrl'),  # YOLO toggle
    re.compile(r'^\s*\*\s+Type your message'),  # input prompt
    re.compile(r'^\s*You are running Gemini CLI'),  # directory warning
    re.compile(r'^\s*no sandbox \(see /docs\)'),  # sandbox notice
    re.compile(r'^\s*Auto \(Gemini \d\) /model'),  # model indicator
    re.compile(r'^\s*Queued \(press .* to edit\)'),  # queued message hint
    re.compile(r'^\s*esc to (cancel|interrupt)'),  # escape hints
    re.compile(r'^\s*CRITICAL INSTRUCTION'),  # internal prompt leak
    re.compile(r'^\s*<ctrl\d+>'),  # control sequences in thought blocks
    re.compile(r'^\s*✓\s+Shell\s'),  # Gemini shell command headers
    re.compile(r'^\s*✓\s+Read(File|Folder)\s'),  # Gemini read tool headers
    re.compile(r'^\s*✓\s+Search(Text|Files)\s'),  # Gemini search headers
    re.compile(r'^\s*Listed \d+ item'),  # folder listing summary
    re.compile(r'^\s*\(no new content\)'),  # empty stream_read result
    re.compile(r'^\s*\{"\s*success'),  # JSON tool output from reliable_send
    re.compile(r'^\s*~\s'),  # tilde with trailing content (Gemini status line)
    re.compile(r'^.*no sandbox \(see /docs\).*Auto \(Gemini'),  # Gemini combined status line

    # === Common UI ===
    re.compile(r'^\s*ctrl\+[a-z].*to (expand|run|toggle|cycle)'),  # keyboard hints
    re.compile(r'^\s*Press .* to (edit|cycle|toggle)'),  # UI hints
    re.compile(r'^─+$'),  # pure horizontal rules (full-width separators)
    re.compile(r'^━+$'),  # heavy horizontal rules

    # === Gemini thought blocks (multi-line internal reasoning) ===
    re.compile(r'^\s*✦\s*<ctrl\d+>'),  # "✦ <ctrl46>thought"
]

# Patterns for lines that are Gemini internal thought content (after ✦ <ctrl>thought)
_GEMINI_THOUGHT_START = re.compile(r'^\s*✦\s*<ctrl\d+>')
_GEMINI_THOUGHT_CONTENT = re.compile(r'^\s{2,}(CRITICAL INSTRUCTION|I will|I need|Plan:|Generating|Wait,|Let me|Looking at|I must|The task|Claude)')


def tui_to_md(text: str) -> str:
    """Clean TUI terminal output into readable markdown.

    Removes:
    - Box-drawing characters and decorative frames
    - ANSI escape sequences
    - Spinner/status lines
    - UI hints and prompts
    - Excess whitespace

    Preserves:
    - All actual content text
    - Code blocks and their formatting
    - Bullet points and lists
    - Error messages and tool outputs

    Args:
        text: raw terminal text (from capture-pane or stream_read)

    Returns:
        cleaned markdown string
    """
    lines = text.split("\n")
    result = []
    in_gemini_thought = False  # Track Gemini internal thought blocks

    for line in lines:
        # Strip ANSI escape sequences
        clean = _ANSI_RE.sub('', line)

        # Skip empty lines made of only box chars and whitespace
        stripped = clean.strip()
        if not stripped:
            if not in_gemini_thought:
                result.append("")
            continue

        # --- Gemini thought block filtering ---
        # Gemini dumps internal reasoning after "✦ <ctrl46>thought"
        # These blocks are indented and contain planning text
        if _GEMINI_THOUGHT_START.match(stripped):
            in_gemini_thought = True
            continue
        if in_gemini_thought:
            # Thought blocks end when we hit non-indented content
            # or a Gemini tool call (✓  Shell ...) or user-visible output (✦ text)
            if _GEMINI_THOUGHT_CONTENT.match(clean):
                continue  # still in thought block
            if stripped.startswith(('CRITICAL', 'I will', 'I need', 'Plan:', 'Generating',
                                   'Wait,', 'Let me', 'Looking at', 'I must', 'The task',
                                   'Claude has', 'The previous', 'I have', 'Now I',
                                   'I\'ll', 'I\'m', 'I should', 'I can', 'The user',
                                   'This is', 'So I', 'My plan', 'OK', 'Hmm')):
                continue  # still in thought block
            if clean.startswith('  '):  # indented content = still in thought
                continue
            # End of thought block
            in_gemini_thought = False

        # Skip lines that are purely box-drawing decorations
        if all(c in _BOX_CHARS or c in ' \t' for c in stripped):
            continue

        # Remove box borders FIRST, then check noise patterns
        clean = _strip_box_borders(clean)
        stripped = clean.strip()

        # Skip if became empty after stripping
        if not stripped:
            continue

        # Skip noise patterns (checked AFTER border removal)
        if any(p.match(stripped) for p in _NOISE_PATTERNS):
            continue

        result.append(clean)

    # Collapse multiple blank lines into max 2
    output = []
    blank_count = 0
    for line in result:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 2:
                output.append("")
        else:
            blank_count = 0
            output.append(line)

    return "\n".join(output).strip()


def _strip_box_borders(line: str) -> str:
    """Remove box-drawing chars from the edges of a line, preserving content."""
    # Remove leading box chars + spaces
    i = 0
    while i < len(line) and (line[i] in _BOX_CHARS or line[i] == ' ' and i < 3):
        if line[i] in _BOX_CHARS:
            i += 1
            # Also skip one space after a box char
            if i < len(line) and line[i] == ' ':
                i += 1
        else:
            break

    # Remove trailing box chars + spaces
    j = len(line)
    while j > i and (line[j - 1] in _BOX_CHARS or line[j - 1] == ' '):
        if line[j - 1] in _BOX_CHARS:
            j -= 1
        elif j - 2 >= i and line[j - 2] in _BOX_CHARS:
            j -= 1
        else:
            break

    return line[i:j]


# =============================================================================
# Internal helpers
# =============================================================================

def _capture_raw(pane_id: str, lines: int = 50) -> str | None:
    """Raw tmux capture-pane."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-pt", pane_id, "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# =============================================================================
# CLI interface (for Gemini and direct use)
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: tmux_tools.py <command> [args]")
        print("Commands: send <pane> <text>  |  read <pane>  |  clean <file|-|text>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "send" and len(sys.argv) >= 4:
        pane = sys.argv[2]
        text = " ".join(sys.argv[3:])
        result = reliable_send(pane, text)
        print(json.dumps(result, ensure_ascii=False))

    elif cmd == "read" and len(sys.argv) >= 3:
        pane = sys.argv[2]
        lines = int(sys.argv[3]) if len(sys.argv) > 3 else 200
        result = stream_read(pane, lines)
        if result.get("new_content"):
            clean = tui_to_md(result["new_content"])
            print(clean if clean else "(no new content)")
        else:
            print("(no new content)")

    elif cmd == "clean":
        if len(sys.argv) >= 3 and sys.argv[2] != "-":
            text = Path(sys.argv[2]).read_text() if os.path.isfile(sys.argv[2]) else " ".join(sys.argv[2:])
        else:
            text = sys.stdin.read()
        print(tui_to_md(text))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
