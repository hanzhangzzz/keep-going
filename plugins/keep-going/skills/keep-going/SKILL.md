---
name: keep-going
description: 一键蒸馏并部署个人 DNA，调用 Keep Going 做轻量决策，或管理当前项目的 Stop hook。
---

# Keep Going Skill

当任务 Agent 卡在「要不要继续、选哪个方案、是否完成、是否提交」这类需要用户轻量判断的问题时，调用本 skill。

当用户通过 `$keep-going` 请求“蒸馏我的 DNA、初始化、onboard、开启、关闭、查询、状态、自检”时，不要把它当成要询问 Keep Going 的问题；直接执行对应控制命令。

## 项目级控制入口

默认目标项目是触发 `$keep-going` 时所在的工作目录。

意图映射：

- `$keep-going 蒸馏我的 DNA` / `$keep-going 初始化` / `$keep-going onboard`：执行插件本地 `scripts/onboard.sh --project "$PWD" --host <当前宿主>`。Codex 使用 `--host codex`，Claude Code 使用 `--host claude-code`。这一个入口必须完成 session 选择、个人策略蒸馏、runtime 编译、本地安装、项目绑定和自检。
- `$keep-going 重新蒸馏` / `$keep-going refresh DNA`：同上并追加 `--replace`；执行前提醒现有本地 canonical 会被替换。
- `$keep-going 开启` / `$keep-going start`：优先执行 npm/CLI 一键入口 `keep-going start --project "$PWD"`；如果当前环境没有 `keep-going` 命令，再执行 `scripts/bridge.sh enable --project "$PWD" --host codex` 并提醒用户 native Stop hook 可能需要先安装。
- `$keep-going enable`：执行 `scripts/bridge.sh enable --project "$PWD" --host codex`
- `$keep-going 关闭` / `$keep-going disable`：执行 `scripts/bridge.sh disable --project "$PWD"`
- `$keep-going 查询` / `$keep-going 状态` / `$keep-going status`：执行 `scripts/bridge.sh status --project "$PWD"`
- `$keep-going 自检` / `$keep-going self-test`：执行 `scripts/bridge.sh self-test --project "$PWD"`
- `$keep-going 概览` / `$keep-going 看 agent` / `$keep-going agents` / `$keep-going 用哪个 decision policy`：执行 `uv run keep-going status --project "$PWD"`，把「你本人的 decision policy 健康度 + 命名 agents + 当前项目绑定」一次性转述给用户

默认开启后会经由宿主对应的本机 CLI 做 Stop decision 决策：`--host codex` 默认调用 `codex exec`，`--host claude-code` 默认调用 `claude -p`。需要改用本机 alias 或 wrapper（如 `c 0`、`omxm`）时，在开启命令后追加用户给出的 backend 参数：

```bash
scripts/bridge.sh enable \
  --project "$PWD" \
  --host codex \
  --backend cli \
  --command "omxm" \
  --shell
```

控制命令执行后，直接把 bridge 输出转述给用户；不要额外调用 Keep Going 生成回复。

## 调用方式

在本仓库根目录调用：

```bash
scripts/reply.sh \
  --question "<AI 的问题>" \
  --project "$PWD" \
  --reply-only
```

需要给下游 Agent 完整上下文包时去掉 `--reply-only`，输出 JSON：

```bash
scripts/reply.sh \
  --question "<AI 的问题>" \
  --project "$PWD" \
  --recent-context /path/to/context.md
```

如需让 Keep Going 直接调用 Claude 生成最终回复，且环境中已有 `ANTHROPIC_API_KEY`，追加 `--generate`。

hook / agent 更适合使用 stdin JSON：

```bash
printf '{"question":"<AI 的问题>","project":"%s"}' "$PWD" | scripts/reply.sh --input-json
```

hook 事件策略入口由插件 hook wrapper 调用：非决策事件会 no-op；高风险工具操作会返回 `escalate=true`。

Stop hook 桥接支持按项目开启 / 关闭。默认会真实执行本机 `codex` / `claude` CLI，让模型以 Keep Going Stop decision 和内联 decision policy 的角色输出 `{action, reply, reason, confidence, evidence, category}`。如果要经由本机 alias 或 wrapper（如 `c 0`、`omxm`）执行，可使用 `--backend cli --command ... --shell`：

```bash
scripts/bridge.sh enable --project "$PWD"
scripts/bridge.sh status --project "$PWD"
printf '{"cwd":"%s","last_assistant_message":"要不要继续最终验证？"}' "$PWD" | scripts/bridge.sh stop-hook --input-json --json-output
scripts/bridge.sh disable --project "$PWD"
scripts/bridge.sh self-test --project "$PWD"
```

MCP 客户端使用 stdio server：

```bash
scripts/mcp.sh
```

