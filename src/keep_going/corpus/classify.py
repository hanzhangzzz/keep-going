"""Rule-based multi-label classifier for user turns.

Goal: do cheap upstream filtering so LLM (or human) reasoning only sees
high-signal slices. Each user turn can carry multiple labels.

Coverage-over-precision: prefer to over-tag than to miss. Downstream
sampling will pick representative examples per label.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from rich.console import Console

from ..config import Config

console = Console()


# ─────────────────────────────────────────────────────────────
# Label rules. Each label has a list of regex patterns;
# matching ANY pattern earns the label.
# Patterns are intentionally broad — coverage > precision here.
# ─────────────────────────────────────────────────────────────
LABEL_RULES: dict[str, list[re.Pattern[str]]] = {
    "rejection": [
        re.compile(r"^(不行|不对|不要|不必|不用|别这|没必要)"),
        re.compile(r"(取消|废弃|搁置|放弃这|跳过这)"),
        re.compile(r"(不应该|不该这|不见得)"),
        re.compile(r"(算了|罢了)"),
    ],
    "scope-correction": [
        re.compile(r"(顺手|无关|只改|只.*相关|不要碰|别动)"),
        re.compile(r".{0,15}不要.{0,5}(提|动|改|加)"),
        re.compile(r"(把.{1,15}(删|去掉|拿掉|拆掉))"),
    ],
    "tool-evaluation": [
        re.compile(r"(哪个|哪种|哪些).{0,10}(更好|更适合|更快|更.*合适|强)"),
        re.compile(r"(对比|区别|分别|差异)"),
        re.compile(r"(还是|或者).{0,30}(更|好|选)"),
        re.compile(r"^(用|我用).{2,30}(还是|或者)"),
    ],
    "strategy-meta": [
        re.compile(r"(方向|长期|战略|路线|主线|大方向)"),
        re.compile(r"(投入产出|ROI|价值|优先级|更值得|更.*重要)"),
        re.compile(r"(共性|本质|核心.*问题|根本)"),
        re.compile(r"(还.*没.*探索|还需要.*研究|没.*想.*清楚)"),
    ],
    "verification-demand": [
        re.compile(r"(端到端|跑一遍|真.{0,3}跑|实际.{0,3}跑|本机.{0,3}跑|先在.{0,5}运行)"),
        re.compile(r"(自己验证|自验|自己.{0,3}调试|自己.{0,3}确认|自己.{0,3}测试)"),
        re.compile(r"(证据链|彻底|完整的证据)"),
        re.compile(r"(验证.{0,5}通过|跑.{0,3}测试)"),
    ],
    "evidence-probe": [
        re.compile(r"(你确定|确定吗|确认一下)"),
        re.compile(r"(质疑|不一定|不见得)"),
        re.compile(r"(为什么|为啥).{0,15}(这样|这么|这种)"),
        re.compile(r"(怎么.{0,3}确定|怎么.{0,3}证明)"),
        re.compile(r"(根因|根本原因)"),
    ],
    "writing-style": [
        re.compile(r"(简短|精简|啰嗦|冗长|累赘|太长|太多)"),
        re.compile(r"(突出|强调|聚焦)"),
        re.compile(r"(重写|换.{0,3}版|换.{0,3}风格|.{0,3}版本)"),
        re.compile(r"(读者|受众|场景|口吻|语气|风格)"),
        re.compile(r"(整理.{0,5}md|写成.{0,5}(md|markdown|文档))"),
    ],
    "visual-design": [
        re.compile(r"(颜色|配色|底色|主色|视觉|审美)"),
        re.compile(r"(柱状图|折线图|饼图|示意图|信息图|大字报)"),
        re.compile(r"(对齐|间距|字号|字体|UI 设计|界面设计|布局)"),
        re.compile(r"(高亮|醒目|视觉.*差异|视觉.*区分)"),
        re.compile(r"(分层.*展示|分级.*显示|信息.*层级)"),
        re.compile(r"(脊梁话|Layer\s*[123]|杂志风|页面风格)"),
    ],
    "ai-collab-meta": [
        re.compile(r"(第二个我|孪生|代替我|替我做)"),
        re.compile(r"(锐化|完善我的|一起.{0,3}(想|探索|捋)|帮我.{0,3}(思考|想))"),
        re.compile(r"(扮演|拷问|[Cc]hallenger|审阅|挑剔)"),
        re.compile(r"(质疑一切|审视|自检)"),
        re.compile(r"(超出预期|超过.{0,3}预期)"),
    ],
    "interrupt-rollback": [
        re.compile(r"(撤回|回退|恢复|取消刚)"),
        re.compile(r"(等等|暂停|先停|停一下|先别)"),
        re.compile(r"(刚才.{0,5}(取消|撤|回))"),
    ],
    "knowledge-question": [
        re.compile(r"(是什么|什么意思|是啥|怎么回事)$"),
        re.compile(r"^(.{0,20}(是什么|是啥|什么意思))"),
        re.compile(r"(怎么.{1,5}(做|实现|用|看到|开|关))"),
        re.compile(r"^(.{0,30}怎么.{0,15}?)$"),
    ],
    "execute-short": [
        re.compile(r"^(好|嗯|对|行|继续|开始|全量|执行|提交|推送|可以|ok|OK)[\s\n。.，,]*$"),
        re.compile(r"^(好|嗯|对|行|继续|开始).{0,8}$"),
    ],
    "failure-mode": [
        re.compile(r"(报错|失败|错误|exception|[Ee]rror|不工作|崩了)"),
        re.compile(r"(行不通|没用|不起作用|无效|不生效)"),
        re.compile(r"(为啥.{0,5}(失败|没成|不行))"),
    ],
    "spec-elaboration": [
        re.compile(r"(我.{0,5}意思.{0,3}是|其实.{0,5}意思|我想说的|换句话说|更准确地说)"),
        re.compile(r"(要的是|想要的是|希望.{0,5}(达到|做到))"),
        re.compile(r"^.{0,3}(我希望|期望|预期)"),
        re.compile(r"(应该是.{0,15}才对|更.{0,3}倾向|更应该)"),
        re.compile(r"(对|不对)，?.{1,20}(是|要|应该)"),  # "对，应该是..."
    ],
    "delivery-finalize": [
        re.compile(r"(总结.{0,5}保存|保存.{0,5}md|更新.{0,5}(文档|readme))"),
        re.compile(r"(写成.{0,5}(md|markdown)|输出.{0,5}(md|文档))"),
        re.compile(r"(推送|push|提交并推|上传.{0,5}github|提交.{0,5}分支)"),
        re.compile(r"(完成.{0,3}收尾|整理.{0,3}完|sync)"),
    ],
    "scope-expansion": [
        re.compile(r"(把.{0,15}(也|都).{0,3}(加|纳入|做掉|搞掉))"),
        re.compile(r"(顺.{0,3}手.{0,3}(把|做))"),
        re.compile(r"(一起.{0,3}(做|搞))"),
    ],
    "choice-among-options": [
        re.compile(r"^.{0,10}用.{0,5}(第[一二三四五六七八]|[12345])(种|个|条|号|方案)"),
        re.compile(r"^(选|那就|那用|那就用|那么用).{0,15}(第|那个|这个)"),
        re.compile(r"^.{0,5}(选 ?[ABCDabcd]|方案 ?[ABCDabcd])"),
        re.compile(r"^那.{0,5}用.{0,15}(吧|了)"),
    ],
    "task-kickoff": [
        # 长任务下发：角色扮演 + 任务清单 + 长 spec
        re.compile(r"^你是.{0,15}(专家|审计员|工程师|开发者|顾问|分析师|reviewer)"),
        re.compile(r"^(请|帮我|帮忙).{0,15}(基于|对|从|分析|生成|创建|检查|审查)"),
        re.compile(r"^(I need you to|Please|Help me|Analyze)"),  # English long-form tasks
        re.compile(r"^.{0,10}(执行|运行).{1,30}(步骤|流程|阶段|workflow)"),
    ],
    "context-statement": [
        # 用户陈述前提/约束/事实（"权限是不允许开通的"、"这个机器人已经..."）
        re.compile(r"(是不允许|是不能|是必须|是.{0,5}规则|是.{0,5}约束)"),
        re.compile(r"^.{0,30}(已经|已).{0,5}(开通|配置|完成|做过|搞过)"),
        re.compile(r"^.{0,30}(其实是|本质上是|实际上是|真实的是)"),
        re.compile(r"(我.{0,3}(机器|机器人|账号|配置).{0,15}(是|不是|有|没有))"),
    ],
    "meta-self-reflection": [
        # 用户对自己/项目/方向的反思
        re.compile(r"(我.{0,3}知识体系|我.{0,3}研究|我.{0,3}方向|我.{0,3}思考)"),
        re.compile(r"(脉络|捋一遍|蒸馏|沉淀|总结自己)"),
        re.compile(r"(第二个我|我.{0,3}孪生|我.{0,5}合作伙伴)"),
        re.compile(r"(我.{0,5}缺点|我.{0,5}优点|盲区)"),
    ],
}


def classify_text(text: str) -> list[str]:
    labels: list[str] = []
    for label, patterns in LABEL_RULES.items():
        for pat in patterns:
            if pat.search(text):
                labels.append(label)
                break
    return labels


def classify_all(cfg: Config) -> tuple[Path, Path]:
    in_path = cfg.paths.data_dir / "turns" / "turns.jsonl"
    if not in_path.exists():
        raise FileNotFoundError(f"missing {in_path}; run `keep-going harvest` first")

    out_dir = cfg.paths.data_dir / "labels"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "labeled.jsonl"
    stats_path = out_dir / "stats.md"

    counts: Counter[str] = Counter()
    per_label_examples: dict[str, list[str]] = defaultdict(list)
    co_occurrence: Counter[tuple[str, str]] = Counter()
    total_user = 0
    with_labels = 0
    no_label_examples: list[str] = []

    with in_path.open("r", encoding="utf-8") as inf, out_path.open("w", encoding="utf-8") as outf:
        for line in inf:
            try:
                obj: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("role") != "user":
                continue
            total_user += 1
            labels = classify_text(obj.get("content", ""))
            obj["labels"] = labels
            outf.write(json.dumps(obj, ensure_ascii=False) + "\n")

            if labels:
                with_labels += 1
                for lab in labels:
                    counts[lab] += 1
                    if len(per_label_examples[lab]) < 3:
                        snippet = obj["content"][:140].replace("\n", " ")
                        per_label_examples[lab].append(snippet)
                for i in range(len(labels)):
                    for j in range(i + 1, len(labels)):
                        a, b = sorted([labels[i], labels[j]])
                        co_occurrence[(a, b)] += 1
            else:
                if len(no_label_examples) < 10:
                    snippet = obj["content"][:140].replace("\n", " ")
                    no_label_examples.append(snippet)

    # Write stats markdown
    lines: list[str] = []
    lines.append("# Classify · 标签统计")
    lines.append("")
    lines.append(f"- total user turns: **{total_user}**")
    pct = 100 * with_labels / total_user if total_user else 0
    lines.append(f"- with ≥1 label: **{with_labels}** ({pct:.1f}%)")
    lines.append(f"- 0 labels: {total_user - with_labels}")
    lines.append("")
    lines.append("## 标签命中分布")
    lines.append("")
    lines.append("| 标签 | 命中 | 占比 | 示例 |")
    lines.append("|---|---:|---:|---|")
    for lab, n in counts.most_common():
        examples = per_label_examples[lab]
        ex_str = " · ".join(f"`{e[:80]}`" for e in examples)
        lines.append(f"| {lab} | {n} | {100*n/total_user:.1f}% | {ex_str} |")
    lines.append("")
    lines.append("## 高频共现标签对 (top 20)")
    lines.append("")
    lines.append("| 标签 A | 标签 B | 共现次数 |")
    lines.append("|---|---|---:|")
    for (a, b), n in co_occurrence.most_common(20):
        lines.append(f"| {a} | {b} | {n} |")
    lines.append("")
    lines.append("## 未被任何规则捕获的样本（前 10 条）")
    lines.append("")
    lines.append("> 这些是规则盲区，提示需要扩展 LABEL_RULES。")
    lines.append("")
    for s in no_label_examples:
        lines.append(f"- `{s}`")
    stats_path.write_text("\n".join(lines), encoding="utf-8")

    console.print(
        f"[green]classify done[/green] {total_user} user turns / "
        f"{with_labels} labeled ({pct:.1f}%) / {len(counts)} labels in play"
    )
    console.print(f"  → {out_path}")
    console.print(f"  → {stats_path}")
    return out_path, stats_path
