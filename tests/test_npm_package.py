from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NPM_ROOT = ROOT / "packages" / "npm"
BIN = NPM_ROOT / "bin" / "keep-going.js"


def _decision_command() -> str:
    payload = {
        "action": "block",
        "reply": "继续跑验证。",
        "reason": "cli_model_blocked",
        "confidence": 0.9,
        "category": "verification",
        "evidence": [{"source": "test-cli", "id": "npm-start"}],
    }
    script = f"import json,sys; sys.stdin.read(); print({json.dumps(json.dumps(payload, ensure_ascii=False))})"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _keep_going_codex_stop_hook(codex_home: Path) -> dict[str, object]:
    hooks_json = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    for entry in hooks_json["hooks"]["Stop"]:
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict):
                command = str(hook.get("command") or "")
                if "KEEP_GOING_HOST=codex" in command and "keep-going-stop-hook.sh" in command:
                    return hook
    raise AssertionError("missing Keep Going Codex Stop hook")


def test_npm_package_metadata_is_publishable():
    package = json.loads((NPM_ROOT / "package.json").read_text(encoding="utf-8"))

    assert package["name"] == "keep-going"
    assert package["bin"] == {"keep-going": "bin/keep-going.js"}
    assert package["publishConfig"]["access"] == "public"
    assert "runtime/" in package["files"]
    assert "prepack" in package["scripts"]


