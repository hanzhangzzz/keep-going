"""Top-level Keep Going command-line interface."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .agents.distill import InsufficientCorpusError, distill_for_agent
from .agents.registry import (
    AGENT_DIR_MODE,
    POLICY_FILE_MODE,
    list_agents,
    load_meta,
    register_codex_agent,
    save_meta,
    validate_agent_name,
)
from .audit import render_audit_markdown, run_audit
from .config import load_config
from .corpus.classify import classify_all as _classify
from .corpus.harvest import harvest as _harvest
from .corpus.sample import sample as _sample
from .corpus.sample import sample_themes as _sample_themes
from .eval.conformance import run_conformance as _run_conformance
from .eval.loop_metrics import render_loop_metrics, run_loop_metrics
from .eval.overrides import render_override_audit, run_override_audit
from .eval.replay import run_eval as _run_eval
from .integration.bridge import (
    DEFAULT_BACKEND,
    disable_project,
    enable_project,
    handle_stop_hook,
    render_stop_hook_output,
    run_self_test,
    status_project,
)
from .integration.install import (
    HOST_PLUGIN_CHOICES,
    render_install_verification,
    run_installer,
    sync_local_install,
    verify_installation,
)
from .integration.package import package_keep_going
from .mcp_stdio import run_stdio_server
from .patterns.distill import distill_candidate as _distill_candidate
from .reasoning.extract import reason as _reason
from .decision.policy_runtime import compile_runtime_policy, load_runtime_policy, runtime_policy_path
from .decision.hook import handle_hook_event, parse_hook_event
from .decision.reply import build_decision_reply, generate_reply_with_claude_cli, load_recent_context

console = Console()


@click.group()
def main() -> None:
    """Keep Going: policy-driven Stop-hook harness for Claude Code and Codex."""


@main.command()
@click.option("--window-days", type=int, default=None, help="override config.window.days")
@click.option("--limit", type=int, default=None, help="cap kept turns (debug)")
def harvest(window_days: int | None, limit: int | None) -> None:
    """Walk Claude Code + Codex logs and emit data/turns/turns.jsonl."""
    cfg = load_config()
    _harvest(cfg, window_days=window_days, limit=limit)


@main.command()
@click.option("--limit", type=int, default=None, help="max turns to reason about")
@click.option("--model", type=str, default=None, help="model override")
def reason(limit: int | None, model: str | None) -> None:
    """Run LLM reasoning on harvested turns to produce decision why."""
    cfg = load_config()
    _reason(cfg, limit=limit, model=model)


@main.command()
@click.option("-n", type=int, default=150, show_default=True)
@click.option("--cap-per-project", type=int, default=8, show_default=True)
@click.option("--include-no-prev", is_flag=True, help="include turns without prev_assistant")
@click.option("--label", type=str, default=None, help="sample only this label")
@click.option("--out", type=click.Path(), default=None)
def sample(n: int, cap_per_project: int, include_no_prev: bool, label: str | None, out: str | None) -> None:
    """Stratified-time or single-label sampling from labeled turns."""
    cfg = load_config()
    _sample(cfg, n=n, cap_per_project=cap_per_project, include_no_prev=include_no_prev, label=label, out=out)


@main.command()
def classify() -> None:
    """Rule-engine multi-label classification on harvested turns."""
    cfg = load_config()
    _classify(cfg)


@main.command(name="sample-themes")
@click.option("--out", type=click.Path(), default=None, help="output markdown")
@click.option("--cap-per-project", type=int, default=8)
def sample_themes_cmd(out: str | None, cap_per_project: int) -> None:
    """Themed multi-label sampling for in-chat decision policy enrichment."""
    cfg = load_config()
    labeled = cfg.paths.data_dir / "labels" / "labeled.jsonl"
    if not labeled.exists():
        raise click.ClickException("run `keep-going classify` first")
    out_path = Path(out) if out else cfg.paths.data_dir / "samples" / "themes.md"
    quotas = {
        "task-kickoff": 15,
        "tool-evaluation": 15,
        "evidence-probe": 12,
        "ai-collab-meta": 15,
        "meta-self-reflection": 15,
        "interrupt-rollback": 12,
        "choice-among-options": 3,
        "context-statement": 10,
        "spec-elaboration": 12,
    }
    _sample_themes(labeled, out_path, theme_quotas=quotas, per_project_cap=cap_per_project)


@main.command()
@click.option("--out", type=click.Path(), default=None, help="output YAML path (default: artifacts/decision-policy.candidate.yaml)")
@click.option("--limit-per-signal", type=int, default=5, show_default=True)
def distill(out: str | None, limit_per_signal: int) -> None:
    """Generate a deterministic candidate decision policy from labeled turns."""
    cfg = load_config()
    try:
        path = _distill_candidate(
            cfg,
            out_path=Path(out) if out else None,
            limit_per_signal=limit_per_signal,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"wrote candidate decision policy → {path}")


@main.command(name="compile-policy")
@click.option("--source", type=click.Path(dir_okay=False), default=None, help="complete decision policy source")
@click.option("--out", type=click.Path(dir_okay=False), default=None, help="persisted runtime policy output")
def compile_policy(source: str | None, out: str | None) -> None:
    """Compile a complete decision policy into its persisted runtime artifact."""
    cfg = load_config() if source is None else None
    source_path = Path(source) if source else cfg.paths.artifacts_dir / "decision-policy.yaml"
    try:
        path = compile_runtime_policy(source_path, out_path=Path(out) if out else None)
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"wrote runtime policy → {path}")


@main.command("distill-mine")
@click.option("--name", required=True, help="agent name (must pass validate_agent_name)")
@click.option("--project", type=click.Path(), default=None, help="project dir for project-tier agent")
@click.option("--window-days", type=int, default=None, help="override harvest window (default: config.toml)")
def distill_mine(name: str, project: str | None, window_days: int | None) -> None:
    """[experimental] One-click distillation: harvest → classify → decision policy for a named agent."""
    try:
        path = distill_for_agent(
            name,
            project=project,
            cfg=load_config(),
            window_days=window_days,
        )
    except (ValueError, InsufficientCorpusError) as exc:
        raise click.ClickException(str(exc)) from exc
    except (FileNotFoundError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"distilled agent decision policy → {path}")


@main.command()
@click.option("--smoke", is_flag=True, help="run local runtime smoke checks without external API calls")
@click.option("--global-install", is_flag=True, help="read-only check real skill/agent/plugin installation targets")
@click.option("--codex-home", type=click.Path(file_okay=False), default=None, help="target CODEX_HOME for --global-install")
@click.option("--agents-home", type=click.Path(file_okay=False), default=None, help="target AGENTS_HOME for --global-install")
@click.option("--claude-home", type=click.Path(file_okay=False), default=None, help="target CLAUDE_HOME for --global-install")
@click.option("--json-output", is_flag=True, help="emit machine-readable JSON")
def audit(
    smoke: bool,
    global_install: bool,
    codex_home: str | None,
    agents_home: str | None,
    claude_home: str | None,
    json_output: bool,
) -> None:
    """Audit Keep Going readiness and list remaining blockers."""
    cfg = load_config()
    report = run_audit(
        cfg,
        smoke=smoke,
        global_install=global_install,
        codex_home=Path(codex_home) if codex_home else None,
        agents_home=Path(agents_home) if agents_home else None,
        claude_home=Path(claude_home) if claude_home else None,
    )
    if json_output:
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        click.echo(render_audit_markdown(report))


@main.command()
@click.option("--execute", is_flag=True, help="write skill/agent/plugin files to target homes")
@click.option("--verify", is_flag=True, help="read-only check that skill/agent/plugin files exist in target homes")
@click.option("--force", is_flag=True, help="replace existing Keep Going integration files during --execute")
@click.option("--upgrade", is_flag=True, help="alias for --force during --execute")
@click.option(
    "--register-hosts",
    type=click.Choice(HOST_PLUGIN_CHOICES),
    default="auto",
    show_default=True,
    help="register host plugins through detected official CLIs after --execute",
)
@click.option("--codex-home", type=click.Path(file_okay=False), default=None, help="target CODEX_HOME")
@click.option("--agents-home", type=click.Path(file_okay=False), default=None, help="target AGENTS_HOME")
@click.option("--claude-home", type=click.Path(file_okay=False), default=None, help="target CLAUDE_HOME")
def install(
    execute: bool,
    verify: bool,
    force: bool,
    upgrade: bool,
    register_hosts: str,
    codex_home: str | None,
    agents_home: str | None,
    claude_home: str | None,
) -> None:
    """Print or execute the Keep Going integration install plan."""
    cfg = load_config()
    try:
        replace_existing = force or upgrade
        if execute and verify:
            raise ValueError("--execute and --verify are mutually exclusive")
        if replace_existing and not execute:
            raise ValueError("--force/--upgrade requires --execute")
        if verify:
            report = verify_installation(
                codex_home=Path(codex_home) if codex_home else None,
                agents_home=Path(agents_home) if agents_home else None,
                claude_home=Path(claude_home) if claude_home else None,
            )
            click.echo(render_install_verification(report), nl=False)
            if not report["ok"]:
                raise click.ClickException("Keep Going integration is not fully installed")
            return
        output = run_installer(
            cfg,
            codex_home=Path(codex_home) if codex_home else None,
            agents_home=Path(agents_home) if agents_home else None,
            claude_home=Path(claude_home) if claude_home else None,
            execute=execute,
            force=replace_existing,
            register_hosts=register_hosts,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(output, nl=False)


@main.command(name="sync-local")
@click.option(
    "--register-hosts",
    type=click.Choice(HOST_PLUGIN_CHOICES),
    default="auto",
    show_default=True,
    help="register or refresh detected Claude Code/Codex plugins after syncing files",
)
@click.option("--codex-home", type=click.Path(file_okay=False), default=None, help="target CODEX_HOME")
@click.option("--agents-home", type=click.Path(file_okay=False), default=None, help="target AGENTS_HOME")
@click.option("--claude-home", type=click.Path(file_okay=False), default=None, help="target CLAUDE_HOME")
@click.option("--runtime-home", type=click.Path(file_okay=False), default=None, help="target KEEP_GOING_RUNTIME_HOME")
@click.option("--runtime-version", type=str, default=None, help="target runtime version directory")
@click.option("--no-verify", is_flag=True, help="skip install verification after syncing")
def sync_local(
    register_hosts: str,
    codex_home: str | None,
    agents_home: str | None,
    claude_home: str | None,
    runtime_home: str | None,
    runtime_version: str | None,
    no_verify: bool,
) -> None:
    """Sync this checkout's latest runtime, skill, plugin, wrappers, and hooks into the local host environment."""
    cfg = load_config()
    try:
        output, report = sync_local_install(
            cfg,
            codex_home=Path(codex_home) if codex_home else None,
            agents_home=Path(agents_home) if agents_home else None,
            claude_home=Path(claude_home) if claude_home else None,
            runtime_home=Path(runtime_home) if runtime_home else None,
            runtime_version=runtime_version,
            register_hosts=register_hosts,
            verify=not no_verify,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(output, nl=False)
    if report is not None:
        click.echo(render_install_verification(report), nl=False)
        if not report["ok"]:
            raise click.ClickException("Keep Going integration is not fully installed")
        click.echo("Keep Going local sync verified.")
    click.echo("sync-local completed.")


@main.command()
@click.option("--project", type=click.Path(file_okay=False), default=".", show_default=True)
@click.option("--host", type=click.Choice(["claude-code", "codex", "generic"]), default="codex", show_default=True)
@click.option("--backend", type=click.Choice(["cli"]), default=DEFAULT_BACKEND, show_default=True)
@click.option("--command", type=str, default=None, help='local CLI or alias command, e.g. "c 0" or "omxm"; implies --backend cli')
@click.option("--shell/--no-shell", default=False, show_default=True, help="run command through $SHELL -lc for local aliases/functions")
@click.option("--input-mode", type=click.Choice(["stdin", "append-arg"]), default="stdin", show_default=True)
@click.option("--force-skill", type=str, default="keep-going", show_default=True)
@click.option("--shell-executable", type=str, default=None, help="override shell used with --shell")
@click.option("--state-home", type=click.Path(file_okay=False), default=None, help="override bridge state directory")
@click.option("--agents", type=str, default=None, help="comma-separated agent names for fan-out")
@click.option("--render-mode", type=click.Choice(["block", "advisory"]), default="block", show_default=True)
@click.option(
    "--register-hosts",
    type=click.Choice(HOST_PLUGIN_CHOICES),
    default="auto",
    show_default=True,
    help="register host plugins through detected official CLIs",
)
@click.option("--codex-home", type=click.Path(file_okay=False), default=None, help="target CODEX_HOME")
@click.option("--agents-home", type=click.Path(file_okay=False), default=None, help="target AGENTS_HOME")
@click.option("--claude-home", type=click.Path(file_okay=False), default=None, help="target CLAUDE_HOME")
@click.option("--no-verify", is_flag=True, help="skip install verification and bridge self-test")
@click.option("--json-output", is_flag=True, help="emit enabled bridge state as machine-readable JSON")
def start(
    project: str,
    host: str,
    backend: str,
    command: str | None,
    shell: bool,
    input_mode: str,
    force_skill: str,
    shell_executable: str | None,
    state_home: str | None,
    agents: str | None,
    render_mode: str,
    register_hosts: str,
    codex_home: str | None,
    agents_home: str | None,
    claude_home: str | None,
    no_verify: bool,
    json_output: bool,
) -> None:
    """Install/refresh Keep Going, enable it for a project, and verify Stop hook readiness."""
    cfg = load_config()
    codex_path = Path(codex_home) if codex_home else None
    agents_path = Path(agents_home) if agents_home else None
    claude_path = Path(claude_home) if claude_home else None
    state_path = Path(state_home) if state_home else None
    agent_list = [a.strip() for a in agents.split(",") if a.strip()] if agents else None
    try:
        output = run_installer(
            cfg,
            codex_home=codex_path,
            agents_home=agents_path,
            claude_home=claude_path,
            execute=True,
            force=True,
            register_hosts=register_hosts,
        )
        click.echo(output, nl=False)
        if not no_verify:
            report = verify_installation(codex_home=codex_path, agents_home=agents_path, claude_home=claude_path)
            click.echo(render_install_verification(report), nl=False)
            if not report["ok"]:
                raise click.ClickException("Keep Going integration is not fully installed")
        state = enable_project(
            Path(project),
            host=host,
            backend=backend,
            command=command,
            shell=shell,
            input_mode=input_mode,
            force_skill=force_skill,
            shell_executable=shell_executable,
            state_home=state_path,
            agents=agent_list,
            render_mode=render_mode,
        )
        _register_agent_tomls(agent_list, cfg, codex_path, project)
        _emit_bridge_state(state, json_output=json_output)
        if not no_verify:
            self_test = run_self_test(cfg, project=Path(project), host=host)
            click.echo(f"Keep Going bridge self-test: {'PASS' if self_test['passed'] else 'FAIL'}")
            if not self_test["passed"]:
                raise click.ClickException("Keep Going bridge self-test failed")
            click.echo("Keep Going start verified.")
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@main.command(name="package")
@click.option("--out", type=click.Path(), default=None)
@click.option("--include-agents", is_flag=True, help="reserved; private agent snapshots are never packaged")
@click.option("--project", type=click.Path(), default=None, help="project dir for project-tier agents")
def package_cmd(out: str | None, include_agents: bool, project: str | None) -> None:
    """Export a privacy-safe activation package with a public policy template."""
    cfg = load_config()
    path = package_keep_going(
        cfg,
        out_dir=Path(out) if out else None,
        include_agents=include_agents,
        project=project,
    )
    console.print(f"[green]wrote Keep Going package → {path}[/green]")


@main.command(name="eval")
@click.option("--holdout-ratio", type=float, default=0.1, show_default=True)
@click.option("--limit", type=int, default=30, show_default=True)
@click.option("--generate", is_flag=True, help="use Claude to generate eval (requires ANTHROPIC_API_KEY)")
@click.option("--generate-backend", type=click.Choice(["sdk", "claude-cli"]), default="sdk", show_default=True)
def eval_cmd(
    holdout_ratio: float,
    limit: int,
    generate: bool,
    generate_backend: str,
) -> None:
    """Evaluate Keep Going reply quality against holdout turns."""
    cfg = load_config()
    generator = generate_reply_with_claude_cli if generate and generate_backend == "claude-cli" else None
    report_path = _run_eval(cfg, holdout_ratio=holdout_ratio, limit=limit, generate=generate, generator=generator)
    click.echo(f"wrote eval report to {report_path}")


@main.command()
@click.option("--out", type=click.Path(), default=None, help="output markdown path")
@click.option("--top-k", type=int, default=5, show_default=True)
@click.option("--json-output", is_flag=True, help="emit machine-readable JSON")
def conformance(out: str | None, top_k: int, json_output: bool) -> None:
    """Run offline behavioral conformance checks for key Keep Going decisions."""
    cfg = load_config()
    try:
        report = _run_conformance(cfg, out_path=Path(out) if out else None, top_k=top_k)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if json_output:
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))
        return
    console.print(f"[green]wrote conformance report → {report['out_path']}[/green]")
    if not report["passed"]:
        raise click.ClickException(f"conformance failed: {report['passed_cases']} / {report['total_cases']} passed")


