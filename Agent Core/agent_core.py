"""Compatibility wrapper for the original user-created entry file."""

from __future__ import annotations

import sys
from pathlib import Path


# The existing user-created folder contains a space. Keep it intact while making
# the project root importable when this file is executed directly.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jarvis.core import AgentCore

__all__ = ["AgentCore"]