def test_npm_wrapper_help_and_syntax_check():
    help_result = subprocess.run(
        ["node", str(BIN), "--help"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "npx keep-going install" in help_result.stdout
    assert "npx keep-going onboard" in help_result.stdout
    assert "npx keep-going start" in help_result.stdout
    assert "npx keep-going sync-local" in help_result.stdout
    assert "--register-hosts <mode>" in help_result.stdout

    subprocess.run(["npm", "--prefix", str(NPM_ROOT), "test"], cwd=ROOT, check=True)


def test_prepare_runtime_script_copies_publish_runtime(tmp_path: Path):
    out = tmp_path / "runtime"

    result = subprocess.run(
        ["node", str(NPM_ROOT / "scripts" / "prepare-runtime.js"), "--out", str(out)],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "prepared Keep Going runtime" in result.stdout
    assert (out / "pyproject.toml").exists()
    assert (out / "src" / "keep_going" / "cli.py").exists()
    assert (out / "src" / "keep_going" / "onboarding.py").exists()
    assert (out / "plugins" / "keep-going" / ".codex-plugin" / "plugin.json").exists()
    assert (out / "plugins" / "keep-going" / "plugin.json").exists()
    assert (out / "plugins" / "keep-going" / "scripts" / "onboard.sh").exists()
    assert not (out / "plugins" / "keep-going" / "runtime-root").exists()
    assert not (out / "plugins" / "keep-going" / ".repo-root").exists()
    assert (out / "artifacts" / "decision-policy.template.yaml").read_bytes() == (
        ROOT / "artifacts" / "decision-policy.template.yaml"
    ).read_bytes()
    assert (out / "docs" / "assets" / "keep-going-concept.svg").read_bytes() == (
        ROOT / "docs" / "assets" / "keep-going-concept.svg"
    ).read_bytes()
    assert not (out / "artifacts" / "decision-policy.yaml").exists()
    assert not (out / "artifacts" / "decision-policy.runtime.yaml").exists()
    assert not any((out / "artifacts").glob("decision-policy.candidate*.yaml"))
    assert not (out / ".venv").exists()
    assert not (out / ".omx").exists()
    assert not (out / "claude.log").exists()
    assert not (out / "AGENTS.md").exists()
    assert not (out / "CLAUDE.md").exists()
    assert "--synthetic" in (out / "README.md").read_text(encoding="utf-8")
    assert "确定性安全边界" in (out / "README.zh.md").read_text(encoding="utf-8")
    assert not (out / ".gitignore").exists()
    assert not (out / ".playwright-mcp").exists()
    assert not (out / "keep_going.debug").exists()
    assert not (out / "save.txt").exists()
    assert not (out / ".claude" / "settings.local.json").exists()
    assert not (out / ".serena").exists()
    assert not (out / "scripts" / "privacy-audit.py").exists()
    assert not any(out.rglob("__pycache__"))
    assert not any(out.rglob("*.pyc"))
    assert not any(out.rglob("*.pyo"))
    assert not any(out.glob("slide-*.png"))
    assert not (out / "tests").exists()
    assert not (out / "packages").exists()


def test_npm_runtime_rejects_unsafe_targets(tmp_path: Path):
    invalid_version = subprocess.run(
        [
            "node",
            str(BIN),
            "runtime-path",
            "--runtime-home",
            str(tmp_path / "runtime-home"),
            "--runtime-version",
            "..",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    same_source = subprocess.run(
        [
            "node",
            "-e",
            (
                "const {copyRuntimeSource}=require('./packages/npm/lib/runtime');"
                "try { copyRuntimeSource(process.cwd(), process.cwd(), {replace:true}); process.exit(2); }"
                "catch (error) { console.error(error.message); }"
            ),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert invalid_version.returncode != 0
    assert "invalid runtime version directory" in invalid_version.stderr
    assert same_source.returncode == 0
    assert "runtime target must not be the source directory" in same_source.stderr


def test_npm_runtime_allowlist_rejects_private_files_and_symlinks(tmp_path: Path):
    source = tmp_path / "source"
    candidates = [
        "src/keep_going/private_profile.py",
        "plugins/keep-going/private-profile.md",
        "plugins/keep-going/private.png",
        "plugins/keep-going/session.jsonl",
        "plugins/keep-going/.env",
        "src/keep_going/private.log",
        ".codex/skills/keep-going/session.jsonl",
        ".agents/plugins/private.zip",
    ]
    for relative in candidates:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private", encoding="utf-8")
    symlink = source / "plugins" / "keep-going" / "linked.md"
    symlink.symlink_to(source / "plugins" / "keep-going" / "session.jsonl")
    script = (
        "const {shouldCopy}=require(process.argv[1]);"
        "const root=process.argv[2];"
        "console.log(JSON.stringify(process.argv.slice(3).map(p=>shouldCopy(root,p))));"
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            script,
            str(NPM_ROOT / "lib" / "runtime.js"),
            str(source),
            *(str(source / relative) for relative in candidates),
            str(symlink),
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    assert json.loads(result.stdout) == [False] * (len(candidates) + 1)


def test_prepare_runtime_rejects_private_policy_option() -> None:
    result = subprocess.run(
        ["node", str(NPM_ROOT / "scripts" / "prepare-runtime.js"), "--include-private-policy"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "unknown option: --include-private-policy" in result.stderr


def test_runtime_copier_cannot_include_private_policy(tmp_path: Path) -> None:
    out = tmp_path / "runtime"
    script = (
        "const {copyRuntimeSource}=require(process.argv[1]);"
        "copyRuntimeSource(process.argv[2],process.argv[3],"
        "{replace:true,includePrivatePolicy:true,writeMarkers:true});"
    )

    subprocess.run(
        [
            "node",
            "-e",
            script,
            str(NPM_ROOT / "lib" / "runtime.js"),
            str(ROOT),
            str(out),
        ],
        cwd=ROOT,
        check=True,
    )

    assert not (out / "artifacts" / "decision-policy.yaml").exists()
    assert not (out / "artifacts" / "decision-policy.runtime.yaml").exists()
    assert not (out / "plugins" / "keep-going" / "runtime-root").exists()
    assert not (out / "plugins" / "keep-going" / ".repo-root").exists()


def test_public_runtime_rejects_sensitive_content_inside_allowlisted_file(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    source = runtime / "src" / "keep_going" / "private.py"
    source.parent.mkdir(parents=True)
    source.write_text("home = '/" + "Users/alice/work'\n", encoding="utf-8")
    script = (
        "const {assertPublicRuntimeTree}=require(process.argv[1]);"
        "try { assertPublicRuntimeTree(process.argv[2]); process.exit(2); }"
        "catch (error) { console.error(error.message); }"
    )
    result = subprocess.run(
        ["node", "-e", script, str(NPM_ROOT / "lib" / "runtime.js"), str(runtime)],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "absolute user-home path" in result.stderr


def test_npm_wrapper_dry_run_from_local_source(tmp_path: Path):
    result = subprocess.run(
        [
            "node",
            str(BIN),
            "install",
            "--dry-run",
            "--source",
            str(ROOT),
            "--runtime-home",
            str(tmp_path / "runtime-home"),
            "--codex-home",
            str(tmp_path / "codex-home"),
            "--agents-home",
            str(tmp_path / "agents-home"),
            "--claude-home",
            str(tmp_path / "claude-home"),
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, "KEEP_GOING_CODEX_CLI_COMMAND": _decision_command()},
    )

    assert "Keep Going runtime:" in result.stdout
    assert "dry-run only" in result.stdout
    assert (tmp_path / "runtime-home" / "0.1.0" / "pyproject.toml").exists()


def test_npm_wrapper_install_and_upgrade_to_temp_homes(tmp_path: Path):
    common = [
        "--source",
        str(ROOT),
        "--runtime-home",
        str(tmp_path / "runtime-home"),
        "--codex-home",
        str(tmp_path / "codex-home"),
        "--agents-home",
        str(tmp_path / "agents-home"),
        "--claude-home",
        str(tmp_path / "claude-home"),
    ]

    install = subprocess.run(
        ["node", str(BIN), "install", *common],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, "KEEP_GOING_CODEX_CLI_COMMAND": _decision_command()},
    )
    upgrade = subprocess.run(
        ["node", str(BIN), "upgrade", *common],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Keep Going install verified." in install.stdout
    assert "Keep Going upgrade verified." in upgrade.stdout
    assert "- force: 0" in install.stdout
    assert "- force: 1" in upgrade.stdout
    assert "host plugin registration" in install.stdout
    assert "custom target homes were provided" in install.stdout
    assert not (tmp_path / "codex-home" / "skills" / "keep-going").exists()
    assert not (tmp_path / "codex-home" / "prompts" / "keep-going.md").exists()
    assert not (tmp_path / "codex-home" / "commands" / "keep-going.md").exists()
    assert "codex slash commands unsupported" in install.stdout
    runtime_root = (tmp_path / "runtime-home" / "0.1.0" / "plugins" / "keep-going" / "runtime-root").read_text(
        encoding="utf-8"
    ).strip()
    assert runtime_root == str(tmp_path / "runtime-home" / "0.1.0")
    repo_root = (tmp_path / "agents-home" / "plugins" / "keep-going" / ".repo-root").read_text(encoding="utf-8").strip()
    assert repo_root == str(tmp_path / "runtime-home" / "0.1.0")


def test_npm_wrapper_start_to_temp_homes(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    state_home = tmp_path / "state-home"

    result = subprocess.run(
        [
            "node",
            str(BIN),
            "start",
            "--source",
            str(ROOT),
            "--runtime-home",
            str(tmp_path / "runtime-home"),
            "--project",
            str(project),
            "--codex-home",
            str(tmp_path / "codex-home"),
            "--agents-home",
            str(tmp_path / "agents-home"),
            "--claude-home",
            str(tmp_path / "claude-home"),
            "--state-home",
            str(state_home),
            "--no-register-hosts",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, "KEEP_GOING_CODEX_CLI_COMMAND": _decision_command()},
    )

    assert "Keep Going runtime:" in result.stdout
    assert "codex native Stop hook: installed" in result.stdout
    assert "codex_native_stop_hook: PASS" in result.stdout
    assert "enabled: True" in result.stdout
    assert "host: codex" in result.stdout
    assert "Keep Going start verified." in result.stdout
    hooks_json = json.loads((tmp_path / "codex-home" / "hooks.json").read_text(encoding="utf-8"))
    commands = [
        str(hook.get("command") or "")
        for entry in hooks_json["hooks"]["Stop"]
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict)
    ]
    assert any("KEEP_GOING_HOST=codex" in command and "keep-going-stop-hook.sh" in command for command in commands)
    assert _keep_going_codex_stop_hook(tmp_path / "codex-home")["timeout"] == 360
    state_files = list(state_home.glob("*/state.json"))
    assert len(state_files) == 1
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert state["enabled"] is True
    assert state["host"] == "codex"


def test_npm_start_and_onboard_preserve_existing_personal_policy(tmp_path: Path):
    package_root = tmp_path / "package"
    shutil.copytree(NPM_ROOT, package_root, ignore=shutil.ignore_patterns("runtime"))
    bundled = package_root / "runtime"
    subprocess.run(
        ["node", str(NPM_ROOT / "scripts" / "prepare-runtime.js"), "--out", str(bundled)],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    runtime_home = tmp_path / "runtime-home"
    runtime = runtime_home / "0.1.0"
    script = (
        "const {copyRuntimeSource}=require(process.argv[1]);"
        "copyRuntimeSource(process.argv[2],process.argv[3],{replace:true});"
    )
    subprocess.run(
        ["node", "-e", script, str(package_root / "lib" / "runtime.js"), str(bundled), str(runtime)],
        cwd=ROOT,
        check=True,
    )
    source_policy = ROOT / "artifacts" / "decision-policy.yaml"
    runtime_policy = ROOT / "artifacts" / "decision-policy.runtime.yaml"
    shutil.copy2(source_policy, runtime / "artifacts" / source_policy.name)
    shutil.copy2(runtime_policy, runtime / "artifacts" / runtime_policy.name)
    before = source_policy.read_bytes()
    project = tmp_path / "project"
    project.mkdir()
    common = [
        "--runtime-home",
        str(runtime_home),
        "--project",
        str(project),
        "--codex-home",
        str(tmp_path / "codex-home"),
        "--agents-home",
        str(tmp_path / "agents-home"),
        "--claude-home",
        str(tmp_path / "claude-home"),
        "--state-home",
        str(tmp_path / "state-home"),
        "--no-register-hosts",
    ]

    subprocess.run(
        ["node", str(package_root / "bin" / "keep-going.js"), "start", *common],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, "KEEP_GOING_CODEX_CLI_COMMAND": _decision_command()},
    )
    stable_artifacts = runtime_home / "user" / "artifacts"
    assert (stable_artifacts / "decision-policy.yaml").read_bytes() == before
    assert (stable_artifacts / "decision-policy.runtime.yaml").read_bytes() == runtime_policy.read_bytes()
    subprocess.run(
        [
            "node",
            str(package_root / "bin" / "keep-going.js"),
            "upgrade",
            "--runtime-home",
            str(runtime_home),
            "--codex-home",
            str(tmp_path / "codex-home"),
            "--agents-home",
            str(tmp_path / "agents-home"),
            "--claude-home",
            str(tmp_path / "claude-home"),
            "--no-register-hosts",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    onboard = subprocess.run(
        [
            "node",
            str(package_root / "bin" / "keep-going.js"),
            "onboard",
            *common,
            "--no-deploy",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "KEEP_GOING_DISTILL_COMMAND": "command-that-must-not-run"},
    )

    assert (stable_artifacts / "decision-policy.yaml").read_bytes() == before
    assert (stable_artifacts / "decision-policy.runtime.yaml").read_bytes() == runtime_policy.read_bytes()
    assert onboard.returncode != 0
    assert "personal DNA already exists" in onboard.stderr


def test_npm_wrapper_sync_local_to_temp_homes(tmp_path: Path):
    result = subprocess.run(
        [
            "node",
            str(BIN),
            "sync-local",
            "--source",
            str(ROOT),
            "--runtime-home",
            str(tmp_path / "runtime-home"),
            "--codex-home",
            str(tmp_path / "codex-home"),
            "--agents-home",
            str(tmp_path / "agents-home"),
            "--claude-home",
            str(tmp_path / "claude-home"),
            "--no-register-hosts",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Keep Going runtime:" in result.stdout
    assert "Keep Going local runtime sync" in result.stdout
    assert "runtime_copy: prepared Keep Going runtime" in result.stdout
    assert "codex native Stop hook: installed" in result.stdout
    assert "codex_native_stop_hook: PASS" in result.stdout
    assert "Keep Going local sync verified." in result.stdout
    runtime = tmp_path / "runtime-home" / "0.1.0"
    assert (runtime / "artifacts" / "decision-policy.yaml").read_bytes() == (
        ROOT / "artifacts" / "decision-policy.yaml"
    ).read_bytes()
    assert (runtime / "artifacts" / "decision-policy.runtime.yaml").read_bytes() == (
        ROOT / "artifacts" / "decision-policy.runtime.yaml"
    ).read_bytes()
    hooks_json = json.loads((tmp_path / "codex-home" / "hooks.json").read_text(encoding="utf-8"))
    commands = [
        str(hook.get("command") or "")
        for entry in hooks_json["hooks"]["Stop"]
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict)
    ]
    assert any("KEEP_GOING_HOST=codex" in command and "keep-going-stop-hook.sh" in command for command in commands)
    assert _keep_going_codex_stop_hook(tmp_path / "codex-home")["timeout"] == 360
