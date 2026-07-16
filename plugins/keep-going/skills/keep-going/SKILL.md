---
name: keep-going
description: 调用本仓库的 Keep Going；也可通过“开启/关闭/查询/自检”管理当前项目的 Keep Going Stop hook。
---

# Keep Going Skill

当任务 Agent 卡在「要不要继续、选哪个方案、是否完成、是否提交」这类需要用户轻量判断的问题时，调用本 skill。

当用户通过 `$keep-going` 请求“开启、关闭、查询、状态、自检、start、enable、disable、status、self-test”时，不要把它当成要询问 Keep Going 的问题；直接执行下面的项目级控制命令。

## 项目级控制入口

默认目标项目是触发 `$keep-going` 时所在的工作目录。

意图映射：

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

## Distill your own agent [experimental]

One-click distillation: harvest → classify → deterministic decision policy for a named agent.

```bash
uv run keep-going distill-mine --name <agent-name>
uv run keep-going distill-mine --name <agent-name> --project /path/to/project
```

**隐私边界（hard requirement）：**

1. 本地 session 只读 — harvest 只读 `~/.claude` 和 `~/.codex`，不写源目录。
2. 模板 / canonical policy 不被触碰。
3. 全流程本地处理，不调 Anthropic API。
4. 产物写到 `~/.keep-going/agents/<name>/policy-<ts>.yaml`，不进 git 跟踪路径。
5. 无 telemetry、无上传。

**约束：**

- agent 名称必须通过 `validate_agent_name`（`default` 等保留名被拒）。
- 语料不足 10 条 user turn 时抛 `InsufficientCorpusError`。
- 产物是 `status: candidate` 的 YAML，不自动覆盖 canonical policy。
- 连续运行产生带历史记录的多个 decision policy 文件（meta.json.history 追加）。

## 边界

- 不做 commit / push / 删除 / 生产操作授权。
- 不伪造验证；没有证据时要求任务 Agent 补验证。
- Stop hook 默认会调用宿主 CLI 对应的模型后端；普通 `keep-going reply` / MCP 仍只有显式传 `--generate` / `generate=true` 才会调用 Anthropic API。
