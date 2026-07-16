"""Bounded rolling context for project-level Stop hook decisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from keep_going.decision.stop_safety import KEEP_GOING_INJECTION_MARKERS, detect_deterministic_risks


SCHEMA_VERSION = 2
DEFAULT_MAX_CONTEXT_BYTES = 16_384
DEFAULT_TAIL_BYTES = 128 * 1024
CURSOR_FINGERPRINT_BYTES = 4096
MAX_RECENT_TURNS = 12
MAX_EVIDENCE = 24
MAX_RECENT_HOOK_DECISIONS = 5
MAX_RECENT_COMMANDS = 20

CONSTRAINT_MARKERS = (
    "PLAN_ONLY",
    "只读",
    "只",
    "不要",
    "别",
    "禁止",
    "不得",
    "必须",
    "先",
    "等我确认",
    "不要执行",
    "不要改",
)
QUESTION_MARKERS = ("?", "？", "要不要", "是否", "能否", "可以继续", "继续吗", "下一步")
# System-injected envelopes that must never be mistaken for a genuine user goal.
# Mirrors corpus.harvest's filter set; kept local to avoid importing the heavy
# harvest module on the Stop hook hot path.
SYSTEM_ENVELOPE_PREFIXES = (
    "<teammate-message",
    "<task-notification",
    "<task-output",
    "<task-error",
    "<local-command-caveat",
    "<local-command-stdout",
    "<local-command-stderr",
    "<bash-input",
    "<bash-stdout",
    "<bash-stderr",
    "<bash-stop-hook",
    "<system-reminder",
    "<command-name",
    "<command-message",
    "<command-args",
    "[Request interrupted",
    "[Request continued",
)


def build_stop_decision_context(
    event: dict[str, Any],
    *,
    project_path: Path,
    cache_root: Path,
    max_context_bytes: int | None = None,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> dict[str, Any]:
    """Build and persist a bounded rolling decision context.

    The reader intentionally uses only the transcript path supplied by the
    current Stop event. It does not scan global Codex session folders, so nested
    backend sessions are filtered by source provenance rather than prompt text.
    """

    del project_path
    budget = max(512, int(max_context_bytes or _env_int("KEEP_GOING_STOP_CONTEXT_MAX_BYTES", DEFAULT_MAX_CONTEXT_BYTES)))
    transcript_raw = str(event.get("transcript_path") or "").strip()
    session_id = str(event.get("session_id") or "").strip()
    transcript_resolution = "event_transcript_path"
    if not transcript_raw:
        located = _locate_transcript_by_session_id(session_id)
        if located is None:
            return _bounded_context(_empty_context("missing_transcript", session_id=session_id), budget)
        transcript_path = located
        transcript_resolution = "session_id_filename"
    else:
        transcript_path = Path(transcript_raw).expanduser().resolve(strict=False)

    if not transcript_path.is_file():
        context = _empty_context("missing_transcript", session_id=session_id)
        context["source"]["transcript_path"] = str(transcript_path)
        context["source"]["transcript_resolution"] = transcript_resolution
        return _bounded_context(context, budget)

    cache_root.mkdir(parents=True, exist_ok=True)
    cache_path = cache_root / f"{_cache_key(session_id, transcript_path)}.json"
    cache = _load_cache(cache_path)
    stat = transcript_path.stat()
    previous = cache.get("decision_context") if isinstance(cache.get("decision_context"), dict) else None
    migrated_depth = 0
    cache_matches = (
        isinstance(previous, dict)
        and int(cache.get("schema_version") or -1) == SCHEMA_VERSION
        and str(cache.get("transcript_path") or "") == str(transcript_path)
        and int(cache.get("last_offset") or -1) <= stat.st_size
        and _cache_cursor_matches(transcript_path, cache)
    )

    if cache_matches:
        read_from = int(cache.get("last_offset") or 0)
        context = _coerce_context(previous, session_id=session_id)
        initialized = False
    else:
        read_from = _tail_start(transcript_path, max(0, int(tail_bytes)))
        context = _empty_context("initialized", session_id=session_id)
        if isinstance(previous, dict):
            migrated_depth = _migrated_chain_depth(previous)
            context["continuation_chain_depth"] = migrated_depth
        initialized = True

    try:
        turns, last_offset, new_lines, effective_read_from = _read_transcript_turns(
            transcript_path,
            read_from,
            skip_partial=initialized and read_from > 0,
        )
    except OSError as exc:
        context = _empty_context("read_error", session_id=session_id)
        context["context_confidence"] = "low"
        context["source"].update({"transcript_path": str(transcript_path), "error": str(exc)})
        return _bounded_context(context, budget)

    status = "initialized" if initialized else ("incremental" if new_lines else "cache_hit")
    context = _merge_turns(context, turns)
    context["continuation_chain_depth"] = max(
        int(context.get("continuation_chain_depth") or 0), migrated_depth
    )
    context["schema_version"] = SCHEMA_VERSION
    context["context_status"] = status
    context["context_confidence"] = _context_confidence(context)
    context["source"].update(
        {
            "transcript_path": str(transcript_path),
            "session_id": session_id,
            "read_from_offset": effective_read_from,
            "last_offset": last_offset,
            "new_lines": new_lines,
            "cache_path": str(cache_path),
            "transcript_resolution": transcript_resolution,
            "cursor_fingerprint": _cursor_fingerprint(transcript_path, last_offset),
        }
    )
    bounded = _bounded_context(context, budget)
    _write_cache(
        cache_path,
        {
            "schema_version": SCHEMA_VERSION,
            "transcript_path": str(transcript_path),
            "session_id": session_id,
            "last_offset": bounded["source"]["last_offset"],
            "cursor_fingerprint": bounded["source"].get("cursor_fingerprint", ""),
            "decision_context": bounded,
        },
    )
    return bounded


def _empty_context(status: str, *, session_id: str = "") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "context_status": status,
        "context_confidence": "low",
        "continuation_chain_depth": 0,
        "latest_user_goal": "",
        "explicit_constraints": [],
        "authorized_actions": [],
        "forbidden_actions": [],
        "current_progress": "",
        "verification_state": "unknown",
        "pending_question": "",
        "risk_flags": [],
        "recent_hook_decisions": [],
        "recent_commands": [],
        "recent_turns": [],
        "evidence": [],
        "source": {
            "session_id": session_id,
            "transcript_path": "",
            "read_from_offset": 0,
            "last_offset": 0,
            "new_lines": 0,
        },
    }


def _coerce_context(value: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    context = _empty_context("cache_hit", session_id=session_id)
    for key in context:
        if key in value:
            context[key] = value[key]
    if not isinstance(context.get("source"), dict):
        context["source"] = _empty_context("cache_hit", session_id=session_id)["source"]
    return context


def _migrated_chain_depth(value: dict[str, Any]) -> int:
    depths = [
        item
        for key, item in value.items()
        if key.endswith("_chain_depth") and isinstance(item, int) and not isinstance(item, bool) and item >= 0
    ]
    return max(depths, default=0)


def _read_transcript_turns(path: Path, offset: int, *, skip_partial: bool) -> tuple[list[dict[str, Any]], int, int, int]:
    turns: list[dict[str, Any]] = []
    new_lines = 0
    effective_read_from = max(0, offset)
    with path.open("rb") as f:
        f.seek(effective_read_from)
        if skip_partial:
            f.readline()
            effective_read_from = f.tell()
        while True:
            line_offset = f.tell()
            raw_line = f.readline()
            if not raw_line:
                break
            new_lines += 1
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            turn = _normalize_turn(obj, source=f"byte:{line_offset}")
            if turn is not None:
                turns.append(turn)
        last_offset = f.tell()
    return turns, last_offset, new_lines, effective_read_from


def _normalize_turn(obj: dict[str, Any], *, source: str) -> dict[str, Any] | None:
    role = ""
    text = ""
    kind = "message"
    if obj.get("type") == "response_item":
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        payload_type = str(payload.get("type") or "")
        if payload_type == "message":
            role = str(payload.get("role") or "")
            text = _extract_text(payload.get("content"))
        elif payload_type in {"function_call", "function_call_output"}:
            role = "tool"
            kind = payload_type
            text = _extract_tool_text(payload)
    elif obj.get("type") in {"user", "assistant"}:
        role = str(obj.get("type") or "")
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        text = _extract_text(message.get("content") or obj.get("content"))
    elif obj.get("role") in {"user", "assistant"}:
        role = str(obj.get("role") or "")
        text = _extract_text(obj.get("content")) or str(obj.get("text") or "")

    role = role if role in {"user", "assistant", "tool"} else ""
    text = str(text or "").strip()
    if not role or not text or _is_user_instructions_wrap(role, text):
        return None
    if role == "user" and _is_system_envelope(text):
        return None
    return {
        "role": role,
        "text": _clip(text, 1200),
        "kind": kind,
        "source": source,
        "source_kind": "keep_going_injection" if _is_keep_going_injection(text) else "task",
    }


def _merge_turns(context: dict[str, Any], turns: list[dict[str, Any]]) -> dict[str, Any]:
    recent_turns = _as_list(context.get("recent_turns"))
    evidence = _as_list(context.get("evidence"))
    hook_decisions = _as_list(context.get("recent_hook_decisions"))
    recent_commands = _as_list(context.get("recent_commands"))
    depth = int(context.get("continuation_chain_depth") or 0)

    for turn in turns:
        recent_turns.append(turn)
        text = str(turn.get("text") or "")
        source = str(turn.get("source") or "")
        if turn.get("source_kind") == "keep_going_injection":
            depth += 1
            hook_decisions.append({"text": _clip(text, 300), "source": source})
            continue
        context["risk_flags"] = _dedupe(
            [*_as_list(context.get("risk_flags")), *detect_deterministic_risks(text)]
        )
        role = turn.get("role")
        if role == "user":
            depth = 0
            context["latest_user_goal"] = _clip(text, 800)
            constraints = _extract_constraints(text)
            context["explicit_constraints"] = _dedupe([*_as_list(context.get("explicit_constraints")), *constraints])
            forbidden = _forbidden_actions(text)
            context["forbidden_actions"] = _dedupe([*_as_list(context.get("forbidden_actions")), *forbidden])
            for item in constraints:
                evidence.append({"field": "explicit_constraints", "source": source, "quote": _clip(item, 180)})
            if text.strip():
                evidence.append({"field": "latest_user_goal", "source": source, "quote": _clip(text, 180)})
        elif role == "assistant":
            context["current_progress"] = _clip(text, 800)
            if _looks_like_question(text):
                context["pending_question"] = _clip(text, 500)
            verification = _verification_state(text)
            if verification != "unknown":
                context["verification_state"] = verification
                evidence.append({"field": "verification_state", "source": source, "quote": _clip(text, 180)})
        elif role == "tool":
            recent_commands.append({"text": _clip(text, 240), "source": source})
            verification = _verification_state(text)
            if verification != "unknown":
                context["verification_state"] = verification

    context["recent_turns"] = recent_turns[-MAX_RECENT_TURNS:]
    context["recent_hook_decisions"] = hook_decisions[-MAX_RECENT_HOOK_DECISIONS:]
    context["recent_commands"] = recent_commands[-MAX_RECENT_COMMANDS:]
    context["evidence"] = evidence[-MAX_EVIDENCE:]
    context["continuation_chain_depth"] = max(0, depth)
    return context


def _bounded_context(context: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    context = json.loads(json.dumps(context, ensure_ascii=False, default=str))
    _preclip_context(context)
    while _json_size(context) > max_bytes and context.get("recent_turns"):
        context["recent_turns"].pop(0)
    while _json_size(context) > max_bytes and context.get("evidence"):
        context["evidence"].pop(0)
    while _json_size(context) > max_bytes and context.get("recent_commands"):
        context["recent_commands"].pop(0)
    while _json_size(context) > max_bytes and context.get("recent_hook_decisions"):
        context["recent_hook_decisions"].pop(0)
    if _json_size(context) > max_bytes:
        for key in ("current_progress", "pending_question", "latest_user_goal"):
            context[key] = _clip(str(context.get(key) or ""), 240)
    if _json_size(context) > max_bytes:
        context["source"] = {
            "session_id": str((context.get("source") or {}).get("session_id") or ""),
            "read_from_offset": int((context.get("source") or {}).get("read_from_offset") or 0),
            "last_offset": int((context.get("source") or {}).get("last_offset") or 0),
            "new_lines": int((context.get("source") or {}).get("new_lines") or 0),
        }
    while _json_size(context) > max_bytes and context.get("explicit_constraints"):
        context["explicit_constraints"].pop(0)
    return context


def _preclip_context(context: dict[str, Any]) -> None:
    for key in ("latest_user_goal", "current_progress", "pending_question"):
        context[key] = _clip(str(context.get(key) or ""), 800)
    for key in ("explicit_constraints", "authorized_actions", "forbidden_actions", "risk_flags"):
        context[key] = [_clip(str(item), 180) for item in _as_list(context.get(key))][-MAX_EVIDENCE:]
    turns = []
    for turn in _as_list(context.get("recent_turns"))[-MAX_RECENT_TURNS:]:
        if isinstance(turn, dict):
            turn = {**turn, "text": _clip(str(turn.get("text") or ""), 600)}
            turns.append(turn)
    context["recent_turns"] = turns
    context["evidence"] = [
        {**item, "quote": _clip(str(item.get("quote") or ""), 180)} if isinstance(item, dict) else item
        for item in _as_list(context.get("evidence"))[-MAX_EVIDENCE:]
    ]


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(part for part in parts if part)


def _extract_tool_text(payload: dict[str, Any]) -> str:
    for key in ("output", "raw_output", "arguments", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(payload, ensure_ascii=False, default=str)


def _extract_constraints(text: str) -> list[str]:
    constraints: list[str] = []
    for sentence in _sentences(text):
        if any(marker in sentence for marker in CONSTRAINT_MARKERS):
            constraints.append(_clip(_constraint_fragment(sentence), 220))
    return _dedupe(constraints)


def _constraint_fragment(sentence: str) -> str:
    candidate = sentence
    for separator in ("：", ":"):
        if separator not in sentence:
            continue
        fragment = sentence.split(separator, 1)[1].strip()
        if fragment and any(marker in fragment for marker in CONSTRAINT_MARKERS):
            candidate = fragment
            break
    marker_indexes = [candidate.find(marker) for marker in CONSTRAINT_MARKERS if marker in candidate]
    if marker_indexes:
        return candidate[min(marker_indexes) :].strip(" ，,")
    return candidate


def _forbidden_actions(text: str) -> list[str]:
    lowered = text.lower()
    has_prohibition = any(marker.lower() in lowered for marker in ("不要", "别", "禁止", "不得", "no ", "do not"))
    if not has_prohibition:
        return []
    return [flag.removeprefix("mentions:") for flag in detect_deterministic_risks(text)]


def _sentences(text: str) -> list[str]:
    chunks = re.split(r"(?<=[。！？!?])\s+|(?<=[。！？!?])|[\r\n]+", text.strip())
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _looks_like_question(text: str) -> bool:
    stripped = text.strip()
    return any(marker in stripped for marker in QUESTION_MARKERS)


def _verification_state(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("failed", "failure", "error", "失败", "报错")):
        return "failed"
    if any(marker in lowered for marker in ("passed", " pass", "all tests", "success", "验证通过", "通过")):
        return "passed"
    if any(marker in lowered for marker in ("not run", "未验证", "没有验证", "未运行")):
        return "not_run"
    return "unknown"


def _context_confidence(context: dict[str, Any]) -> str:
    if context.get("latest_user_goal") and context.get("recent_turns"):
        return "high"
    if context.get("recent_turns") or context.get("current_progress"):
        return "medium"
    return "low"


def _is_keep_going_injection(text: str) -> bool:
    return any(marker in text for marker in KEEP_GOING_INJECTION_MARKERS)


def _is_user_instructions_wrap(role: str, text: str) -> bool:
    stripped = text.strip()
    return role == "user" and stripped.startswith("<user_instructions>") and stripped.endswith("</user_instructions>")


def _is_system_envelope(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in SYSTEM_ENVELOPE_PREFIXES)


def _tail_start(path: Path, tail_bytes: int) -> int:
    if tail_bytes <= 0:
        return 0
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    return max(0, size - tail_bytes)


def _cache_cursor_matches(path: Path, cache: dict[str, Any]) -> bool:
    cached = str(cache.get("cursor_fingerprint") or "")
    offset = int(cache.get("last_offset") or -1)
    if not cached or offset < 0:
        return False
    return _cursor_fingerprint(path, offset) == cached


def _cursor_fingerprint(path: Path, offset: int) -> str:
    if offset < 0:
        return ""
    start = max(0, offset - CURSOR_FINGERPRINT_BYTES)
    length = max(0, offset - start)
    try:
        with path.open("rb") as f:
            f.seek(start)
            data = f.read(length)
    except OSError:
        return ""
    payload = f"{start}:{offset}:".encode("utf-8") + data
    return hashlib.sha256(payload).hexdigest()


def _locate_transcript_by_session_id(session_id: str) -> Path | None:
    safe_session_id = _cache_key(session_id, Path(session_id or ""))
    if not safe_session_id or safe_session_id != session_id:
        return None
    roots = []
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    roots.extend([base / "sessions", base / "archived_sessions"])
    matches: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        try:
            matches.extend(path for path in root.rglob(f"*{session_id}*.jsonl") if path.is_file())
        except OSError:
            continue
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime).resolve(strict=False)


def _cache_key(session_id: str, transcript_path: Path) -> str:
    if session_id:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", session_id).strip("-") or _path_digest(transcript_path)
    return _path_digest(transcript_path)


def _path_digest(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _json_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
