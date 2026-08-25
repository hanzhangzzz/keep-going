"""Harvest: walk both sources, normalize, scrub, filter, write data/turns/turns.jsonl."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import Progress

from ..config import Config
from .adapters import claude_code as cc
from .adapters import codex as cx
from .schema import Turn
from .scrub import scrub

console = Console()


_ENVELOPE_PREFIXES = (
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
    "<hook_prompt",
    "<turn_aborted",
    "<task>",
    "<recommended_plugins",
)
_SYSTEM_INJECTED_PREFIXES = (
    "[Request interrupted",
    "[Request continued",
    "Base directory for this skill",
    "Caveat:",
    "This session is being continued",
    "<session-summary>",
    "Stop hook feedback:",
    "# AGENTS.md instructions",
)
# Machine-dispatched prompts (skill expansions, scheduled /loop re-fires, batch
# agent kickoffs) repeat verbatim; one copy is signal, the rest is spam.
_DEDUP_MIN_CHARS = 200


def _looks_like_command_only(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    # Pure slash command (no body)
    if t.startswith("/") and "\n" not in t and len(t) < 80:
        return True
    return False


def _is_envelope_noise(text: str) -> bool:
    t = text.lstrip()
    if not t:
        return True
    for p in _ENVELOPE_PREFIXES:
        if t.startswith(p):
            return True
    for p in _SYSTEM_INJECTED_PREFIXES:
        if t.startswith(p):
            return True
    return False


def should_keep_turn(turn: Turn, cfg: Config) -> bool:
    if turn.role == "user":
        if len(turn.content) < cfg.filters.min_user_chars:
            return False
        if cfg.filters.skip_command_only and _looks_like_command_only(turn.content):
            return False
        if _is_envelope_noise(turn.content):
            return False
    return True


def _apply_scrub(turn: Turn, cfg: Config) -> Turn:
    if not cfg.scrub.enabled:
        return turn
    repl = cfg.scrub.user_replacement
    scrubbed_meta = {
        k: scrub(v, user_replacement=repl) if isinstance(v, str) else v
        for k, v in turn.meta.items()
    }
    return turn.model_copy(
        update={
            "content": scrub(turn.content, user_replacement=repl),
            "prev_assistant": (
                scrub(turn.prev_assistant, user_replacement=repl) if turn.prev_assistant else None
            ),
            "project": scrub(turn.project, user_replacement=repl),
            "meta": scrubbed_meta,
        }
    )


def _all_turns(cfg: Config, since: datetime) -> Iterator[Turn]:
    yield from cc.iter_sessions(cfg.sources.claude_code_dir, since=since)
    yield from cx.iter_sessions(cfg.sources.codex_archived_dir, since=since)
    sessions_dir = cfg.sources.codex_sessions_dir
    if sessions_dir is not None and sessions_dir != cfg.sources.codex_archived_dir:
        yield from cx.iter_sessions(sessions_dir, since=since)


def harvest(cfg: Config, *, window_days: int | None = None, limit: int | None = None) -> Path:
    days = window_days or cfg.window.days
    since = datetime.now(timezone.utc) - timedelta(days=days)

    out_dir = cfg.paths.data_dir / "turns"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "turns.jsonl"

    counters = {"total": 0, "kept": 0, "filtered": 0, "user": 0, "assistant": 0}
    by_source: dict[str, int] = {}
    seen_turn_ids: set[str] = set()
    seen_long_user_content: set[str] = set()

    with out_path.open("w", encoding="utf-8") as f, Progress(console=console) as prog:
        task = prog.add_task("[cyan]harvesting", total=None)
        for turn in _all_turns(cfg, since):
            counters["total"] += 1
            if turn.turn_id in seen_turn_ids:
                counters["filtered"] += 1
                continue
            seen_turn_ids.add(turn.turn_id)
            if turn.ts < since:
                counters["filtered"] += 1
                continue
            if not should_keep_turn(turn, cfg):
                counters["filtered"] += 1
                continue
            if turn.role == "user" and len(turn.content) >= _DEDUP_MIN_CHARS:
                if turn.content in seen_long_user_content:
                    counters["filtered"] += 1
                    continue
                seen_long_user_content.add(turn.content)
            scrubbed = _apply_scrub(turn, cfg)
            f.write(scrubbed.model_dump_json() + "\n")
            counters["kept"] += 1
            counters[turn.role] += 1
            by_source[turn.source] = by_source.get(turn.source, 0) + 1
            prog.update(task, advance=1)
            if limit is not None and counters["kept"] >= limit:
                break

    console.print(
        f"[green]harvest done[/green]  total={counters['total']}  kept={counters['kept']}  "
        f"filtered={counters['filtered']}  user={counters['user']}  assistant={counters['assistant']}"
    )
    console.print(f"  by source: {by_source}")
    console.print(f"  → {out_path}")
    return out_path
