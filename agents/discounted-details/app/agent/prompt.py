"""Loads the system prompt from docs/AGENT_INSTRUCTIONS.md (everything after the first line that is exactly '---')."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parents[2] / "docs" / "AGENT_INSTRUCTIONS.md"


@lru_cache
def system_prompt() -> str:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    idx = next(i for i, line in enumerate(lines) if line.strip() == "---")
    return "\n".join(lines[idx + 1:]).strip()
