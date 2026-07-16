from __future__ import annotations

import os
import shlex
import subprocess
import sys
import json
import tomllib
from pathlib import Path

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
from keep_going.integration.install import _codex_marketplace_path


ROOT = Path(__file__).resolve().parents[1]


def _decision_command() -> str:
    payload = {
        "action": "block",
        "reply": "继续跑验证。",
        "reason": "cli_model_blocked",
        "confidence": 0.9,
        "category": "verification",
        "evidence": [{"source": "test-cli", "id": "self-test"}],
    }
    script = f"import json,sys; sys.stdin.read(); print({json.dumps(json.dumps(payload, ensure_ascii=False))})"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


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


def _codex_stop_hook_entries(codex_home: Path) -> list[dict[str, object]]:
    hooks_json = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    hooks: list[dict[str, object]] = []
    for entry in hooks_json["hooks"]["Stop"]:
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict):
                hooks.append(hook)
    return hooks


def _codex_stop_hook_commands(codex_home: Path) -> list[str]:
    commands: list[str] = []
    for hook in _codex_stop_hook_entries(codex_home):
        commands.append(str(hook.get("command") or ""))
    return commands


def _keep_going_codex_stop_hook(codex_home: Path) -> dict[str, object]:
    for hook in _codex_stop_hook_entries(codex_home):
        command = str(hook.get("command") or "")
        if "KEEP_GOING_HOST=codex" in command and "keep-going-stop-hook.sh" in command:
            return hook
    raise AssertionError("missing Keep Going Codex Stop hook")


def test_agent_definition_is_valid_toml():
    data = tomllib.loads((ROOT / ".codex" / "agents" / "keep-going.toml").read_text(encoding="utf-8"))

    assert data["name"] == "keep-going"
    assert "scripts/03-reply.sh" in data["developer_instructions"]
    assert "scripts/04-mcp.sh" in data["developer_instructions"]


def test_install_integration_dry_run_lists_command_agent_and_mcp(tmp_path: Path):
    codex_home = tmp_path / "codex-home"
    agents_home = tmp_path / "agents-home"
    claude_home = tmp_path / "claude-home"
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "install-integration.sh"),
            "--dry-run",
            "--codex-home",
            str(codex_home),
            "--agents-home",
            str(agents_home),
            "--claude-home",
            str(claude_home),
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "codex_legacy_skill_cleanup:" in result.stdout
    assert "codex_legacy_prompt_cleanup:" in result.stdout
    assert "codex_legacy_command_cleanup:" in result.stdout
    assert "codex_slash_commands: unsupported" in result.stdout
    assert ".codex/agents/keep-going.toml" in result.stdout
    assert "plugins/keep-going" in result.stdout
    assert ".agents/plugins/marketplace.json" in result.stdout
    assert "agents_home:" in result.stdout
    assert "claude_home:" in result.stdout
    assert "marketplace.json" in result.stdout
    assert "scripts/04-mcp.sh" in result.stdout
    assert ".claude-plugin/marketplace.json" in result.stdout
    assert "bridge_enable:" in result.stdout


