"""U6 tests: distill-mine one-click distillation pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from keep_going import cli
from keep_going.agents.distill import InsufficientCorpusError, distill_for_agent
from keep_going.agents.registry import load_meta
from keep_going.config import (
    Config,
    FiltersCfg,
    ModelsCfg,
    PathsCfg,
    ReasoningCfg,
    ScrubCfg,
    SourcesCfg,
    WindowCfg,
)

ROOT = Path(__file__).resolve().parents[1]


# ── helpers ──────────────────────────────────────────────────────────────────


def _config(root: Path) -> Config:
    return Config(
        window=WindowCfg(days=90),
        sources=SourcesCfg(
            claude_code_dir=root / "claude",
            codex_archived_dir=root / "codex",
            codex_history=root / "history.jsonl",
        ),
        paths=PathsCfg(data_dir=root / "data", artifacts_dir=root / "artifacts"),
        scrub=ScrubCfg(enabled=True, user_replacement="USER"),
        models=ModelsCfg(reasoning="r", eval="e", decision="t"),
        reasoning=ReasoningCfg(max_content_chars=4000, max_prev_assistant_chars=2000, concurrency=1),
        filters=FiltersCfg(min_user_chars=4, skip_command_only=True),
        root=root,
    )


def _seed_labeled(data_dir: Path, n_user_turns: int = 30) -> Path:
    """Write a minimal labeled.jsonl with exactly n_user_turns user rows."""
    labels_dir = data_dir / "labels"
    labels_dir.mkdir(parents=True, exist_ok=True)
    labeled = labels_dir / "labeled.jsonl"
    rows: list[str] = []
    idx = 0
    for i in range(n_user_turns):
        row = {
            "turn_id": f"test-u{i:04d}",
            "source": "claude-code",
            "session_id": "sess-001",
            "ts": f"2026-06-01T00:{i % 60:02d}:00",
            "project": "/tmp/test",
            "role": "user",
            "content": f"user turn content {i}",
            "prev_assistant": f"assistant reply {i}",
            "turn_idx": idx,
            "meta": {},
            "labels": ["execute-short", "verification-demand"] if i % 3 == 0 else ["rejection"],
        }
        rows.append(json.dumps(row, ensure_ascii=False))
        idx += 1
        # interleave assistant turns
        rows.append(json.dumps({
            "turn_id": f"test-a{i:04d}",
            "source": "claude-code",
            "session_id": "sess-001",
            "ts": f"2026-06-01T00:{i % 60:02d}:01",
            "project": "/tmp/test",
            "role": "assistant",
            "content": f"assistant reply {i}",
            "prev_assistant": None,
            "turn_idx": idx,
            "meta": {},
            "labels": [],
        }, ensure_ascii=False))
        idx += 1
    labeled.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return labeled


# ── distill_for_agent ─────────────────────────────────────────────────────────


def test_distill_success(tmp_path: Path):
    """30-turn corpus produces a decision policy file + updated meta.json."""
    agent_home = tmp_path / "agents"
    data_dir = tmp_path / "data"
    _seed_labeled(data_dir, n_user_turns=30)

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    cfg = _config(tmp_path)

    with patch("keep_going.agents.distill.harvest"), patch("keep_going.agents.distill.classify_all"):
        policy_path = distill_for_agent(
            "my-agent",
            cfg=cfg,
            agent_home=agent_home,
        )

    assert policy_path.exists()
    content = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert content["status"] == "candidate"
    assert content["version"] == 0.5

    meta = load_meta(agent_home / "my-agent")
    assert meta["current_policy"] == str(policy_path)
    assert meta["name"] == "my-agent"


def test_distill_insufficient_corpus(tmp_path: Path):
    """5-turn corpus raises InsufficientCorpusError."""
    data_dir = tmp_path / "data"
    _seed_labeled(data_dir, n_user_turns=5)
    cfg = _config(tmp_path)

    with patch("keep_going.agents.distill.harvest"), patch("keep_going.agents.distill.classify_all"):
        try:
            distill_for_agent("tiny-agent", cfg=cfg, agent_home=tmp_path / "agents")
        except InsufficientCorpusError as exc:
            assert "insufficient corpus" in str(exc)
            assert "5 user turns" in str(exc)
        else:
            raise AssertionError("expected InsufficientCorpusError")


def test_distill_rejects_reserved_name(tmp_path: Path):
    """Reserved name 'default' is rejected."""
    cfg = _config(tmp_path)
    try:
        distill_for_agent("default", cfg=cfg, agent_home=tmp_path / "agents")
    except ValueError as exc:
        assert "reserved" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError")


def test_distill_twice_produces_two_policy_files(tmp_path: Path):
    """Running distill twice creates two decision policy files with history in meta.json."""
    data_dir = tmp_path / "data"
    _seed_labeled(data_dir, n_user_turns=30)
    agent_home = tmp_path / "agents"
    cfg = _config(tmp_path)

    with patch("keep_going.agents.distill.harvest"), patch("keep_going.agents.distill.classify_all"):
        import time
        first = distill_for_agent("iter-agent", cfg=cfg, agent_home=agent_home)
        time.sleep(0.01)
        second = distill_for_agent("iter-agent", cfg=cfg, agent_home=agent_home)

    assert first != second
    assert first.exists()
    assert second.exists()

    meta = load_meta(agent_home / "iter-agent")
    assert meta["current_policy"] == str(second)
    assert len(meta["history"]) == 1
    assert meta["history"][0]["path"] == str(first)


def test_distill_mine_cli_smoke(monkeypatch, tmp_path: Path):
    """CLI smoke test for distill-mine."""
    data_dir = tmp_path / "data"
    _seed_labeled(data_dir, n_user_turns=30)
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(tmp_path / "agents"))

    with patch("keep_going.agents.distill.harvest"), patch("keep_going.agents.distill.classify_all"):
        result = CliRunner().invoke(
            cli.main,
            [
                "distill-mine",
                "--name", "cli-agent",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "distilled agent decision policy" in result.output
