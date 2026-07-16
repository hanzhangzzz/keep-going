"""Unified Turn schema shared by all adapters."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Source = Literal["claude-code", "codex"]
Role = Literal["user", "assistant"]


class Turn(BaseModel):
    turn_id: str
    source: Source
    session_id: str
    ts: datetime
    project: str
    role: Role
    content: str
    prev_assistant: str | None = None
    turn_idx: int
    meta: dict[str, Any] = Field(default_factory=dict)


def make_turn_id(source: str, session_id: str, idx: int) -> str:
    h = hashlib.sha1(f"{source}|{session_id}|{idx}".encode()).hexdigest()
    return h[:16]