def test_install_dry_run_uses_temp_agents_home_when_codex_home_is_custom(tmp_path: Path):
    codex_home = tmp_path / "codex-home"

    result = subprocess.run(
        [
            str(ROOT / "scripts" / "install-integration.sh"),
            "--dry-run",
            "--codex-home",
            str(codex_home),
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    assert f"agents_home: {codex_home / '.agents'}" in result.stdout


def test_install_cli_dry_run_lists_command_agent_and_mcp(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(cli, "load_config", lambda: _config(ROOT))

    result = CliRunner().invoke(
        cli.main,
        [
            "install",
            "--codex-home",
            str(tmp_path / "codex-home"),
            "--agents-home",
            str(tmp_path / "agents-home"),
            "--claude-home",
            str(tmp_path / "claude-home"),
        ],
    )

    assert result.exit_code == 0
    assert "codex_legacy_skill_cleanup:" in result.output
    assert ".codex/agents/keep-going.toml" in result.output
    assert "scripts/04-mcp.sh" in result.output
    assert "plugins/keep-going" in result.output
    assert "marketplace.json" in result.output
    assert "claude_home:" in result.output
    assert "dry-run only" in result.output


def test_install_cli_execute_to_temp_codex_home(monkeypatch, tmp_path: Path):
    codex_home = tmp_path / "codex-home"
    agents_home = tmp_path / "agents-home"
    claude_home = tmp_path / "claude-home"
    monkeypatch.setattr(cli, "load_config", lambda: _config(ROOT))
    legacy_skill = codex_home / "skills" / "keep-going" / "SKILL.md"
    legacy_skill.parent.mkdir(parents=True)
    legacy_skill.write_text("Keep Going legacy skill\n", encoding="utf-8")
    legacy_prompt = codex_home / "prompts" / "keep-going-setup.md"
    legacy_prompt.parent.mkdir(parents=True)
    legacy_prompt.write_text("Keep Going legacy prompt\n", encoding="utf-8")
    legacy_command = codex_home / "commands" / "keep-going.md"
    legacy_command.parent.mkdir(parents=True)
    legacy_command.write_text("Keep Going legacy command\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli.main,
        [
            "install",
            "--execute",
            "--codex-home",
            str(codex_home),
            "--agents-home",
            str(agents_home),
            "--claude-home",
            str(claude_home),
        ],
    )

    assert result.exit_code == 0
    assert not (codex_home / "skills" / "keep-going").exists()
    assert not legacy_prompt.exists()
    assert not legacy_command.exists()
    assert (codex_home / "agents" / "keep-going.toml").exists()
    assert (agents_home / "plugins" / "keep-going" / ".codex-plugin" / "plugin.json").exists()
    assert (agents_home / "plugins" / "keep-going" / "plugin.json").exists()
    assert (agents_home / "plugins" / "keep-going" / ".claude-plugin" / "plugin.json").exists()
    assert (agents_home / "plugins" / "keep-going" / "hooks" / "keep-going-decision-hook.sh").exists()
    assert (agents_home / "plugins" / "keep-going" / "hooks" / "keep-going-stop-hook.sh").exists()
    assert (agents_home / "plugins" / "keep-going" / "scripts" / "bridge.sh").exists()
    commands = _codex_stop_hook_commands(codex_home)
    assert any("KEEP_GOING_HOST=codex" in command and "keep-going-stop-hook.sh" in command for command in commands)
    assert _keep_going_codex_stop_hook(codex_home)["timeout"] == 360
    assert (agents_home / "plugins" / "keep-going" / "runtime-root").read_text(encoding="utf-8").strip() == str(ROOT)
    assert (agents_home / "plugins" / "keep-going" / ".repo-root").read_text(encoding="utf-8").strip() == str(ROOT)
    assert not (agents_home / "plugins" / "marketplace.json").exists()
    assert (claude_home / "plugins" / "marketplaces" / "keep-going-local" / ".claude-plugin" / "marketplace.json").exists()
    assert (
        claude_home / "plugins" / "marketplaces" / "keep-going-local" / "plugins" / "keep-going" / ".claude-plugin" / "plugin.json"
    ).exists()
    assert (
        claude_home / "plugins" / "marketplaces" / "keep-going-local" / "plugins" / "keep-going" / "runtime-root"
    ).read_text(encoding="utf-8").strip() == str(ROOT)
    assert "removed legacy Codex skill" in result.output
    assert "removed legacy Codex prompt" in result.output
    assert "removed legacy Codex command" in result.output
    assert "codex slash commands unsupported" in result.output
    assert "installed agent" in result.output
    assert "installed plugin" in result.output
    assert "codex marketplace registration" in result.output
    assert "installed Claude plugin" in result.output
    assert "codex native Stop hook: installed" in result.output

    verify = CliRunner().invoke(
        cli.main,
        [
            "install",
            "--verify",
            "--codex-home",
            str(codex_home),
            "--agents-home",
            str(agents_home),
            "--claude-home",
            str(claude_home),
        ],
    )

    assert verify.exit_code == 0
    assert "Keep Going install verification" in verify.output
    assert "status: PASS" in verify.output
    assert "codex_legacy_skill_removed: PASS" in verify.output
    assert "codex_legacy_prompts_removed: PASS" in verify.output
    assert "codex_legacy_commands_removed: PASS" in verify.output
    assert "plugin_hook: PASS" in verify.output
    assert "plugin_stop_hook: PASS" in verify.output
    assert "codex_native_stop_hook: PASS" in verify.output
    assert "plugin_runtime_root: PASS" in verify.output
    assert "claude_plugin_manifest: PASS" in verify.output
    assert "claude_plugin_runtime_root: PASS" in verify.output
    assert "plugin_repo_runtime: PASS" in verify.output


def test_install_cli_preserves_existing_codex_stop_hooks(monkeypatch, tmp_path: Path):
    codex_home = tmp_path / "codex-home"
    agents_home = tmp_path / "agents-home"
    claude_home = tmp_path / "claude-home"
    codex_home.mkdir(parents=True)
    (codex_home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "'/tmp/notify.sh' >/dev/null 2>&1 || true",
                                }
                            ]
                        }
                    ]
                },
                "state": {"existing": {"trusted_hash": "sha256:abc"}},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "load_config", lambda: _config(ROOT))

    result = CliRunner().invoke(
        cli.main,
        [
            "install",
            "--execute",
            "--codex-home",
            str(codex_home),
            "--agents-home",
            str(agents_home),
            "--claude-home",
            str(claude_home),
        ],
    )

    assert result.exit_code == 0
    hooks_json = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    commands = _codex_stop_hook_commands(codex_home)
    assert "'/tmp/notify.sh' >/dev/null 2>&1 || true" in commands
    assert any("KEEP_GOING_HOST=codex" in command and "keep-going-stop-hook.sh" in command for command in commands)
    assert _keep_going_codex_stop_hook(codex_home)["timeout"] == 360
    assert hooks_json["state"] == {"existing": {"trusted_hash": "sha256:abc"}}


