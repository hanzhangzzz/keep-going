"""One-click per-agent distillation (U6, experimental).

Orchestrates harvest → classify → distill_candidate for a named agent,
writing the output decision policy to ``~/.keep-going/agents/<name>/policy-<ts>.yaml`` and
updating the agent's ``meta.json``.

Privacy / isolation boundaries (hard requirements from the framework plan):
1. **Local sessions are read-only** — harvest only reads ``~/.claude`` and
   ``~/.codex``; never writes to them.
2. **Template / canonical decision policy is untouched** — this module does not modify
   ``artifacts/decision-policy.yaml``.
3. **Local processing only** — no Anthropic API calls.
4. **No repo writes** — output goes to ``~/.keep-going/agents/<name>/``, never
   into the git worktree.
5. **No uploads** — no telemetry, no remote backup.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .registry import (
    AGENT_DIR_MODE,
    POLICY_FILE_MODE,
    _agent_root,
    save_meta,
    validate_agent_name,
)
from ..config import Config, load_config
from ..corpus.classify import classify_all
from ..corpus.harvest import harvest
from ..patterns.distill import distill_candidate

MINIMUM_USER_TURNS = 10


class InsufficientCorpusError(RuntimeError):
    """Raised when harvested corpus has fewer than MINIMUM_USER_TURNS user turns."""


def distill_for_agent(
    name: str,
    *,
    project: str | None = None,
    cfg: Config | None = None,
    window_days: int | None = None,
    agent_home: Path | None = None,
) -> Path:
    """Run full distillation pipeline for a named agent.

    Steps:
    1. Validate name (reject reserved names including ``default``).
    2. Harvest user sessions (read-only on source dirs).
    3. Classify (local rule engine).
    4. Distill deterministic candidate decision policy.
    5. Write output to agent dir + update ``meta.json``.

    Returns the path to the newly written decision policy file.

    Raises:
        ValueError: name fails validation.
        InsufficientCorpusError: fewer than MINIMUM_USER_TURNS user turns.
    """
    validation = validate_agent_name(name)
    if not validation["ok"]:
        raise ValueError(validation["reason"])

    resolved_cfg = cfg or load_config()

    harvest_kwargs: dict[str, Any] = {}
    if window_days is not None:
        harvest_kwargs["window_days"] = window_days
    harvest(resolved_cfg, **harvest_kwargs)

    classify_all(resolved_cfg)

    labeled_path = resolved_cfg.paths.data_dir / "labels" / "labeled.jsonl"
    user_turn_count = _count_user_turns(labeled_path)
    if user_turn_count < MINIMUM_USER_TURNS:
        raise InsufficientCorpusError(
            f"insufficient corpus: {user_turn_count} user turns "
            f"(minimum {MINIMUM_USER_TURNS})"
        )

    agent_dir = _resolve_agent_dir(name, project, agent_home)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"
    policy_path = agent_dir / f"policy-{ts}.yaml"

    distill_candidate(resolved_cfg, out_path=policy_path)

    _update_meta(agent_dir, name, policy_path)

    return policy_path


def _resolve_agent_dir(name: str, project: str | None, agent_home: Path | None) -> Path:
    """Determine agent directory (project-tier, explicit home, or global-tier)."""
    if agent_home:
        agent_dir = agent_home / name
    elif project:
        agent_dir = Path(project) / ".keep-going" / "agents" / name
    else:
        agent_dir = _agent_root() / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    try:
        agent_dir.chmod(AGENT_DIR_MODE)
    except OSError:
        pass
    return agent_dir


def _count_user_turns(labeled_path: Path) -> int:
    """Count user turns in a JSONL file (fast, no full parse)."""
    if not labeled_path.exists():
        return 0
    import json

    count = 0
    with labeled_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                if row.get("role") == "user":
                    count += 1
            except json.JSONDecodeError:
                continue
    return count


def _update_meta(agent_dir: Path, name: str, policy_path: Path) -> None:
    """Update or create agent meta.json with new current_policy."""
    from .registry import load_meta

    existing = load_meta(agent_dir)
    now = datetime.now(timezone.utc).isoformat()
    history = existing.get("history", [])
    prev_policy = existing.get("current_policy")
    if prev_policy:
        history.append({"ts": existing.get("updated_at", ""), "path": prev_policy})

    meta = {
        "name": name,
        "description": existing.get("description", f"Distilled agent: {name}"),
        "created_at": existing.get("created_at", now),
        "updated_at": now,
        "current_policy": str(policy_path),
        "history": history,
    }
    save_meta(agent_dir, meta)
