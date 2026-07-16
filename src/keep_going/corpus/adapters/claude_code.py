"""Claude Code JSONL → unified Turn stream.

Format reference (after empirical sweep over ~/.claude/projects):
- event types seen: user, assistant, attachment, system, file-history-snapshot,
  last-prompt, permission-mode, queue-operation, ai-title
- We only emit user/assistant events.
- `message.content` may be a string OR a list of content blocks.
- `cwd` is at the top level for user/assistant events.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from ..schema import Turn, make_turn_id

_COMMAND_BLOCK_RE = re.compile(r"<command-(name|message|args)>[^<]*</command-\1>", re.DOTALL)
_LOCAL_COMMAND_RE = re.compile(r"<local-command-stdout>[\s\S]*?</local-command-stdout>", re.DOTALL)


def _extract_text(content: str | list | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                # Skip tool_use payloads — they're machine actions, not user intent
                continue
            elif btype == "tool_result":
                # Tool results are inputs to the model, not user words
                continue
        return "\n".join(p for p in parts if p)
    return ""


def _clean_user_text(text: str) -> str:
    """Strip command-message envelopes that Claude Code wraps slash commands with."""
    out = _COMMAND_BLOCK_RE.sub("", text)
    out = _LOCAL_COMMAND_RE.sub("", out)
    return out.strip()


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # ISO 8601 with Z
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_session(path: Path) -> Iterator[Turn]:
    """Stream Turn objects out of a single Claude Code session JSONL."""
    session_id = path.stem  # filename minus .jsonl
    project = path.parent.name
    prev_assistant: str | None = None
    idx = 0

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = obj.get("type")
            if etype not in ("user", "assistant"):
                continue
            if obj.get("isSidechain") is True:
                continue

            msg = obj.get("message") or {}
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue

            text = _extract_text(msg.get("content"))
            if role == "user":
                text = _clean_user_text(text)
            else:
                text = text.strip()

            if not text:
                continue

            ts = _parse_ts(obj.get("timestamp"))
            if ts is None:
                continue

            cwd = obj.get("cwd") or project

            turn = Turn(
                turn_id=make_turn_id("claude-code", session_id, idx),
                source="claude-code",
                session_id=session_id,
                ts=ts,
                project=cwd,
                role=role,
                content=text,
                prev_assistant=prev_assistant if role == "user" else None,
                turn_idx=idx,
                meta={"file": str(path), "etype": etype},
            )
            yield turn

            if role == "assistant":
                prev_assistant = text
            idx += 1


def iter_sessions(root: Path, *, since: datetime | None = None) -> Iterator[Turn]:
    """Walk all *.jsonl under root, optionally filtering by mtime."""
    if not root.exists():
        return
    for jsonl in root.rglob("*.jsonl"):
        if since is not None:
            try:
                mtime = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < since:
                continue
        try:
            yield from iter_session(jsonl)
        except OSError:
            continue
