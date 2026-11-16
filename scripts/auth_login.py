#!/usr/bin/env python3
"""Convenience wrapper: python scripts/auth_login.py"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from salesforce_mcp_bridge.auth_cli import main

if __name__ == "__main__":
    main()
