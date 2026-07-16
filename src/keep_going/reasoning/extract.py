"""Per-turn reasoning extraction via Claude API."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from anthropic import Anthropic, APIError
from rich.console import Console
from rich.progress import Progress
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import Config
from .prompts import SYSTEM_PROMPT, render_user_prompt

console = Console()


def _truncate(s: str | None, max_chars: int) -> str:
    if not s:
        return ""
    if len(s) <= max_chars:
        return s
    return s[: max_chars // 2] + "\n...[truncated]...\n" + s[-max_chars // 2 :]


def _cache_key(prev: str, content: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(b"|")
    h.update(prev.encode())
    h.update(b"|")
    h.update(content.encode())
    return h.hexdigest()[:24]


class ReasoningCache:
    """Content-hash cache to avoid re-paying for unchanged turns."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (OSError, json.JSONDecodeError):
                self._data = {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self._data.get(key)

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._data[key] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False)
        tmp.replace(self.path)


@retry(
    retry=retry_if_exception_type(APIError),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(4),
    reraise=True,
)
def _call_api(client: Anthropic, *, model: str, system: str, user: str) -> dict[str, Any]:
    resp = client.messages.create(
        model=model,
        max_tokens=600,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def extract_one(
    client: Anthropic,
    *,
    model: str,
    turn: dict[str, Any],
    cfg: Config,
    cache: ReasoningCache,
) -> dict[str, Any] | None:
    if turn.get("role") != "user":
        return None
    content = turn.get("content", "")
    prev = turn.get("prev_assistant") or ""
    content_t = _truncate(content, cfg.reasoning.max_content_chars)
    prev_t = _truncate(prev, cfg.reasoning.max_prev_assistant_chars)
    key = _cache_key(prev_t, content_t, model)

    cached = cache.get(key)
    if cached is not None:
        return cached

    user_prompt = render_user_prompt(
        project=turn.get("project", ""),
        ts=turn.get("ts", ""),
        prev_assistant=prev_t,
        user_content=content_t,
    )
    try:
        result = _call_api(client, model=model, system=SYSTEM_PROMPT, user=user_prompt)
    except (APIError, json.JSONDecodeError, ValueError) as e:
        result = {"error": str(e), "intent": "unknown"}

    cache.put(key, result)
    return result


def reason(cfg: Config, *, limit: int | None = None, model: str | None = None) -> Path:
    in_path = cfg.paths.data_dir / "turns" / "turns.jsonl"
    if not in_path.exists():
        raise FileNotFoundError(f"missing {in_path}; run `keep-going harvest` first")

    out_dir = cfg.paths.data_dir / "reasoned"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "reasoned.jsonl"
    cache_path = out_dir / "cache.json"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set; export it before running reason")

    client = Anthropic(api_key=api_key)
    cache = ReasoningCache(cache_path)
    use_model = model or cfg.models.reasoning

    user_turns: list[dict[str, Any]] = []
    with in_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("role") == "user":
                user_turns.append(obj)
            if limit is not None and len(user_turns) >= limit:
                break

    console.print(f"[cyan]reasoning[/cyan] over {len(user_turns)} user turns with {use_model}")

    results: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    with Progress(console=console) as prog:
        task = prog.add_task("reason", total=len(user_turns))
        with ThreadPoolExecutor(max_workers=cfg.reasoning.concurrency) as ex:
            futs = {
                ex.submit(extract_one, client, model=use_model, turn=t, cfg=cfg, cache=cache): t
                for t in user_turns
            }
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    r = fut.result()
                except Exception as e:  # noqa: BLE001
                    r = {"error": str(e)}
                results.append((t, r))
                prog.update(task, advance=1)
                if len(results) % 50 == 0:
                    cache.save()

    cache.save()

    with out_path.open("w", encoding="utf-8") as f:
        for turn, r in results:
            row = {
                "turn_id": turn["turn_id"],
                "source": turn["source"],
                "session_id": turn["session_id"],
                "ts": turn["ts"],
                "project": turn["project"],
                "content": turn["content"],
                "prev_assistant": turn.get("prev_assistant"),
                "reasoning": r,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    console.print(f"[green]reason done[/green] → {out_path}  (cache: {cache_path})")
    return out_path