@main.command(name="loop-metrics")
@click.option("--turns", type=click.Path(dir_okay=False), default=None, help="turns.jsonl path; defaults to data/turns/turns.jsonl")
@click.option(
    "--events",
    type=click.Path(dir_okay=False),
    default=None,
    help="Stop hook event JSONL path; defaults to ~/.keep-going/events/stop-hook.jsonl",
)
@click.option("--project", "projects", multiple=True, help="project path/name filter; can be passed multiple times")
@click.option("--split-at", type=str, default=None, help="ISO timestamp separating before/after adoption windows")
@click.option("--json-output", is_flag=True, help="emit machine-readable JSON")
@click.option("--out", type=click.Path(dir_okay=False), default=None, help="write report to this path")
def loop_metrics_cmd(
    turns: str | None,
    events: str | None,
    projects: tuple[str, ...],
    split_at: str | None,
    json_output: bool,
    out: str | None,
) -> None:
    """Measure mean time between human interventions before/after Keep Going adoption."""
    cfg = load_config()
    try:
        report = run_loop_metrics(
            cfg,
            turns_path=Path(turns) if turns else None,
            events_path=Path(events) if events else None,
            projects=projects,
            split_at=_parse_cli_datetime(split_at) if split_at else None,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    rendered = json.dumps(report, ensure_ascii=False, indent=2) if json_output else render_loop_metrics(report)
    if out:
        Path(out).write_text(rendered, encoding="utf-8")
        click.echo(f"loop metrics written to {out}")
    else:
        click.echo(rendered)


@main.command(name="overrides")
@click.option(
    "--events",
    type=click.Path(dir_okay=False),
    default=None,
    help="Stop hook event JSONL path; defaults to ~/.keep-going/events/stop-hook.jsonl",
)
@click.option("--project", "projects", multiple=True, help="project path/name filter; can be passed multiple times")
@click.option("--window-days", type=int, default=None, help="override config.window.days")
@click.option("--json-output", is_flag=True, help="emit machine-readable JSON")
@click.option("--out", type=click.Path(dir_okay=False), default=None, help="write report to this path")
def overrides_cmd(
    events: str | None,
    projects: tuple[str, ...],
    window_days: int | None,
    json_output: bool,
    out: str | None,
) -> None:
    """Audit how often humans overturned Keep Going stop-hook replies (quality loop)."""
    cfg = load_config()
    try:
        report = run_override_audit(
            cfg,
            events_path=Path(events) if events else None,
            projects=projects,
            window_days=window_days,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    rendered = json.dumps(report, ensure_ascii=False, indent=2) if json_output else render_override_audit(report)
    if out:
        Path(out).write_text(rendered, encoding="utf-8")
        click.echo(f"override audit written to {out}")
    else:
        click.echo(rendered)


@main.command()
def mcp() -> None:
    """Run the MCP stdio server for Keep Going."""
    run_stdio_server(load_config())


@main.command()
@click.option("--input-json", is_flag=True, help="read hook event from stdin as JSON")
@click.option("--top-k", type=int, default=5, show_default=True)
@click.option("--generate", is_flag=True)
def hook(input_json: bool, top_k: int, generate: bool) -> None:
    """Process a hook event and decide whether to ask the Keep Going."""
    cfg = load_config()
    if input_json:
        raw = sys.stdin.read()
        event = parse_hook_event(raw)
    else:
        raise click.ClickException("hook requires --input-json")
    result = handle_hook_event(cfg, event, top_k=top_k, generate=generate)
    click.echo(json.dumps(result, ensure_ascii=False))


@main.group()
def bridge() -> None:
    """Manage project-level Keep Going Stop hook activation."""


@bridge.command("setup")
@click.option("--enable", "mode", flag_value="enable", default="enable", help="enable Keep Going for this project")
@click.option("--disable", "mode", flag_value="disable", help="disable Keep Going for this project")
@click.option("--project", type=click.Path(file_okay=False), default=".", show_default=True)
@click.option("--host", type=click.Choice(["claude-code", "codex", "generic"]), default="claude-code", show_default=True)
@click.option("--backend", type=click.Choice(["cli"]), default=DEFAULT_BACKEND, show_default=True)
@click.option("--command", type=str, default=None, help='local CLI or alias command, e.g. "c 0" or "omxm"; implies --backend cli')
@click.option("--shell/--no-shell", default=False, show_default=True, help="run command through $SHELL -lc for local aliases/functions")
@click.option("--input-mode", type=click.Choice(["stdin", "append-arg"]), default="stdin", show_default=True)
@click.option("--force-skill", type=str, default="keep-going", show_default=True)
@click.option("--shell-executable", type=str, default=None, help="override shell used with --shell")
@click.option("--state-home", type=click.Path(file_okay=False), default=None, help="override bridge state directory")
@click.option("--json-output", is_flag=True, help="emit machine-readable JSON")
def bridge_setup(
    mode: str,
    project: str,
    host: str,
    backend: str,
    command: str | None,
    shell: bool,
    input_mode: str,
    force_skill: str,
    shell_executable: str | None,
    state_home: str | None,
    json_output: bool,
) -> None:
    """One-shot enable/disable command for host control surfaces."""
    if mode == "disable":
        bridge_disable.callback(project, state_home, json_output)  # type: ignore[attr-defined]
        return
    bridge_enable.callback(  # type: ignore[attr-defined]
        project,
        host,
        backend,
        command,
        shell,
        input_mode,
        force_skill,
        shell_executable,
        state_home,
        json_output,
    )


@bridge.command("enable")
@click.option("--project", type=click.Path(file_okay=False), default=".", show_default=True)
@click.option("--host", type=click.Choice(["claude-code", "codex", "generic"]), default="claude-code", show_default=True)
@click.option("--backend", type=click.Choice(["cli"]), default=DEFAULT_BACKEND, show_default=True)
@click.option("--command", type=str, default=None, help='local CLI or alias command, e.g. "c 0" or "omxm"; implies --backend cli')
@click.option("--shell/--no-shell", default=False, show_default=True, help="run command through $SHELL -lc for local aliases/functions")
@click.option("--input-mode", type=click.Choice(["stdin", "append-arg"]), default="stdin", show_default=True)
@click.option("--force-skill", type=str, default="keep-going", show_default=True)
@click.option("--shell-executable", type=str, default=None, help="override shell used with --shell")
@click.option("--state-home", type=click.Path(file_okay=False), default=None, help="override bridge state directory")
@click.option("--json-output", is_flag=True, help="emit machine-readable JSON")
def bridge_enable(
    project: str,
    host: str,
    backend: str,
    command: str | None,
    shell: bool,
    input_mode: str,
    force_skill: str,
    shell_executable: str | None,
    state_home: str | None,
    json_output: bool,
) -> None:
    """Enable Keep Going Stop hook for one project root."""
    try:
        state = enable_project(
            Path(project),
            host=host,
            backend=backend,
            command=command,
            shell=shell,
            input_mode=input_mode,
            force_skill=force_skill,
            shell_executable=shell_executable,
            state_home=Path(state_home) if state_home else None,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_bridge_state(state, json_output=json_output)


@bridge.command("disable")
@click.option("--project", type=click.Path(file_okay=False), default=".", show_default=True)
@click.option("--state-home", type=click.Path(file_okay=False), default=None)
@click.option("--json-output", is_flag=True)
def bridge_disable(project: str, state_home: str | None, json_output: bool) -> None:
    """Disable Keep Going Stop hook for one project root."""
    state = disable_project(Path(project), state_home=Path(state_home) if state_home else None)
    _emit_bridge_state(state, json_output=json_output)


@bridge.command("status")
@click.option("--project", type=click.Path(file_okay=False), default=".", show_default=True)
@click.option("--state-home", type=click.Path(file_okay=False), default=None)
@click.option("--json-output", is_flag=True)
def bridge_status(project: str, state_home: str | None, json_output: bool) -> None:
    """Show current bridge state for a project."""
    state = status_project(Path(project), state_home=Path(state_home) if state_home else None)
    _emit_bridge_state(state, json_output=json_output)


@bridge.command("stop-hook")
@click.option("--input-json", is_flag=True, help="read event from stdin as JSON")
@click.option("--host", type=click.Choice(["claude-code", "codex", "generic"]), default=None)
@click.option("--state-home", type=click.Path(file_okay=False), default=None)
@click.option("--synthetic", is_flag=True, help="synthetic probe, skip metrics recording")
@click.option("--json-output", is_flag=True, help="emit the full result dict as JSON")
def bridge_stop_hook(
    input_json: bool,
    host: str | None,
    state_home: str | None,
    synthetic: bool,
    json_output: bool,
) -> None:
    """Handle a Stop hook event: consult Keep Going and return a host-appropriate response."""
    cfg = load_config()
    if input_json:
        raw = sys.stdin.read()
        event = json.loads(raw)
    else:
        raise click.ClickException("bridge stop-hook requires --input-json")
    result = handle_stop_hook(
        cfg,
        event,
        host=host,
        state_home=Path(state_home) if state_home else None,
        record_metrics=not synthetic,
    )
    output = render_stop_hook_output(result)
    if json_output:
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    elif output:
        click.echo(output)


@bridge.command("self-test")
@click.option("--project", type=click.Path(file_okay=False), default=".")
@click.option("--host", type=click.Choice(["claude-code", "codex"]), default="codex", show_default=True)
@click.option("--json-output", is_flag=True)
def bridge_self_test(project: str, host: str, json_output: bool) -> None:
    """Run a local self-test to verify the bridge can block and allow correctly."""
    cfg = load_config()
    report = run_self_test(cfg, project=Path(project), host=host)
    if json_output:
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        click.echo(f"self-test: {'PASS' if report['passed'] else 'FAIL'}")
        for key in ("project", "host", "disabled_action", "enabled_action"):
            click.echo(f"  {key}: {report.get(key)}")


@main.command()
@click.option("-q", "--question", type=str, default=None, help="question for Keep Going to answer")
@click.option("--project", type=str, default=None, help="project path for context")
@click.option("--policy-path", "policy_path", type=click.Path(exists=True), default=None, help="override decision policy file (highest priority)")
@click.option("--agent", "agent_name", type=str, default=None, help="named policy agent; omit = your canonical policy (default)")
@click.option("--examples-path", type=click.Path(exists=True), default=None, help="override examples file")
@click.option("--recent-context", type=click.Path(), default=None, help="file with recent context text")
@click.option("--input-json", is_flag=True, help="read question from stdin JSON")
@click.option("--top-k", type=int, default=5, show_default=True)
@click.option("--generate", is_flag=True, help="use Claude API to generate reply")
@click.option("--generate-backend", type=click.Choice(["sdk", "claude-cli"]), default="sdk", show_default=True)
@click.option("--reply-only", is_flag=True, help="print only the drafted reply")
def reply(
    question: str | None,
    project: str | None,
    policy_path: str | None,
    agent_name: str | None,
    examples_path: str | None,
    recent_context: str | None,
    input_json: bool,
    top_k: int,
    generate: bool,
    generate_backend: str,
    reply_only: bool,
) -> None:
    """Answer a question using the user's decision policy."""
    cfg = load_config()
    resolved_question, resolved_project, context_text, resolved_agent = _resolve_reply_input(
        question, project, recent_context, input_json, agent_name,
    )
    labels = cfg.paths.data_dir / "labels" / "labeled.jsonl"
    turns = cfg.paths.data_dir / "turns" / "turns.jsonl"
    resolved_policy = _resolve_reply_policy(cfg, policy_path, resolved_agent, resolved_project)
    resolved_examples = Path(examples_path) if examples_path else (labels if labels.exists() else turns)
    result = build_decision_reply(
        question=resolved_question,
        project=resolved_project,
        policy_path=resolved_policy,
        examples_path=resolved_examples,
        recent_context=context_text,
        top_k=top_k,
        model=cfg.models.decision,
        generate=generate,
        generator=generate_reply_with_claude_cli if generate and generate_backend == "claude-cli" else None,
    )
    if reply_only:
        click.echo(result.get("reply", ""))
    else:
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))


def _policy_summary(path: Path) -> dict[str, object]:
    """Summarize a decision policy file: existence, principle count, stub detection.

    A decision policy with fewer than 3 core_principles, or any principle whose statement
    is literally ``test``, is flagged ``looks_stub`` — the canonical file was
    likely overwritten by a placeholder.
    """
    if not path.is_file():
        return {"exists": False, "principles": 0, "looks_stub": False, "lines": 0}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml

        data = yaml.safe_load(text) or {}
    except Exception:
        data = {}
    principles = data.get("core_principles") or []
    count = len(principles) if isinstance(principles, list) else 0
    stub = count < 3 or any(
        isinstance(p, dict) and str(p.get("statement", "")).strip().lower() == "test"
        for p in (principles if isinstance(principles, list) else [])
    )
    return {
        "exists": True,
        "principles": count,
        "looks_stub": stub,
        "lines": len(text.splitlines()),
    }


def _runtime_policy_summary(canonical: Path) -> dict[str, object]:
    runtime = runtime_policy_path(canonical)
    summary: dict[str, object] = {"path": str(runtime), "exists": runtime.is_file(), "valid": False}
    try:
        loaded = load_runtime_policy(canonical)
    except (FileNotFoundError, ValueError) as exc:
        summary["error"] = str(exc)
        return summary
    summary.update(
        {
            "valid": True,
            "runtime_schema_version": loaded.get("runtime_schema_version"),
            "source_sha256": hashlib.sha256(canonical.read_bytes()).hexdigest(),
            "runtime_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        }
    )
    return summary


@main.command("status")
@click.option("--project", type=click.Path(file_okay=False), default=".", show_default=True)
@click.option("--json-output", is_flag=True, help="emit machine-readable JSON")
def status(project: str, json_output: bool) -> None:
    """One-look overview: your decision policy, named agents, and this project's binding."""
    cfg = load_config()
    canonical = cfg.paths.artifacts_dir / "decision-policy.yaml"
    summary = _policy_summary(canonical)
    runtime_summary = _runtime_policy_summary(canonical)
    agents = list_agents(scope="all", project=project)
    proj_state = status_project(project)
    bound = proj_state.get("agents") or ["default"]

    payload = {
        "canonical_policy": {"path": str(canonical), **summary},
        "runtime_policy": runtime_summary,
        "named_agents": agents,
        "project": {
            "path": proj_state.get("project"),
            "stop_hook_enabled": bool(proj_state.get("enabled")),
            "host": proj_state.get("host"),
            "bound_agents": bound,
            "state_file": proj_state.get("state_file"),
        },
    }
    if json_output:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    click.echo("Keep Going Status\n")
    health = "⚠️  疑似占位/被覆盖" if summary["looks_stub"] else "✅ healthy"
    miss = "  (缺失！)" if not summary["exists"] else ""
    click.echo("── 你本人 decision policy (default / myself) ──")
    click.echo(f"  path: {canonical}{miss}")
    click.echo(f"  core_principles: {summary['principles']}   {health}")
    runtime_health = "✅ valid" if runtime_summary["valid"] else "⚠️  invalid / missing"
    click.echo(f"  runtime: {runtime_summary['path']}   {runtime_health}")
    if runtime_summary["valid"]:
        click.echo(f"  source_sha256: {runtime_summary['source_sha256']}")
        click.echo(f"  runtime_sha256: {runtime_summary['runtime_sha256']}")

    click.echo("\n── 命名 agents ──")
    if not agents:
        click.echo("  (none)  ·  用 'keep-going distill-mine --name <name>' 蒸馏一个")
    else:
        table = Table(show_header=True)
        table.add_column("name", style="cyan")
        table.add_column("scope")
        table.add_column("decision policy")
        table.add_column("updated_at")
        for e in sorted(agents, key=lambda x: x["name"]):
            policy_ok = "✅" if e["current_policy"] and Path(e["current_policy"]).is_file() else "⚠️ 缺失"
            table.add_row(e["name"], e["scope"], policy_ok, e["updated_at"])
        console.print(table)

    click.echo(f"\n── 当前项目 ({proj_state.get('project')}) ──")
    hook = f"enabled (host={proj_state.get('host')})" if proj_state.get("enabled") else "disabled"
    click.echo(f"  Stop hook: {hook}")
    if bound == ["default"]:
        click.echo(f"  绑定 decision policy: default → {runtime_summary['path']} (compiled from canonical)")
    else:
        click.echo(f"  绑定 decision policy: {', '.join(bound)}")


# ── Agent subcommand group (U4) ─────────────────────────────────────────────


@main.group()
def agent() -> None:
    """Manage named Keep Going agents (data-plane CRUD)."""


def _agent_root() -> Path:
    override = os.environ.get("KEEP_GOING_AGENTS_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".keep-going" / "agents"


def _agent_dir(name: str, *, scope: str, project: str | None) -> Path:
    if scope == "project" and project:
        return Path(project) / ".keep-going" / "agents" / name
    return _agent_root() / name


@agent.command("new")
@click.argument("name")
@click.option("--description", type=str, default="", help="human-readable description")
@click.option("--scope", type=click.Choice(["global", "project"]), default="global", show_default=True)
@click.option("--project", type=click.Path(file_okay=False), default=".", show_default=True, help="project root for --scope project")
@click.option("--force", is_flag=True, help="overwrite existing agent with same name")
@click.option("--from-template", type=click.Path(exists=True), default=None, help="custom decision policy template")
def agent_new(name: str, description: str, scope: str, project: str, force: bool, from_template: str | None) -> None:
    """Create a new named agent with a starter decision policy."""
    validation = validate_agent_name(name)
    if not validation["ok"]:
        raise click.ClickException(f"invalid agent name: {validation['reason']}")

    dest = _agent_dir(name, scope=scope, project=project)

    if dest.exists() and any(dest.iterdir()):
        if not force:
            raise click.ClickException(f"agent {name!r} already exists at {dest}; use --force to overwrite")
        backup = dest.parent / f"{name}.bak-{_now_ts()}"
        dest.rename(backup)
        click.echo(f"existing agent backed up to {backup}")

    dest.mkdir(parents=True, exist_ok=True)
    try:
        dest.chmod(AGENT_DIR_MODE)
    except OSError:
        pass

    template_src = Path(from_template) if from_template else _default_template()
    policy_name = f"policy-{_now_ts()}.yaml"
    policy_path = dest / policy_name
    if template_src.is_file():
        policy_path.write_text(template_src.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        policy_path.write_text("# Keep Going agent decision policy\nversion: 0.1\n", encoding="utf-8")
    try:
        policy_path.chmod(POLICY_FILE_MODE)
    except OSError:
        pass

    author = _get_author()
    meta = {
        "name": name,
        "description": description,
        "author": author,
        "scope": scope,
        "current_policy": str(policy_path),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    save_meta(dest, meta)

    click.echo(f"agent {name!r} created at {dest}")
    click.echo(f"  decision policy: {policy_path}")
    click.echo(f"  author: {author}")


@agent.command("list")
@click.option("--scope", type=click.Choice(["all", "global", "project"]), default="all", show_default=True)
@click.option("--project", type=click.Path(file_okay=False), default=".", show_default=True)
def agent_list(scope: str, project: str) -> None:
    """List registered agents across global and project tiers."""
    agents = list_agents(scope=scope, project=project)
    if not agents:
        click.echo("no agents found.")
        return
    table = Table(title="Keep Going Agents")
    table.add_column("name", style="cyan")
    table.add_column("scope")
    table.add_column("current_policy", max_width=50, no_wrap=True)
    table.add_column("updated_at")
    for entry in sorted(agents, key=lambda e: e["name"]):
        table.add_row(entry["name"], entry["scope"], entry["current_policy"], entry["updated_at"])
    console.print(table)


@agent.command("show")
@click.argument("name")
@click.option("--project", type=click.Path(file_okay=False), default=".", show_default=True)
def agent_show(name: str, project: str) -> None:
    """Show agent meta and current decision policy summary."""
    from .agents.registry import resolve_agent
    cfg = load_config()
    resolved = resolve_agent(name, project=project, canonical_policy=cfg.paths.artifacts_dir / "decision-policy.yaml")
    if not resolved["valid"]:
        raise click.ClickException(f"agent {name!r} not found ({resolved.get('reason')})")

    agent_dir = resolved["path"].parent if resolved["path"] else None
    meta = load_meta(agent_dir) if agent_dir else {}
    click.echo("meta.json:")
    click.echo(json.dumps(meta, ensure_ascii=False, indent=2))

    policy_path = resolved["path"]
    if policy_path and policy_path.is_file():
        click.echo("\nDecision policy summary:")
        lines = policy_path.read_text(encoding="utf-8").splitlines()
        for line in lines[:30]:
            click.echo(line)
        if len(lines) > 30:
            click.echo(f"... ({len(lines) - 30} more lines)")


@agent.command("edit")
@click.argument("name")
@click.option("--project", type=click.Path(file_okay=False), default=".", show_default=True)
def agent_edit(name: str, project: str) -> None:
    """Open agent's current decision policy in $EDITOR for manual editing."""
    from .agents.registry import resolve_agent
    cfg = load_config()
    resolved = resolve_agent(name, project=project, canonical_policy=cfg.paths.artifacts_dir / "decision-policy.yaml")
    if not resolved["valid"] or not resolved["path"] or not resolved["path"].is_file():
        raise click.ClickException(f"agent {name!r} has no current decision policy to edit")

    policy_path = resolved["path"]
    backup_path = policy_path.parent / f"{policy_path.name}.bak-{_now_ts()}"
    backup_path.write_text(policy_path.read_text(encoding="utf-8"), encoding="utf-8")

    editor = os.environ.get("EDITOR", "vi")
    result = subprocess.run([editor, str(policy_path)])

    if result.returncode != 0:
        _restore_backup(policy_path, backup_path)
        raise click.ClickException(f"editor exited with code {result.returncode}; decision policy restored from backup")

    validation_error = _validate_policy(policy_path)
    if validation_error:
        _restore_backup(policy_path, backup_path)
        raise click.ClickException(f"decision policy validation failed ({validation_error}); restored from backup")

    backup_path.unlink(missing_ok=True)
    agent_dir = policy_path.parent
    meta = load_meta(agent_dir)
    meta["updated_at"] = _now_iso()
    save_meta(agent_dir, meta)
    click.echo(f"agent {name!r} decision policy updated.")


@agent.command("delete")
@click.argument("name")
@click.option("--purge", is_flag=True, help="permanently delete (no trash)")
@click.option("--project", type=click.Path(file_okay=False), default=".", show_default=True)
def agent_delete(name: str, purge: bool, project: str) -> None:
    """Delete an agent (default: move to trash; --purge for permanent)."""
    if name == "default":
        raise click.ClickException("cannot delete the default agent")

    from .agents.registry import resolve_agent
    cfg = load_config()
    resolved = resolve_agent(name, project=project, canonical_policy=cfg.paths.artifacts_dir / "decision-policy.yaml")
    if not resolved["valid"] or not resolved["path"]:
        raise click.ClickException(f"agent {name!r} not found ({resolved.get('reason')})")

    agent_dir = resolved["path"].parent
    if not agent_dir.is_dir():
        raise click.ClickException(f"agent directory not found: {agent_dir}")

    if purge:
        import shutil
        shutil.rmtree(agent_dir)
        click.echo(f"agent {name!r} permanently deleted.")
        return

    agents_root = _agent_root() if resolved["scope"] == "global" else Path(project) / ".keep-going" / "agents"
    trash_dir = agents_root / ".trash" / f"{name}-{_now_ts()}"
    trash_dir.parent.mkdir(parents=True, exist_ok=True)
    agent_dir.rename(trash_dir)
    _log_trash(name, str(agent_dir), str(trash_dir))
    click.echo(f"agent {name!r} moved to trash: {trash_dir}")


# ── helpers ──────────────────────────────────────────────────────────────────


def _resolve_reply_input(
    question: str | None,
    project: str | None,
    recent_context: str | None,
    input_json: bool,
    agent_name: str | None = None,
) -> tuple[str, str, str, str | None]:
    if input_json:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        resolved_question = str(payload.get("question") or payload.get("message") or "").strip()
        resolved_project = str(payload.get("project") or project or "")
        context_text = str(payload.get("recent_context") or payload.get("context") or "")
        resolved_agent = agent_name or payload.get("agent")
    else:
        resolved_question = sys.stdin.read().strip() if question == "-" else (question or "").strip()
        resolved_project = project
        context_text = load_recent_context(Path(recent_context) if recent_context else None)
        resolved_agent = agent_name
    if not resolved_question:
        raise click.ClickException("question is required; pass --question or --input-json")
    return resolved_question, resolved_project, context_text, resolved_agent


def _resolve_reply_policy(
    cfg: object,
    policy_path: str | None,
    agent_name: str | None,
    project: str | None,
) -> Path:
    """Resolve which decision policy file a reply should embody.

    Priority: explicit ``--policy-path`` > named ``--agent`` > canonical (the
    user's own decision policy, i.e. the ``default`` agent). Omitting both means "answer
    as myself".
    """
    canonical = cfg.paths.artifacts_dir / "decision-policy.yaml"
    if policy_path:
        return Path(policy_path)
    if agent_name and agent_name != "default":
        from .agents.registry import resolve_agent

        resolved = resolve_agent(agent_name, project=project, canonical_policy=canonical)
        if not resolved["valid"] or not resolved["path"]:
            raise click.ClickException(
                f"agent {agent_name!r} not found ({resolved.get('reason')}); "
                f"run 'keep-going agent list' to see available agents"
            )
        return resolved["path"]
    return runtime_policy_path(canonical)


def _register_agent_tomls(
    agent_list: list[str] | None,
    cfg: object,
    codex_home: Path | None,
    project: str,
) -> None:
    """Register Codex agent TOML files for each named agent in the list."""
    if not agent_list:
        return
    from .agents.registry import resolve_agent

    cfg_typed = cfg
    artifacts_dir = getattr(cfg_typed, "paths", None)
    if artifacts_dir is None:
        return
    canonical_policy = getattr(artifacts_dir, "artifacts_dir", Path("artifacts")) / "decision-policy.yaml"
    for name in agent_list:
        if name == "default":
            continue
        resolved = resolve_agent(name, project=project, canonical_policy=canonical_policy)
        if not resolved["valid"] or not resolved["path"]:
            continue
        register_codex_agent(name, resolved["path"], codex_home=codex_home)


def _emit_bridge_state(state: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        click.echo(json.dumps(state, ensure_ascii=False, indent=2))
        return
    click.echo("Keep Going bridge state")
    for key in ("enabled", "project", "host", "backend", "command", "shell", "input_mode", "force_skill", "state_file"):
        click.echo(f"- {key}: {state.get(key)}")


def _parse_cli_datetime(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise click.ClickException(f"invalid --split-at ISO timestamp: {raw}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _get_author() -> str:
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return os.environ.get("USER", "unknown")


def _default_template() -> Path:
    cfg = load_config()
    project_template = cfg.root / "artifacts" / "decision-policy.template.yaml"
    if project_template.is_file():
        return project_template
    return Path(__file__).resolve().parent.parent.parent / "artifacts" / "decision-policy.template.yaml"


def _validate_policy(policy_path: Path) -> str | None:
    import yaml
    try:
        data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return f"YAML parse error: {exc}"
    if not isinstance(data, dict):
        return "decision policy must be a YAML mapping"
    for required in ("core_principles", "preferences", "redlines"):
        if required not in data:
            return f"missing required section: {required}"
    return None


def _restore_backup(policy_path: Path, backup_path: Path) -> None:
    if backup_path.is_file():
        policy_path.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")


def _log_trash(name: str, source: str, trash: str) -> None:
    agents_root = _agent_root()
    log_dir = agents_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "agent-trash.jsonl"
    entry = {
        "ts": _now_iso(),
        "action": "trash",
        "name": name,
        "source": source,
        "trash": trash,
    }
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
