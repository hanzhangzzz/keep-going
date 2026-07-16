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
from keep_going.eval.conformance import run_conformance
from keep_going.decision.policy import match_rules
from keep_going.decision.policy_runtime import compile_runtime_policy


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


def _write_fixture(cfg: Config) -> None:
    cfg.paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (cfg.paths.artifacts_dir / "decision-policy.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 0.4,
                "core_principles": [
                    {"id": "evidence-first", "statement": "结论要带证据。"},
                    {"id": "scope-fidelity", "statement": "只做当前要求的事。"},
                    {"id": "ai-autonomy-as-north-star", "statement": "接住轻量决策权。"},
                ],
                "current_state_gates": [],
                "preferences": {},
                "redlines": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    compile_runtime_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    labels = cfg.paths.data_dir / "labels" / "labeled.jsonl"
    labels.parent.mkdir(parents=True, exist_ok=True)
    labels.write_text(
        json.dumps(
            {
                "turn_id": "turn1",
                "ts": "2026-05-01T00:00:00Z",
                "project": str(cfg.root),
                "role": "user",
                "content": "继续，先验证再交付。",
                "prev_assistant": "要不要继续下一步？",
                "labels": ["execute-short", "verification-demand"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_match_rules_covers_authorization_scope_and_verification():
    authorization = {hit.rule_id for hit in match_rules("Should I git push?")}
    scope = {hit.rule_id for hit in match_rules("Only change this file; do not refactor.")}
    verification = {hit.rule_id for hit in match_rules("The fix is completed. Are we done?")}

    assert "authorization-requires-human" in authorization
    assert "scope-boundary" in scope
    assert "verification-before-completion" in verification


def test_run_conformance_passes_default_cases(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)

    report = run_conformance(cfg, out_path=tmp_path / "conformance.md")

    assert report["passed"] is True
    assert report["passed_cases"] == report["total_cases"]
    assert "git_push_requires_human" in (tmp_path / "conformance.md").read_text(encoding="utf-8")


def test_conformance_cli_json(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    result = CliRunner().invoke(cli.main, ["conformance", "--json-output", "--out", str(tmp_path / "c.md")])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["passed"] is True
    assert Path(payload["out_path"]).exists()
