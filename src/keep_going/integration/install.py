"""CLI wrapper for repo-local integration installer."""

from __future__ import annotations

import json
import hashlib
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from keep_going.config import Config
from keep_going.decision.policy_runtime import load_runtime_policy, runtime_policy_path

HOST_PLUGIN_CHOICES = ("auto", "all", "claude-code", "codex", "none")
DEFAULT_RUNTIME_VERSION = "0.1.0"
STOP_HOOK_TIMEOUT_SECONDS = 360


def resolve_target_homes(
    *,
    codex_home: Path | None = None,
    agents_home: Path | None = None,
    claude_home: Path | None = None,
) -> tuple[Path, Path, Path]:
    codex_target = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    if agents_home is not None:
        agents_target = agents_home
    elif codex_home is not None:
        agents_target = codex_target / ".agents"
    else:
        agents_target = Path(os.environ.get("AGENTS_HOME", Path.home() / ".agents"))
    claude_target = claude_home or Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))
    return codex_target, agents_target, claude_target


def run_installer(
    cfg: Config,
    *,
    codex_home: Path | None = None,
    agents_home: Path | None = None,
    claude_home: Path | None = None,
    execute: bool = False,
    force: bool = False,
    register_hosts: str = "auto",
) -> str:
    if register_hosts not in HOST_PLUGIN_CHOICES:
        raise ValueError(f"--register-hosts must be one of: {', '.join(HOST_PLUGIN_CHOICES)}")
    script = cfg.root / "scripts" / "install-integration.sh"
    if not script.exists():
        raise FileNotFoundError(f"missing installer: {script}")
    args = [str(script), "--execute" if execute else "--dry-run"]
    if force:
        args.append("--force")
    if codex_home is not None:
        args.extend(["--codex-home", str(codex_home)])
    if agents_home is not None:
        args.extend(["--agents-home", str(agents_home)])
    if claude_home is not None:
        args.extend(["--claude-home", str(claude_home)])
    result = subprocess.run(args, cwd=cfg.root, text=True, capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"installer failed with exit code {result.returncode}")
    output = result.stdout
    if execute:
        _, agents_target, _ = resolve_target_homes(
            codex_home=codex_home,
            agents_home=agents_home,
            claude_home=claude_home,
        )
        output += install_codex_native_stop_hook(
            codex_home=codex_home,
            agents_home=agents_target,
        )
        custom_homes = any(path is not None for path in (codex_home, agents_home, claude_home))
        output += register_host_plugins(cfg.root, register_hosts=register_hosts, custom_homes=custom_homes, force=force)
    return output


def sync_local_install(
    cfg: Config,
    *,
    codex_home: Path | None = None,
    agents_home: Path | None = None,
    claude_home: Path | None = None,
    runtime_home: Path | None = None,
    runtime_version: str | None = None,
    register_hosts: str = "auto",
    verify: bool = True,
) -> tuple[str, dict[str, Any] | None]:
    """Refresh the local runtime copy and installed host integration from this checkout."""
    if register_hosts not in HOST_PLUGIN_CHOICES:
        raise ValueError(f"--register-hosts must be one of: {', '.join(HOST_PLUGIN_CHOICES)}")
    output = sync_local_runtime(cfg, runtime_home=runtime_home, runtime_version=runtime_version)
    output += run_installer(
        cfg,
        codex_home=codex_home,
        agents_home=agents_home,
        claude_home=claude_home,
        execute=True,
        force=True,
        register_hosts=register_hosts,
    )
    report = None
    if verify:
        report = verify_installation(codex_home=codex_home, agents_home=agents_home, claude_home=claude_home)
    return output, report