def test_start_cli_installs_enables_and_verifies_to_temp_homes(monkeypatch, tmp_path: Path):
    codex_home = tmp_path / "codex-home"
    agents_home = tmp_path / "agents-home"
    claude_home = tmp_path / "claude-home"
    state_home = tmp_path / "state-home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("KEEP_GOING_CODEX_CLI_COMMAND", _decision_command())
    monkeypatch.setattr(cli, "load_config", lambda: _config(ROOT))

    result = CliRunner().invoke(
        cli.main,
        [
            "start",
            "--project",
            str(project),
            "--codex-home",
            str(codex_home),
            "--agents-home",
            str(agents_home),
            "--claude-home",
            str(claude_home),
            "--state-home",
            str(state_home),
            "--register-hosts",
            "none",
        ],
    )

    assert result.exit_code == 0
    assert "codex native Stop hook: installed" in result.output
    assert "codex_native_stop_hook: PASS" in result.output
    assert "enabled: True" in result.output
    assert "host: codex" in result.output
    assert "Keep Going bridge self-test: PASS" in result.output
    assert "Keep Going start verified." in result.output
    state_files = list(state_home.glob("*/state.json"))
    assert len(state_files) == 1
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert state["enabled"] is True
    assert state["host"] == "codex"


def test_sync_local_cli_refreshes_runtime_and_installed_surfaces(monkeypatch, tmp_path: Path):
    codex_home = tmp_path / "codex-home"
    agents_home = tmp_path / "agents-home"
    claude_home = tmp_path / "claude-home"
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setattr(cli, "load_config", lambda: _config(ROOT))

    result = CliRunner().invoke(
        cli.main,
        [
            "sync-local",
            "--codex-home",
            str(codex_home),
            "--agents-home",
            str(agents_home),
            "--claude-home",
            str(claude_home),
            "--runtime-home",
            str(runtime_home),
            "--register-hosts",
            "none",
        ],
    )

    assert result.exit_code == 0
    assert "Keep Going local runtime sync" in result.output
    assert "prepared Keep Going runtime" in result.output
    assert "codex native Stop hook: installed" in result.output
    assert "codex_native_stop_hook: PASS" in result.output
    assert "Keep Going local sync verified." in result.output
    assert "source_sha256:" in result.output
    assert "runtime_sha256:" in result.output
    assert "installed_runtime_sha256:" in result.output
    assert (runtime_home / "0.1.0" / "src" / "keep_going" / "cli.py").exists()
    assert (runtime_home / "0.1.0" / "artifacts" / "decision-policy.runtime.yaml").read_bytes() == (
        ROOT / "artifacts" / "decision-policy.runtime.yaml"
    ).read_bytes()
    commands = _codex_stop_hook_commands(codex_home)
    assert any("KEEP_GOING_HOST=codex" in command and "keep-going-stop-hook.sh" in command for command in commands)
    assert _keep_going_codex_stop_hook(codex_home)["timeout"] == 360


