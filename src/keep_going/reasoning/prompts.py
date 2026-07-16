"""Prompt templates for reasoning extraction."""

from __future__ import annotations

SYSTEM_PROMPT = """你是一个对话研究员。你的任务是阅读「AI 工具刚刚说的话」+「用户的回复」，反推用户这条回复背后的真实动机。

只输出 JSON，不要任何额外解释。Schema:
{
  "intent": "follow-up | correction | rejection | choice | confirmation | new-request | interrupt | clarification | praise | meta",
  "decision_type": "scope | approach | tool | priority | quality-bar | style | risk | none",
  "why_short": "<= 30 字的中文一句话，解释动机",
  "criteria": ["用户在意的隐性判据，每条 <= 20 字"],
  "tone": "neutral | impatient | satisfied | firm | exploratory",
  "stance_to_ai": "accept | partial-accept | redirect | reject | ignore",
  "is_high_signal": true | false
}

判据说明：
- intent：用户这条话在对话流里的作用
- decision_type：这条回复主要是在做哪类决策（如无决策就 "none"）
- why_short：必须是中文，提炼真实意图，不要复述用户原话
- criteria：用户隐性看重的东西（如可验证性、最小变更、可读性、可回滚、闭环）；可以为空数组
- is_high_signal：这条对未来"模仿用户"有价值就 true；闲聊、纯命令、纯转述就 false
"""

USER_PROMPT_TEMPLATE = """[项目路径] {project}
[时间] {ts}

[AI 上一条说]
{prev_assistant}

[用户回复]
{user_content}

请输出 JSON。"""


def render_user_prompt(*, project: str, ts: str, prev_assistant: str, user_content: str) -> str:
    return USER_PROMPT_TEMPLATE.format(
        project=project or "(unknown)",
        ts=ts,
        prev_assistant=prev_assistant or "(无前置 AI 消息 / 首轮)",
        user_content=user_content,
    )
