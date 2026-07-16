"""Build a local Keep Going reply package from decision policy and historical turns."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from keep_going.decision.policy_runtime import load_runtime_policy
from keep_going.decision.policy import RuleHit, draft_reply, estimate_confidence, match_rules, should_escalate


_WORD_RE = re.compile(r"[a-zA-Z0-9_./:-]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass(frozen=True)
class DecisionExample:
    turn_id: str
    project: str
    user_reply: str
    prev_assistant: str
    labels: tuple[str, ...]
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "project": self.project,
            "score": round(self.score, 4),
            "labels": list(self.labels),
            "prev_assistant": _clip(self.prev_assistant, 500),
            "user_reply": _clip(self.user_reply, 500),
        }


def load_decision_policy(path: Path) -> dict[str, Any]:
    if path.name.endswith(".runtime.yaml"):
        return load_runtime_policy(path)
    if not path.exists():
        raise FileNotFoundError(f"decision policy not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"decision policy must be a mapping: {path}")
    return data


def load_recent_context(path: Path | None) -> str:
    if path is None:
        return ""
    return path.read_text(encoding="utf-8")


def build_decision_reply(
    *,
    question: str,
    project: str,
    policy_path: Path,
    examples_path: Path,
    recent_context: str = "",
    top_k: int = 5,
    model: str = "",
    exclude_turn_ids: set[str] | None = None,
    generate: bool = False,
    generator: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    if not question.strip():
        raise ValueError("question must not be empty")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    policy = load_decision_policy(policy_path)
    examples = retrieve_examples(
        examples_path,
        question,
        project,
        top_k=top_k,
        exclude_turn_ids=exclude_turn_ids,
    )
    hits = match_rules(question, recent_context)
    confidence = estimate_confidence(question, examples, hits)
    escalate = should_escalate(question, confidence)
    reply = draft_reply(question, hits, examples, escalate)
    principles = _ordered_unique(p for hit in hits for p in hit.principles)
    prompt = build_decision_prompt(
        policy=policy,
        question=question,
        project=project,
        recent_context=recent_context,
        examples=examples,
        hits=hits,
    )
    generated = False
    if generate:
        reply = (generator or generate_reply_with_anthropic)(prompt, model).strip()
        generated = True

    return {
        "reply": reply,
        "confidence": confidence,
        "escalate": escalate,
        "principles_applied": principles,
        "heuristics_applied": [hit.rule_id for hit in hits],
        "few_shots": [example.to_dict() for example in examples],
        "prompt": prompt,
        "model": model,
        "policy_version": policy.get("version"),
        "generated": generated,
    }


def generate_reply_with_anthropic(prompt: str, model: str) -> str:
    if not model:
        raise ValueError("model must not be empty when generate=True")
    from anthropic import Anthropic

    message = Anthropic().messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [getattr(block, "text", "") for block in message.content]
    text = "\n".join(part for part in parts if part).strip()
    if not text:
        raise RuntimeError("Anthropic returned an empty reply")
    return text


def generate_reply_with_claude_cli(prompt: str, model: str) -> str:
    """Generate a Keep Going reply through the authenticated local Claude CLI.

    This is an explicit fallback for environments where Claude Code is logged in
    but Anthropic SDK credentials are not exported to the shell.
    """
    command = os.environ.get("KEEP_GOING_CLAUDE_CLI", "claude")
    executable = shutil.which(command) if Path(command).name == command else command
    if not executable:
        raise RuntimeError("Claude CLI not found; install `claude` or use the default Anthropic backend")

    cli_model = os.environ.get("KEEP_GOING_CLAUDE_CLI_MODEL") or model
    cmd = [
        executable,
        "-p",
        "--output-format",
        "text",
        "--no-session-persistence",
        "--tools",
        "",
        "--setting-sources",
        "user",
    ]
    if cli_model:
        cmd.extend(["--model", cli_model])
    if budget := os.environ.get("KEEP_GOING_CLAUDE_CLI_MAX_BUDGET_USD"):
        cmd.extend(["--max-budget-usd", budget])

    result = subprocess.run(cmd, input=prompt, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Claude CLI generation failed with exit code {result.returncode}: {_clip(detail, 1000)}")
    text = result.stdout.strip()
    if not text:
        raise RuntimeError("Claude CLI returned an empty reply")
    return text


def retrieve_examples(
    path: Path,
    question: str,
    project: str,
    *,
    top_k: int,
    exclude_turn_ids: set[str] | None = None,
) -> list[DecisionExample]:
    if not path.exists():
        return []
    excluded = exclude_turn_ids or set()
    query_tokens = _tokens(question)
    project_hint = _project_key(project)
    examples: list[DecisionExample] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row.get("role") != "user":
                continue
            if str(row.get("turn_id", "")) in excluded:
                continue
            score = _score_row(row, query_tokens, project_hint)
            if score <= 0:
                continue
            examples.append(
                DecisionExample(
                    turn_id=str(row.get("turn_id", "")),
                    project=str(row.get("project", "")),
                    user_reply=str(row.get("content", "")),
                    prev_assistant=str(row.get("prev_assistant") or ""),
                    labels=tuple(row.get("labels") or ()),
                    score=score,
                )
            )
    return sorted(examples, key=lambda x: x.score, reverse=True)[:top_k]


def build_decision_prompt(
    *,
    policy: dict[str, Any],
    question: str,
    project: str,
    recent_context: str,
    examples: list[DecisionExample],
    hits: list[RuleHit],
) -> str:
    policy_sections = yaml.safe_dump(policy, allow_unicode=True, sort_keys=False).strip()
    few_shots = _format_examples(examples)
    rule_hints = "\n".join(f"- {hit.rule_id}: {hit.reply_hint}" for hit in hits)
    context = _clip(recent_context, 3000) if recent_context else "(none)"
    return f"""你是用户的 Keep Going。目标：代用户给正在执行任务的 AI 一个短、准、可执行的回复。

