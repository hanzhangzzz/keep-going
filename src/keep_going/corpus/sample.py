"""Stratified sampling: emit a Markdown digest that fits in a Claude chat window.

Strategy:
- Prefer user turns that have prev_assistant (responsive decisions > free-form prompts)
- Time strata: bias toward recent (0-30d 50% / 30-60d 30% / 60-90d 20%)
- Cap per-project to ensure diversity
- Truncate content for readability
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

console = Console()


def _bucket(ts: datetime, now: datetime) -> str:
    days = (now - ts).days
    if days <= 30:
        return "recent"
    if days <= 60:
        return "mid"
    return "old"


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[: n - 80] + "\n…[截断 " + str(len(s) - n + 80) + " 字符]…\n" + s[-60:]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def sample(
    turns_path: Path,
    out_path: Path,
    *,
    n: int = 150,
    per_project_cap: int = 5,
    only_with_prev: bool = True,
    seed: int = 42,
    label_filter: str | None = None,
) -> Path:
    rng = random.Random(seed)
    rows: list[dict] = []
    with turns_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("role") != "user":
                continue
            if only_with_prev and not obj.get("prev_assistant"):
                continue
            if label_filter is not None:
                labels = obj.get("labels") or []
                if label_filter not in labels:
                    continue
            rows.append(obj)

    now = datetime.now(timezone.utc)
    by_bucket: dict[str, list[dict]] = {"recent": [], "mid": [], "old": []}
    for r in rows:
        try:
            ts = _parse(r["ts"])
        except (KeyError, ValueError):
            continue
        by_bucket[_bucket(ts, now)].append(r)

    quotas = {
        "recent": int(n * 0.5),
        "mid": int(n * 0.3),
        "old": n - int(n * 0.5) - int(n * 0.3),
    }

    picked: list[dict] = []
    per_proj: dict[str, int] = {}
    for bucket, quota in quotas.items():
        pool = by_bucket[bucket][:]
        rng.shuffle(pool)
        taken = 0
        for r in pool:
            if taken >= quota:
                break
            proj = r.get("project", "")
            if per_proj.get(proj, 0) >= per_project_cap:
                continue
            picked.append(r)
            per_proj[proj] = per_proj.get(proj, 0) + 1
            taken += 1

    picked.sort(key=lambda r: r["ts"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# Keep Going 采样 · {len(picked)} 条 user turn\n\n")
        f.write(f"- 时间窗：近 90 天\n")
        f.write(f"- 分层配额（recent/mid/old）：{quotas}\n")
        f.write(f"- 每项目上限：{per_project_cap}\n")
        f.write(f"- 源：{turns_path}\n\n---\n\n")
        for i, r in enumerate(picked, 1):
            f.write(f"## [{i}/{len(picked)}] `{r['turn_id']}` · {r['source']} · {r['ts']}\n\n")
            f.write(f"- project: `{r['project']}`\n\n")
            f.write("**AI 上一条：**\n\n")
            f.write("```\n" + _truncate(r.get("prev_assistant"), 1200) + "\n```\n\n")
            f.write("**用户回复：**\n\n")
            f.write("```\n" + _truncate(r.get("content"), 1500) + "\n```\n\n---\n\n")

    console.print(f"[green]sampled[/green] {len(picked)} turns → {out_path}")
    console.print(f"  per-bucket: {quotas}")
    console.print(f"  unique projects: {len(per_proj)}")
    return out_path


def sample_themes(
    labeled_path: Path,
    out_path: Path,
    *,
    theme_quotas: dict[str, int],
    per_project_cap: int = 8,
    seed: int = 42,
) -> Path:
    """Multi-label themed sampling: pick N turns per label, output one combined markdown.

    Useful for in-chat distillation across multiple under-covered dimensions.
    """
    rng = random.Random(seed)
    rows_by_label: dict[str, list[dict]] = {label: [] for label in theme_quotas}

    with labeled_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("role") != "user":
                continue
            if not obj.get("prev_assistant"):
                # for themes we want responsive decisions when possible,
                # but allow no-prev for task-kickoff / context-statement
                pass
            for lab in obj.get("labels") or []:
                if lab in rows_by_label:
                    rows_by_label[lab].append(obj)

    picked_per_label: dict[str, list[dict]] = {}
    for lab, quota in theme_quotas.items():
        pool = rows_by_label[lab][:]
        rng.shuffle(pool)
        picked: list[dict] = []
        per_proj: dict[str, int] = {}
        for r in pool:
            if len(picked) >= quota:
                break
            proj = r.get("project", "")
            if per_proj.get(proj, 0) >= per_project_cap:
                continue
            picked.append(r)
            per_proj[proj] = per_proj.get(proj, 0) + 1
        picked.sort(key=lambda r: r["ts"])
        picked_per_label[lab] = picked

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        total = sum(len(v) for v in picked_per_label.values())
        f.write(f"# Keep Going 主题采样 · {total} 条 user turn 跨 {len(theme_quotas)} 类\n\n")
        f.write(f"- 来源：{labeled_path}\n")
        f.write(f"- 配额：{theme_quotas}\n\n")
        for lab, picks in picked_per_label.items():
            f.write(f"\n---\n\n# 主题：`{lab}`  ·  {len(picks)} / {theme_quotas[lab]} 条\n\n")
            for i, r in enumerate(picks, 1):
                f.write(f"## [{lab}#{i}] `{r['turn_id']}` · {r['source']} · {r['ts']}\n\n")
                f.write(f"- project: `{r['project']}`\n")
                f.write(f"- labels: {r.get('labels', [])}\n\n")
                prev = r.get("prev_assistant")
                if prev:
                    f.write("**AI 上一条：**\n\n")
                    snippet = prev[:1000]
                    if len(prev) > 1000:
                        snippet += "\n…[截断]…"
                    f.write("```\n" + snippet + "\n```\n\n")
                f.write("**用户回复：**\n\n")
                content = r.get("content", "")
                snippet = content[:1500]
                if len(content) > 1500:
                    snippet += "\n…[截断]…"
                f.write("```\n" + snippet + "\n```\n\n")

    console.print(f"[green]themed sample[/green] {total} turns → {out_path}")
    for lab, picks in picked_per_label.items():
        console.print(f"  · {lab}: {len(picks)}/{theme_quotas[lab]}")
    return out_path