def sync_local_runtime(
    cfg: Config,
    *,
    runtime_home: Path | None = None,
    runtime_version: str | None = None,
) -> str:
    version = runtime_version or _runtime_version(cfg.root)
    target = (runtime_home or Path(os.environ.get("KEEP_GOING_RUNTIME_HOME", Path.home() / ".keep-going" / "runtime"))) / version
    npm_package_root = (cfg.root / "packages" / "npm").resolve()
    resolved_target = target.resolve()
    if resolved_target == npm_package_root or resolved_target.is_relative_to(npm_package_root):
        raise ValueError(f"runtime target must be outside npm package staging: {resolved_target}")
    source_policy = cfg.paths.artifacts_dir / "decision-policy.yaml"
    runtime_policy = runtime_policy_path(source_policy)
    load_runtime_policy(source_policy)
    source_sha = _sha256(source_policy)
    runtime_sha = _sha256(runtime_policy)
    script = cfg.root / "packages" / "npm" / "scripts" / "prepare-runtime.js"
    lines = [
        "Keep Going local runtime sync",
        f"- runtime_target: {target}",
        f"- source_sha256: {source_sha}",
        f"- runtime_sha256: {runtime_sha}",
    ]
    if not script.exists():
        if _same_path(str(target), cfg.root):
            lines.append("- runtime_copy: SKIP current root is already the active runtime")
            lines.append(f"- installed_runtime_sha256: {runtime_sha}")
            return "\n".join(lines) + "\n"
        lines.append("- runtime_copy: SKIP package prepare script is not present in this runtime")
        return "\n".join(lines) + "\n"
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is required to refresh the local Keep Going runtime copy")
    result = subprocess.run(
        [node, str(script), "--out", str(target)],
        cwd=cfg.root,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"runtime sync failed with exit code {result.returncode}")
    target_artifacts = target / "artifacts"
    target_artifacts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_policy, target_artifacts / source_policy.name)
    shutil.copy2(runtime_policy, target_artifacts / runtime_policy.name)
    plugin_root = target / "plugins" / "keep-going"
    if plugin_root.is_dir():
        marker = f"{target.resolve()}\n"
        (plugin_root / "runtime-root").write_text(marker, encoding="utf-8")
        (plugin_root / ".repo-root").write_text(marker, encoding="utf-8")
    installed_runtime = target / "artifacts" / runtime_policy.name
    if not installed_runtime.is_file():
        raise RuntimeError(f"synced runtime decision policy missing: {installed_runtime}")
    installed_sha = _sha256(installed_runtime)
    if installed_sha != runtime_sha:
        raise RuntimeError(
            f"synced runtime decision policy hash mismatch: checkout={runtime_sha} installed={installed_sha} path={installed_runtime}"
        )
    lines.append(f"- runtime_copy: {result.stdout.strip()}")
    lines.append(f"- installed_runtime_sha256: {installed_sha}")
    return "\n".join(lines) + "\n"


def _runtime_version(root: Path) -> str:
    package_json = root / "packages" / "npm" / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {package_json}: {exc}") from exc
        version = data.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    return DEFAULT_RUNTIME_VERSION


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def register_host_plugins(
    runtime_root: Path,
    *,
    register_hosts: str = "auto",
    custom_homes: bool = False,
    force: bool = False,
) -> str:
    """Register the installed plugin through host CLIs instead of editing private registries."""
    if register_hosts not in HOST_PLUGIN_CHOICES:
        raise ValueError(f"--register-hosts must be one of: {', '.join(HOST_PLUGIN_CHOICES)}")
    lines = ["", "Keep Going host plugin registration"]
    if register_hosts == "none":
        lines.append("- host_plugins: SKIP disabled by --register-hosts none")
        return "\n".join(lines) + "\n"
    if custom_homes:
        lines.append("- host_plugins: SKIP custom target homes were provided; host CLIs register against their default homes")
        return "\n".join(lines) + "\n"

    detected = {
        "claude-code": shutil.which("claude"),
        "codex": shutil.which("codex"),
    }
    selected = _select_hosts(register_hosts, detected)
    if not selected:
        lines.append("- host_plugins: SKIP no supported host CLI found on PATH")
        return "\n".join(lines) + "\n"

    for host in selected:
        executable = detected.get(host)
        if not executable:
            raise RuntimeError(f"{host} plugin registration requested but host CLI was not found on PATH")
        if host == "claude-code":
            lines.extend(_register_claude_code_plugin(runtime_root, executable, force=force))
        elif host == "codex":
            lines.extend(_register_codex_plugin(runtime_root, executable, force=force))
        else:
            raise RuntimeError(f"unsupported host plugin target: {host}")
    return "\n".join(lines) + "\n"


