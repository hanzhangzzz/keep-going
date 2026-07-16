from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from keep_going import cli
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
from keep_going.patterns.distill import distill_candidate


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
        models=ModelsCfg(reasoning="reasoning", eval="eval", decision="keep-going-model"),
        reasoning=ReasoningCfg(max_content_chars=4000, max_prev_assistant_chars=2000, concurrency=1),
        filters=FiltersCfg(min_user_chars=4, skip_command_only=True),
        root=root,
    )


def _write_labeled(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "turn_id": "verify1",
            "ts": "2026-05-01T00:00:00Z",
            "role": "user",
            "content": "你确定吗？我要完整证据链。",
            "labels": ["verification-demand", "evidence-probe"],
        },
        {
            "turn_id": "scope1",
            "ts": "2026-05-02T00:00:00Z",
            "role": "user",
            "content": "不要顺手改无关文件。",
            "labels": ["scope-correction"],
        },
        {
            "turn_id": "choice1",
            "ts": "2026-05-03T00:00:00Z",
            "role": "user",
            "content": "选第二种吧。",
            "labels": ["choice-among-options"],
        },
    ]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_distill_candidate_writes_reviewable_yaml(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_labeled(cfg.paths.data_dir / "labels" / "labeled.jsonl")

    out = distill_candidate(cfg, out_path=tmp_path / "candidate.yaml", limit_per_signal=2)

    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["status"] == "candidate"
    assert data["distill_mode"] == "deterministic-label-baseline"
    assert data["coverage"]["total_user_turns"] == 3
    assert data["coverage"]["label_counts"]["verification-demand"] == 1
    assert data["core_principles"][0]["id"] == "scope-fidelity"
    assert "scope1" in data["core_principles"][0]["evidence_turn_ids"]
    assert data["heuristics"][1]["id"] == "when-multiple-options"
    assert "choice1" in data["heuristics"][1]["evidence_turn_ids"]


def test_distill_cli_outputs_candidate_path(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_labeled(cfg.paths.data_dir / "labels" / "labeled.jsonl")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    result = CliRunner().invoke(cli.main, ["distill", "--out", str(tmp_path / "candidate.yaml")])

    assert result.exit_code == 0
    assert "wrote candidate decision policy" in result.output
    assert (tmp_path / "candidate.yaml").exists()