def test_sync_local_rejects_npm_staging_runtime_target(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(cli, "load_config", lambda: _config(ROOT))
    staging_link = tmp_path / "npm-link"
    staging_link.symlink_to(ROOT / "packages" / "npm", target_is_directory=True)

    result = CliRunner().invoke(
        cli.main,
        [
            "sync-local",
            "--runtime-home",
            str(staging_link),
            "--runtime-version",
            "runtime",
            "--no-verify",
        ],
    )

    assert result.exit_code != 0
    assert "runtime target must be outside npm package staging" in result.output


def test_install_cli_force_replaces_existing_temp_install(monkeypatch, tmp_path: Path):
    codex_home = tmp_path / "codex-home"
    agents_home = tmp_path / "agents-home"
    claude_home = tmp_path / "claude-home"
    monkeypatch.setattr(cli, "load_config", lambda: _config(ROOT))
    base_args = [
        "install",
        "--execute",
        "--codex-home",
        str(codex_home),
        "--agents-home",
        str(agents_home),
        "--claude-home",
        str(claude_home),
    ]

    first = CliRunner().invoke(cli.main, base_args)
    second = CliRunner().invoke(cli.main, base_args)
    forced = CliRunner().invoke(cli.main, [*base_args, "--force"])

    assert first.exit_code == 0
    assert second.exit_code != 0
    assert "refusing to overwrite" in second.output
    assert forced.exit_code == 0
    assert (agents_home / "plugins" / "keep-going" / ".repo-root").read_text(encoding="utf-8").strip() == str(ROOT)


def test_install_cli_execute_registers_detected_host_plugins(monkeypatch, tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "host-commands.log"
    claude = fake_bin / "claude"
    codex = fake_bin / "codex"
    claude.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'claude %s\\n' "$*" >> "$KEEP_GOING_FAKE_LOG"
if [[ "${1:-}" == "plugin" && "${2:-}" == "marketplace" && "${3:-}" == "list" ]]; then
  echo "Configured marketplaces:"
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "marketplace" && "${3:-}" == "add" ]]; then
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "list" && "${3:-}" == "--json" ]]; then
  echo "[]"
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "install" && "${3:-}" == "keep-going@keep-going-local" ]]; then
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "enable" && "${3:-}" == "keep-going@keep-going-local" ]]; then
  exit 0
fi
echo "unexpected claude args: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    codex.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'codex %s\\n' "$*" >> "$KEEP_GOING_FAKE_LOG"
if [[ "${1:-}" == "plugin" && "${2:-}" == "list" ]]; then
  if [[ "${3:-}" == "--marketplace" && "${4:-}" == "keep-going-local" ]]; then
    echo 'Marketplace `keep-going-local`'
    echo '  keep-going@keep-going-local (not installed)'
  fi
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "marketplace" && "${3:-}" == "list" ]]; then
  echo "Configured marketplaces:"
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "marketplace" && "${3:-}" == "add" ]]; then
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "marketplace" && "${3:-}" == "remove" ]]; then
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "add" && "${3:-}" == "keep-going@keep-going-local" ]]; then
  exit 0
fi
echo "unexpected codex args: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    codex.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("KEEP_GOING_FAKE_LOG", str(log))
    monkeypatch.setattr(cli, "load_config", lambda: _config(ROOT))

    result = CliRunner().invoke(cli.main, ["install", "--execute"])

    assert result.exit_code == 0
    assert "Keep Going host plugin registration" in result.output
    assert "claude-code marketplace: registered keep-going-local" in result.output
    assert "claude-code plugin: installed keep-going@keep-going-local" in result.output
    assert "codex plugin: installed keep-going@keep-going-local" in result.output
    commands = log.read_text(encoding="utf-8")
    assert f"claude plugin marketplace add {ROOT}" in commands
    assert "claude plugin install keep-going@keep-going-local" in commands
    assert f"codex plugin marketplace add {ROOT}" in commands
    assert "codex plugin list --marketplace keep-going-local" in commands
    assert "codex plugin add keep-going@keep-going-local" in commands


def test_install_cli_refreshes_stale_claude_plugin_cache(monkeypatch, tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    stale_cache = tmp_path / "claude-cache" / "keep-going"
    stale_cache.mkdir(parents=True)
    log = tmp_path / "host-commands.log"
    claude = fake_bin / "claude"
    claude.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'claude %s\\n' "$*" >> "$KEEP_GOING_FAKE_LOG"
if [[ "${1:-}" == "plugin" && "${2:-}" == "marketplace" && "${3:-}" == "list" ]]; then
  echo "keep-going-local"
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "list" && "${3:-}" == "--json" ]]; then
  printf '[{"id":"keep-going@keep-going-local","enabled":true,"installPath":"%s"}]\\n' "$KEEP_GOING_FAKE_CACHE"
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "uninstall" && "${3:-}" == "--keep-data" ]]; then
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "install" && "${3:-}" == "keep-going@keep-going-local" ]]; then
  exit 0