def _select_hosts(register_hosts: str, detected: dict[str, str | None]) -> list[str]:
    if register_hosts == "auto":
        return [host for host in ("claude-code", "codex") if detected.get(host)]
    if register_hosts == "all":
        return ["claude-code", "codex"]
    return [register_hosts]


def _register_claude_code_plugin(runtime_root: Path, executable: str, *, force: bool = False) -> list[str]:
    lines = []
    marketplace = _run_host_command([executable, "plugin", "marketplace", "list"])
    if "keep-going-local" in marketplace.stdout:
        lines.append("- claude-code marketplace: present")
    else:
        _run_host_command([executable, "plugin", "marketplace", "add", str(runtime_root)])
        lines.append("- claude-code marketplace: registered keep-going-local")

    plugins = _run_host_command([executable, "plugin", "list", "--json"])
    try:
        installed = json.loads(plugins.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("claude plugin list --json did not return valid JSON") from exc

    plugin = next((item for item in installed if item.get("id") == "keep-going@keep-going-local"), None)
    if plugin is None:
        _run_host_command([executable, "plugin", "install", "keep-going@keep-going-local"])
        lines.append("- claude-code plugin: installed keep-going@keep-going-local")
    elif force or _claude_plugin_cache_needs_refresh(plugin, runtime_root):
        _run_host_command([executable, "plugin", "uninstall", "--keep-data", "-y", "keep-going@keep-going-local"])
        _run_host_command([executable, "plugin", "install", "keep-going@keep-going-local"])
        lines.append("- claude-code plugin: refreshed keep-going@keep-going-local")
    elif plugin.get("enabled") is False:
        _run_host_command([executable, "plugin", "enable", "keep-going@keep-going-local"])
        lines.append("- claude-code plugin: enabled keep-going@keep-going-local")
    else:
        lines.append("- claude-code plugin: present keep-going@keep-going-local")
    return lines


def _claude_plugin_cache_needs_refresh(plugin: dict[str, Any], runtime_root: Path) -> bool:
    install_path = plugin.get("installPath")
    if not isinstance(install_path, str) or not install_path:
        return False
    marker = Path(install_path) / "runtime-root"
    if not marker.exists():
        return True
    try:
        return not _same_path(marker.read_text(encoding="utf-8").strip(), runtime_root)
    except OSError:
        return True


def _register_codex_plugin(runtime_root: Path, executable: str, *, force: bool = False) -> list[str]:
    lines = []
    if _remove_legacy_codex_marketplace_shadow():
        lines.append("- codex legacy marketplace: removed ~/.agents/plugins/marketplace.json shadow")

    marketplaces = _run_host_command([executable, "plugin", "marketplace", "list"]).stdout
    configured_path = _codex_marketplace_path(marketplaces, "keep-going-local")
    if configured_path is None:
        _run_host_command([executable, "plugin", "marketplace", "add", str(runtime_root)])
        lines.append("- codex marketplace: registered keep-going-local")
    elif _same_path(configured_path, runtime_root):
        lines.append("- codex marketplace: present keep-going-local")
    else:
        _run_host_command([executable, "plugin", "marketplace", "remove", "keep-going-local"])
        _run_host_command([executable, "plugin", "marketplace", "add", str(runtime_root)])
        lines.append(f"- codex marketplace: updated keep-going-local from {configured_path}")

    listing = _run_host_command([executable, "plugin", "list", "--marketplace", "keep-going-local"]).stdout
    if "keep-going@keep-going-local (installed" in listing:
        if force or _codex_plugin_cache_needs_refresh(runtime_root):
            _run_host_command([executable, "plugin", "remove", "keep-going@keep-going-local"])
            _run_host_command([executable, "plugin", "add", "keep-going@keep-going-local"])
            lines.append("- codex plugin: refreshed keep-going@keep-going-local")
            return lines
        lines.append("- codex plugin: present keep-going@keep-going-local")
        return lines
    if "keep-going@keep-going-local" not in listing:
        lines.append("- codex plugin: not visible before install; attempting plugin add")
    _run_host_command([executable, "plugin", "add", "keep-going@keep-going-local"])
    lines.append("- codex plugin: installed keep-going@keep-going-local")
    return lines


def _codex_plugin_cache_needs_refresh(runtime_root: Path) -> bool:
    metadata = _load_codex_plugin_metadata(runtime_root)
    if metadata is None:
        return False
    name, version = metadata
    marker = (
        Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        / "plugins"
        / "cache"
        / "keep-going-local"
        / name
        / version
        / "runtime-root"
    )
    if not marker.exists():
        return True
    try:
        return not _same_path(marker.read_text(encoding="utf-8").strip(), runtime_root)
    except OSError:
        return True


def _load_codex_plugin_metadata(runtime_root: Path) -> tuple[str, str] | None:
    manifest = runtime_root / "plugins" / "keep-going" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name = data.get("name")
    version = data.get("version")
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        return None
    return name, version


def _codex_marketplace_path(output: str, name: str) -> str | None:
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("MARKETPLACE") or stripped.startswith("Configured marketplaces"):
            continue
        parts = stripped.split(None, 1)
        if len(parts) == 2 and parts[0] == name:
            return parts[1].strip()
    return None


def _same_path(left: str, right: Path) -> bool:
    try:
        return Path(left).expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return str(Path(left).expanduser()) == str(right.expanduser())


def _remove_legacy_codex_marketplace_shadow() -> bool:
    """Remove the legacy ambient marketplace file written by older Keep Going installers.

    Current Codex prefers the ambient ~/.agents/plugins/marketplace.json over a
    configured marketplace with the same name. If the old file is left in place,
    `codex plugin add keep-going@keep-going-local` can resolve the stale file and fail
    with "missing plugin.json".
    """
    path = Path(os.environ.get("AGENTS_HOME", Path.home() / ".agents")) / "plugins" / "marketplace.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if data.get("name") != "keep-going-local":
        return False
    path.unlink()
    return True


def _run_host_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        command = " ".join(args)
        raise RuntimeError(detail or f"host plugin command failed: {command}")
    return result


def install_codex_native_stop_hook(
    *,
    codex_home: Path | None = None,
    agents_home: Path | None = None,
) -> str:
    codex_target, agents_target, _ = resolve_target_homes(codex_home=codex_home, agents_home=agents_home)
    plugin_root = agents_target / "plugins" / "keep-going"
    hook_path = plugin_root / "hooks" / "keep-going-stop-hook.sh"
    if not hook_path.exists():
        raise FileNotFoundError(f"missing Keep Going Stop hook wrapper: {hook_path}")
    hooks_path = codex_target / "hooks.json"
    before = _load_json_object(hooks_path)
    after, action = _merge_codex_native_stop_hook(before, hook_path)
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(after, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return "\n".join(["", "Keep Going Codex native hook", f"- codex native Stop hook: {action} {hooks_path}"]) + "\n"


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def _merge_codex_native_stop_hook(data: dict[str, Any], hook_path: Path) -> tuple[dict[str, Any], str]:
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json field 'hooks' must be an object")
    stop_hooks = hooks.setdefault("Stop", [])
    if not isinstance(stop_hooks, list):
        raise ValueError("hooks.json field 'hooks.Stop' must be a list")

    hook_command = f"KEEP_GOING_HOST=codex {shlex.quote(str(hook_path))}"
    keep_going_hook = {"type": "command", "command": hook_command, "timeout": STOP_HOOK_TIMEOUT_SECONDS}
    for entry in stop_hooks:
        if not isinstance(entry, dict):
            continue
        commands = entry.get("hooks")
        if not isinstance(commands, list):
            continue
        for command in commands:
            if not isinstance(command, dict):
                continue
            raw = str(command.get("command") or "")
            if "keep-going-stop-hook.sh" in raw:
                changed = command.get("command") != hook_command or command.get("timeout") != STOP_HOOK_TIMEOUT_SECONDS
                command.update(keep_going_hook)
                return data, "updated" if changed else "present"

    stop_hooks.append({"matcher": "*", "hooks": [keep_going_hook]})
    return data, "installed"


def verify_installation(
    *,
    codex_home: Path | None = None,
    agents_home: Path | None = None,
    claude_home: Path | None = None,
) -> dict[str, Any]:
    codex_target, agents_target, claude_target = resolve_target_homes(
        codex_home=codex_home,
        agents_home=agents_home,
        claude_home=claude_home,
    )
    plugin_root = agents_target / "plugins" / "keep-going"
    repo_root_file = plugin_root / ".repo-root"
    claude_marketplace_root = claude_target / "plugins" / "marketplaces" / "keep-going-local"
    claude_plugin_root = claude_marketplace_root / "plugins" / "keep-going"
    checks = [
        ("agent", codex_target / "agents" / "keep-going.toml"),
        ("plugin_manifest", plugin_root / ".codex-plugin" / "plugin.json"),
        ("plugin_root_manifest", plugin_root / "plugin.json"),
        ("plugin_claude_manifest", plugin_root / ".claude-plugin" / "plugin.json"),
        ("plugin_skill", plugin_root / "skills" / "keep-going" / "SKILL.md"),
        ("plugin_reply_wrapper", plugin_root / "scripts" / "reply.sh"),
        ("plugin_mcp_wrapper", plugin_root / "scripts" / "mcp.sh"),
        ("plugin_bridge_wrapper", plugin_root / "scripts" / "bridge.sh"),
        ("plugin_hook", plugin_root / "hooks" / "keep-going-decision-hook.sh"),
        ("plugin_stop_hook", plugin_root / "hooks" / "keep-going-stop-hook.sh"),
        ("plugin_claude_hooks", plugin_root / "hooks" / "hooks.json"),
        ("plugin_mcp", plugin_root / ".mcp.json"),
        ("plugin_runtime_root", plugin_root / "runtime-root"),
        ("plugin_repo_root", repo_root_file),
        ("claude_marketplace", claude_marketplace_root / ".claude-plugin" / "marketplace.json"),
        ("claude_plugin_manifest", claude_plugin_root / ".claude-plugin" / "plugin.json"),
        ("claude_plugin_stop_hook", claude_plugin_root / "hooks" / "keep-going-stop-hook.sh"),
        ("claude_plugin_bridge_wrapper", claude_plugin_root / "scripts" / "bridge.sh"),
        ("claude_plugin_runtime_root", claude_plugin_root / "runtime-root"),
    ]
    rendered = [
        _verify_absent_keep_going_skill(codex_target / "skills" / "keep-going"),
        _verify_absent_keep_going_files(
            "codex_legacy_prompts_removed",
            codex_target / "prompts",
            _legacy_codex_prompt_names(),
        ),
        _verify_absent_keep_going_files(
            "codex_legacy_commands_removed",
            codex_target / "commands",
            _legacy_codex_prompt_names(),
        ),
    ]
    rendered.extend(_verify_path(name, path) for name, path in checks)
    rendered.append(_verify_codex_native_stop_hook(codex_target / "hooks.json"))
    rendered.append(_verify_repo_root(repo_root_file))
    return {
        "ok": all(check["status"] == "PASS" for check in rendered),
        "codex_home": str(codex_target),
        "agents_home": str(agents_target),
        "claude_home": str(claude_target),
        "checks": rendered,
    }


def _verify_path(name: str, path: Path) -> dict[str, str]:
    if not path.exists():
        return {"name": name, "path": str(path), "status": "MISSING"}
    if name in {
        "plugin_reply_wrapper",
        "plugin_mcp_wrapper",
        "plugin_bridge_wrapper",
        "plugin_hook",
        "plugin_stop_hook",
        "claude_plugin_stop_hook",
        "claude_plugin_bridge_wrapper",
    } and not os.access(path, os.X_OK):
        return {"name": name, "path": str(path), "status": "NOT_EXECUTABLE"}
    return {"name": name, "path": str(path), "status": "PASS"}


def _verify_absent_keep_going_skill(path: Path) -> dict[str, str]:
    name = "codex_legacy_skill_removed"
    skill_file = path / "SKILL.md"
    if not path.exists():
        return {"name": name, "path": str(path), "status": "PASS"}
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError:
        return {"name": name, "path": str(path), "status": "PRESENT"}
    markers = ("Keep Going", "keep-going", "scripts/03-reply.sh", "keep-going reply")
    if any(marker in text for marker in markers):
        return {"name": name, "path": str(path), "status": "PRESENT"}
    return {"name": name, "path": str(path), "status": "PASS"}


def _legacy_codex_prompt_names() -> tuple[str, ...]:
    return (
        "keep-going.md",
        "keep-going-setup.md",
        "keep-going-status.md",
        "keep-going-self-test.md",
        "keep-going:setup.md",
        "keep-going:status.md",
        "keep-going:self-test.md",
    )


def _verify_absent_keep_going_files(name: str, root: Path, filenames: tuple[str, ...]) -> dict[str, str]:
    present: list[str] = []
    for filename in filenames:
        path = root / filename
        if _is_keep_going_owned_file(path):
            present.append(filename)
    if present:
        return {"name": name, "path": f"{root}: {', '.join(present)}", "status": "PRESENT"}
    return {"name": name, "path": str(root), "status": "PASS"}


def _verify_codex_native_stop_hook(path: Path) -> dict[str, str]:
    name = "codex_native_stop_hook"
    if not path.exists():
        return {"name": name, "path": str(path), "status": "MISSING"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"name": name, "path": str(path), "status": "INVALID"}
    stop_hooks = ((data.get("hooks") or {}).get("Stop") or []) if isinstance(data, dict) else []
    for entry in stop_hooks:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if not isinstance(hook, dict):
                continue
            command = str(hook.get("command") or "")
            if "KEEP_GOING_HOST=codex" in command and "keep-going-stop-hook.sh" in command:
                return {"name": name, "path": str(path), "status": "PASS"}
    return {"name": name, "path": str(path), "status": "MISSING"}


def _is_keep_going_owned_file(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    markers = ("Keep Going", "keep-going", "KEEP_GOING_PLUGIN_ROOT", "scripts/bridge.sh", "keep-going bridge")
    return any(marker in text for marker in markers)


def _verify_repo_root(path: Path) -> dict[str, str]:
    name = "plugin_repo_runtime"
    if not path.exists():
        return {"name": name, "path": str(path), "status": "MISSING"}
    repo_root = Path(path.read_text(encoding="utf-8").strip())
    required = [
        repo_root / ".agents" / "plugins" / "marketplace.json",
        repo_root / "scripts" / "03-reply.sh",
        repo_root / "scripts" / "04-mcp.sh",
        repo_root / "plugins" / "keep-going" / ".codex-plugin" / "plugin.json",
        repo_root / "src" / "keep_going",
        repo_root / "src" / "keep_going" / "integration" / "bridge.py",
    ]
    if not all(item.exists() for item in required):
        return {"name": name, "path": str(path), "status": "INVALID"}
    return {"name": name, "path": str(repo_root), "status": "PASS"}


def render_install_verification(report: dict[str, Any]) -> str:
    lines = [
        "Keep Going install verification",
        f"- codex_home: {report['codex_home']}",
        f"- agents_home: {report['agents_home']}",
        f"- claude_home: {report['claude_home']}",
        f"- status: {'PASS' if report['ok'] else 'FAIL'}",
    ]
    for check in report["checks"]:
        lines.append(f"- {check['name']}: {check['status']} {check['path']}")
    return "\n".join(lines) + "\n"
