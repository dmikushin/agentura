#!/usr/bin/env python3
"""
mock_agent.py — fake agent that prints numbered lines for testing.

Usage: python3 mock_agent.py [interval_sec] [num_lines]
Default: prints 50 lines at 2-second intervals.
"""

import sys
import time

interval = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
count = int(sys.argv[2]) if len(sys.argv) > 2 else 50

for i in range(1, count + 1):
    print(f"[mock] Line {i}: the quick brown fox jumps over the lazy dog")
    sys.stdout.flush()
    if i < count:
        time.sleep(interval)

print("[mock] Done.")
