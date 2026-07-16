"""Codex CLI rollout JSONL → unified Turn stream.

Format reference (after empirical sweep over ~/.codex/archived_sessions):
- First line is `session_meta` with id, timestamp, cwd
- Then `response_item` events with payload.type=message, payload.role=user|assistant
- Content is a list of blocks with `type` and `text` fields
- Also `event_msg`, `response_item.function_call`, `reasoning` etc. — ignored here
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from ..schema import Turn, make_turn_id


def _extract_text(content: list | None) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        # Codex payload blocks: input_text / output_text / text
        if "text" in block and isinstance(block["text"], str):
            parts.append(block["text"])
    return "\n".join(p for p in parts if p)


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_user_instructions_wrap(text: str) -> bool:
    """Codex prepends <user_instructions>...</user_instructions> on first turn — not user intent."""
    t = text.strip()
    return t.startswith("<user_instructions>") and t.endswith("</user_instructions>")


def iter_session(path: Path) -> Iterator[Turn]:
    session_id: str = ""
    project: str = ""
    prev_assistant: str | None = None
    idx = 0
    meta_ts: datetime | None = None

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = obj.get("type")

            if etype == "session_meta":
                p = obj.get("payload") or {}
                session_id = p.get("id") or path.stem
                project = p.get("cwd") or ""
                meta_ts = _parse_ts(p.get("timestamp"))
                continue

            if etype != "response_item":
                continue

            payload = obj.get("payload") or {}
            if payload.get("type") != "message":
                continue

            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue

            text = _extract_text(payload.get("content"))
            text = text.strip()
            if not text:
                continue
            if role == "user" and _is_user_instructions_wrap(text):
                continue

            ts = _parse_ts(obj.get("timestamp")) or meta_ts
            if ts is None:
                continue

            turn = Turn(
                turn_id=make_turn_id("codex", session_id or path.stem, idx),
                source="codex",
                session_id=session_id or path.stem,
                ts=ts,
                project=project,
                role=role,
                content=text,
                prev_assistant=prev_assistant if role == "user" else None,
                turn_idx=idx,
                meta={"file": str(path)},
            )
            yield turn

            if role == "assistant":
                prev_assistant = text
            idx += 1


def iter_sessions(root: Path, *, since: datetime | None = None) -> Iterator[Turn]:
    if not root.exists():
        return
    for jsonl in sorted(root.rglob("rollout-*.jsonl")):
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
