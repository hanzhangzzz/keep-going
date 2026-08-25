"""Load config.toml into a typed object."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WindowCfg:
    days: int


@dataclass(frozen=True)
class SourcesCfg:
    claude_code_dir: Path
    codex_archived_dir: Path
    codex_history: Path
    codex_sessions_dir: Path | None = None


@dataclass(frozen=True)
class PathsCfg:
    data_dir: Path
    artifacts_dir: Path


@dataclass(frozen=True)
class ScrubCfg:
    enabled: bool
    user_replacement: str


@dataclass(frozen=True)
class ModelsCfg:
    reasoning: str
    eval: str
    decision: str


@dataclass(frozen=True)
class ReasoningCfg:
    max_content_chars: int
    max_prev_assistant_chars: int
    concurrency: int


@dataclass(frozen=True)
class FiltersCfg:
    min_user_chars: int
    skip_command_only: bool


@dataclass(frozen=True)
class Config:
    window: WindowCfg
    sources: SourcesCfg
    paths: PathsCfg
    scrub: ScrubCfg
    models: ModelsCfg
    reasoning: ReasoningCfg
    filters: FiltersCfg
    root: Path


def _expand(p: str, root: Path) -> Path:
    path = Path(p).expanduser()
    if not path.is_absolute():
        path = (root / path).resolve()
    return path


def load_config(path: Path | None = None) -> Config:
    root = Path(__file__).resolve().parents[2]
    cfg_path = path or (root / "config.toml")
    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)
    user_home_raw = os.environ.get("KEEP_GOING_USER_HOME", "").strip()
    user_home = Path(user_home_raw).expanduser().resolve() if user_home_raw else None
    return Config(
        window=WindowCfg(**raw["window"]),
        sources=SourcesCfg(
            claude_code_dir=_expand(raw["sources"]["claude_code_dir"], root),
            codex_archived_dir=_expand(raw["sources"]["codex_archived_dir"], root),
            codex_history=_expand(raw["sources"]["codex_history"], root),
            codex_sessions_dir=(
                _expand(raw["sources"]["codex_sessions_dir"], root)
                if raw["sources"].get("codex_sessions_dir")
                else Path.home() / ".codex" / "sessions"
            ),
        ),
        paths=PathsCfg(
            data_dir=user_home / "data" if user_home else _expand(raw["paths"]["data_dir"], root),
            artifacts_dir=(
                user_home / "artifacts" if user_home else _expand(raw["paths"]["artifacts_dir"], root)
            ),
        ),
        scrub=ScrubCfg(**raw["scrub"]),
        models=ModelsCfg(**raw["models"]),
        reasoning=ReasoningCfg(**raw["reasoning"]),
        filters=FiltersCfg(**raw["filters"]),
        root=root,
    )