fi
echo "unexpected claude args: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("KEEP_GOING_FAKE_LOG", str(log))
    monkeypatch.setenv("KEEP_GOING_FAKE_CACHE", str(stale_cache))
    monkeypatch.setattr(cli, "load_config", lambda: _config(ROOT))

    result = CliRunner().invoke(cli.main, ["install", "--execute", "--register-hosts", "claude-code"])

    assert result.exit_code == 0
    assert "claude-code plugin: refreshed keep-going@keep-going-local" in result.output
    commands = log.read_text(encoding="utf-8")
    assert "claude plugin uninstall --keep-data -y keep-going@keep-going-local" in commands
    assert "claude plugin install keep-going@keep-going-local" in commands


def test_install_cli_refreshes_stale_codex_plugin_cache(monkeypatch, tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "host-commands.log"
    codex = fake_bin / "codex"
    codex.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'codex %s\\n' "$*" >> "$KEEP_GOING_FAKE_LOG"
if [[ "${1:-}" == "plugin" && "${2:-}" == "marketplace" && "${3:-}" == "list" ]]; then
  printf 'keep-going-local\\t%s\\n' "$KEEP_GOING_FAKE_RUNTIME"
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "list" ]]; then
  if [[ "${3:-}" == "--marketplace" && "${4:-}" == "keep-going-local" ]]; then
    echo 'Marketplace `keep-going-local`'
    echo '  keep-going@keep-going-local (installed, enabled)'
  fi
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "remove" && "${3:-}" == "keep-going@keep-going-local" ]]; then
  exit 0
fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "add" && "${3:-}" == "keep-going@keep-going-local" ]]; then
  exit 0
fi
echo "unexpected codex args: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("KEEP_GOING_FAKE_LOG", str(log))
    monkeypatch.setenv("KEEP_GOING_FAKE_RUNTIME", str(ROOT))
    monkeypatch.setattr(cli, "load_config", lambda: _config(ROOT))

    result = CliRunner().invoke(cli.main, ["install", "--execute", "--register-hosts", "codex"])

    assert result.exit_code == 0
    assert "codex plugin: refreshed keep-going@keep-going-local" in result.output
    commands = log.read_text(encoding="utf-8")
    assert "codex plugin remove keep-going@keep-going-local" in commands
    assert "codex plugin add keep-going@keep-going-local" in commands


def test_codex_marketplace_path_parses_current_table_format():
    output = """MARKETPLACE             ROOT
keep-going-local           /Users/USER/.keep-going/runtime/0.1.0
openai-bundled          /Users/USER/.codex/.tmp/bundled-marketplaces/openai-bundled
"""

    assert _codex_marketplace_path(output, "keep-going-local") == "/Users/USER/.keep-going/runtime/0.1.0"


def test_install_cli_verify_reports_missing_targets(tmp_path: Path):
    result = CliRunner().invoke(
        cli.main,
        [
            "install",
            "--verify",
            "--codex-home",
            str(tmp_path / "missing-codex"),
            "--agents-home",
            str(tmp_path / "missing-agents"),
            "--claude-home",
            str(tmp_path / "missing-claude"),
        ],
    )

    assert result.exit_code != 0
    assert "status: FAIL" in result.output
    assert "skill: MISSING" in result.output