当前 MCP tools：

- `keep_going_reply`（可选 `generate=true`，会调用 Anthropic API）
- `keep_going_eval`（可选 `generate=true`，会调用 Anthropic API）

## 查看 / 启动特定 agent

**查看当前有哪些 decision policy、当前项目用的是哪个**（一条命令，只读）：

```bash
uv run keep-going status --project "$PWD"          # 人类视图
uv run keep-going status --project "$PWD" --json-output  # 机器视图
uv run keep-going agent list                        # 只看命名 agent 列表
```

`status` 输出三段：你本人的 decision policy（`default` / myself）健康度、命名 agents、当前项目绑定哪个 decision policy。canonical policy 被占位或被覆盖时会标 `⚠️`。

默认 Keep Going 的完整事实源是本地私有、Git ignored 的 `artifacts/decision-policy.yaml`，实际加载的是同样本地私有、可直接 review 的持久化 `artifacts/decision-policy.runtime.yaml`；公开仓库和发布包只包含 `decision-policy.template.yaml`。修改 canonical 后先运行 `uv run keep-going compile-policy`；runtime 缺失、过期或被手改时禁止静默回退。

**启动特定 agent 来回复**——用 `--agent <name>`；**不指定就是你本人的 canonical policy（`default` / myself）**：

```bash
# 用你本人的 decision policy（默认，等价于不传 --agent）
scripts/reply.sh --question "<AI 的问题>" --project "$PWD" --reply-only

# 用某个命名 agent 的 decision policy
uv run keep-going reply -q "<AI 的问题>" --agent qa-reviewer --project "$PWD" --reply-only
```

stdin JSON 也支持 `agent` 字段：

```bash
printf '{"question":"<AI 的问题>","project":"%s","agent":"qa-reviewer"}' "$PWD" | uv run keep-going reply --input-json --reply-only
```

优先级：显式 `--policy-path` > `--agent` > canonical（myself）。`--agent` 指向的 agent 不存在时直接报错并提示 `keep-going agent list`。

## 输出语义

- `reply`：可直接转发给任务 Agent 的用户口吻回复。
- `confidence`：Keep Going 对当前回复或裁决的置信度。
- Stop hook 裁决统一输出 `{action, reply, reason, confidence, evidence, category}`；`category` 是五类分诊结果（preference/verification/authorization/capability/information/other），落入事件日志供 `keep-going overrides` 按类目统计推翻率。`action=block` 时 bridge 才会把 `reply` 注入上游 agent，`action=escalate` 时转人工确认。
- `escalate`：仅适用于 `keep-going reply` / `keep-going hook` 旧接口；为 `true` 时不要代用户继续授权。
- `prompt`：可交给更强 LLM 继续生成最终回复的 Keep Going system/context 包。

## 一键蒸馏并部署你的个人 DNA

用户不需要理解内部流水线。默认只从当前宿主最近 5 个本地 session 中选择最多 40 条高信号决策，脱敏后交给同一已登录宿主 CLI 蒸馏，并把完整事实源与持久化 runtime 保存在版本无关的本机用户目录。

```bash
scripts/onboard.sh --project "$PWD" --host codex
# Claude Code 中使用：
scripts/onboard.sh --project "$PWD" --host claude-code
```

**隐私边界（hard requirement）：**

1. 原始 session 只读，不修改 `~/.claude` 和 `~/.codex`。
2. 只读取当前宿主自己的 session；经过脱敏和有界选择的决策样本只发送给同一已认证宿主 CLI，不走 Keep Going 自建服务，无 telemetry。
3. 完整 canonical、runtime 和证据包都保存在本机私有、Git ignored 路径，不进入发布包。
4. 安全、授权和隐私门由公开 baseline 强制合并，模型输出不能删除。
5. 已存在个人 DNA 时默认拒绝覆盖；只有用户明确要求重新蒸馏才使用 `--replace`。

**约束：**

- 少于 3 条可用决策时显式失败，并提示扩大时间窗或先积累 session。
- 默认 `--scope recent` 跨项目提炼稳定个人偏好；只想使用当前项目时传 `--scope project`。
- 完成后必须把 profile 摘要、session/turn 数、canonical/runtime/证据包路径、部署状态和可立即体验的下一步原样告诉用户。

命名专家 agent 仍可使用高级入口 `keep-going distill-mine --name <agent-name>`；它不替代默认 personal DNA onboarding。

## 边界

- 不做 commit / push / 删除 / 生产操作授权。
- 不伪造验证；没有证据时要求任务 Agent 补验证。
- Stop hook 默认会调用宿主 CLI 对应的模型后端；普通 `keep-going reply` / MCP 仍只有显式传 `--generate` / `generate=true` 才会调用 Anthropic API。