硬约束：
- 使用中文简体。
- 信息密度高，先给结论。
- 不伪造验证；没有证据就要求验证。
- 不授权高风险操作，除非用户原话已经明确授权。
- 优先使用下方 decision policy 的偏好、启发式、协作模式、红线和词汇，而不只是通用原则。

用户决策 decision policy：
{policy_sections}

本次命中的规则：
{rule_hints}

历史 few-shot：
{few_shots}

当前项目：
{project or "(unknown)"}

近期上下文：
{context}

AI 的问题：
{question}

请输出用户最可能说的话。"""


def _score_row(row: dict[str, Any], query_tokens: set[str], project_hint: str) -> float:
    text = "\n".join(
        [
            str(row.get("prev_assistant") or "")[:3000],
            str(row.get("content") or "")[:1500],
        ]
    )
    row_tokens = _tokens(text)
    if not row_tokens:
        return 0.0
    overlap = len(query_tokens & row_tokens)
    score = overlap / math.sqrt(len(row_tokens))
    row_project = _project_key(str(row.get("project", "")))
    if project_hint and row_project and project_hint == row_project:
        score += 0.35
    if row.get("prev_assistant"):
        score += 0.05
    return score


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    tokens = set(_WORD_RE.findall(lowered))
    cjk = _CJK_RE.findall(text)
    tokens.update(a + b for a, b in zip(cjk, cjk[1:]))
    tokens.update(cjk)
    return {token for token in tokens if token.strip()}


def _project_key(project: str) -> str:
    parts = [p for p in Path(project).parts if p and p not in ("/", "Users", "USER")]
    return "/".join(parts[-2:])


def _format_examples(examples: list[DecisionExample]) -> str:
    if not examples:
        return "- (no local few-shot matched)"
    blocks = []
    for example in examples:
        blocks.append(
            "\n".join(
                [
                    f"- turn_id: {example.turn_id} score={example.score:.3f}",
                    f"  AI: {_clip(example.prev_assistant, 260)}",
                    f"  User: {_clip(example.user_reply, 260)}",
                ]
            )
        )
    return "\n".join(blocks)


def _ordered_unique(items: Any) -> list[Any]:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _clip(text: str, limit: int) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"