def test_plugin_manifest_and_marketplace_are_valid_json():
    manifest = json.loads(
        (ROOT / "plugins" / "keep-going" / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))

    root_manifest = json.loads((ROOT / "plugins" / "keep-going" / "plugin.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "keep-going"
    assert root_manifest == manifest
    assert manifest["skills"] == "./skills/"
    assert manifest["commands"] == "./commands/"
    assert manifest["hooks"] == "./hooks.json"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert "[TODO:" not in json.dumps(manifest, ensure_ascii=False)
    assert marketplace["plugins"][0]["name"] == "keep-going"
    assert marketplace["plugins"][0]["source"]["path"] == "./plugins/keep-going"


def test_plugin_hook_assets_are_valid():
    hooks = json.loads((ROOT / "plugins" / "keep-going" / "hooks.json").read_text(encoding="utf-8"))
    claude_hooks = json.loads((ROOT / "plugins" / "keep-going" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    mcp = json.loads((ROOT / "plugins" / "keep-going" / ".mcp.json").read_text(encoding="utf-8"))
    hook_script = ROOT / "plugins" / "keep-going" / "hooks" / "keep-going-decision-hook.sh"

    assert mcp["mcpServers"]["keep-going"]["command"] == "./scripts/mcp.sh"
    assert mcp["mcpServers"]["keep-going"]["cwd"] == "."
    assert mcp["mcpServers"]["keep-going"]["startup_timeout_sec"] == 60
    assert hooks["hooks"]["keep-going-decision"]["command"] == "./hooks/keep-going-decision-hook.sh"
    assert hooks["hooks"]["keep-going-stop"]["command"] == "./hooks/keep-going-stop-hook.sh"
    assert claude_hooks["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 360
    assert hook_script.exists()
    assert "keep-going hook --input-json" in hook_script.read_text(encoding="utf-8")
    assert (ROOT / "plugins" / "keep-going" / "hooks" / "keep-going-stop-hook.sh").exists()
    assert (ROOT / "plugins" / "keep-going" / "hooks" / "hooks.json").exists()


def test_plugin_wrappers_validate_python_package_path():
    wrapper_paths = [
        ROOT / "plugins" / "keep-going" / "scripts" / "bridge.sh",
        ROOT / "plugins" / "keep-going" / "hooks" / "keep-going-decision-hook.sh",
        ROOT / "plugins" / "keep-going" / "hooks" / "keep-going-stop-hook.sh",
    ]

    for path in wrapper_paths:
        content = path.read_text(encoding="utf-8")
        assert "src/keep_going" in content
        assert "src/keep-going" not in content


def test_public_mcp_tool_names_match_server_contract():
    surfaces = [
        ROOT / ".codex" / "skills" / "keep-going" / "SKILL.md",
        ROOT / ".codex" / "agents" / "keep-going.toml",
        ROOT / "plugins" / "keep-going" / "skills" / "keep-going" / "SKILL.md",
    ]

    for path in surfaces:
        content = path.read_text(encoding="utf-8")
        assert "keep_going_reply" in content
        assert "keep-going_reply" not in content


def test_mcp_wrapper_prefers_installed_console_script():
    script = (ROOT / "scripts" / "04-mcp.sh").read_text(encoding="utf-8")

    assert '.venv/bin/keep-going' in script
    assert 'exec "$repo_root/.venv/bin/keep-going" mcp' in script
    assert "exec uv run keep-going mcp" in script


def test_codex_prompt_commands_are_valid():
    prompt_root = ROOT / "plugins" / "keep-going" / "prompts"
    root = (prompt_root / "keep-going.md").read_text(encoding="utf-8")
    native_setup = (prompt_root / "keep-going-setup.md").read_text(encoding="utf-8")
    native_status = (prompt_root / "keep-going-status.md").read_text(encoding="utf-8")
    native_self_test = (prompt_root / "keep-going-self-test.md").read_text(encoding="utf-8")
    setup = (prompt_root / "keep-going:setup.md").read_text(encoding="utf-8")
    status = (prompt_root / "keep-going:status.md").read_text(encoding="utf-8")
    self_test = (prompt_root / "keep-going:self-test.md").read_text(encoding="utf-8")

    assert "keep-going setup --enable" in root
    assert "keep-going enable" in root
    assert "keep-going disable" in root
    assert "keep-going-setup --enable" in native_setup
    assert "setup --project" in native_setup
    assert "--host codex" in native_setup
    assert "status --project" in native_status
    assert "self-test --project" in native_self_test
    assert "keep-going:setup --enable" in setup
    assert "setup --project" in setup
    assert "--host codex" in setup
    assert "status --project" in status
    assert "self-test --project" in self_test


def test_keep_going_skill_documents_project_controls():
    repo_skill = (ROOT / ".codex" / "skills" / "keep-going" / "SKILL.md").read_text(encoding="utf-8")
    plugin_skill = (ROOT / "plugins" / "keep-going" / "skills" / "keep-going" / "SKILL.md").read_text(encoding="utf-8")

    for text in (repo_skill, plugin_skill):
        assert "$keep-going 开启" in text
        assert "$keep-going 关闭" in text
        assert "$keep-going 查询" in text
        assert "$keep-going 自检" in text
        assert "--host codex" in text
        assert "不要把它当成要询问 Keep Going 的问题" in text
