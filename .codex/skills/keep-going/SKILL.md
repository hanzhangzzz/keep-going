---
name: keep-going
description: 一键蒸馏并部署个人 DNA，调用 Keep Going 做轻量决策，或管理当前项目 Stop hook。
---

# Keep Going Skill

任务 Agent 卡在「要不要继续、选哪个方案、是否完成、是否提交」这类轻量判断时，调用本 skill。

当用户通过 `$keep-going` 请求“蒸馏我的 DNA、初始化、onboard、开启、关闭、查询、状态、自检”时，不要把它当成要询问 Keep Going 的问题；直接执行对应控制命令。

## 执行总流程

1. 保存触发目录：

   ```bash
   TASK_PROJECT="$PWD"
   KEEP_GOING_REPO="${KEEP_GOING_REPO:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
   ```

2. 判断用户意图：
   - 命中 `$keep-going 蒸馏我的 DNA/初始化/onboard`：执行 `plugins/keep-going/scripts/onboard.sh --project "$TASK_PROJECT" --host codex`；当前宿主是 Claude Code 时改用 `--host claude-code`。
   - 命中 `$keep-going 重新蒸馏/refresh DNA`：在上一条命令追加 `--replace`，并先提醒会替换现有本地 canonical。
   - 命中 `$keep-going 开启/关闭/查询/状态/自检/start/enable/disable/status/self-test`：进入“项目级控制入口”。
   - 否则如果是在问任务 Agent 的轻量决策问题：进入“普通 Keep Going 询问入口”。
   - 否则停止使用本 skill，并按用户原任务继续。

3. 切换到 Keep Going repo 后执行命令：

   ```bash
   cd "$KEEP_GOING_REPO"
   ```

4. 将命令输出转述给用户或上游 Agent。输出必须包含执行的入口、结果状态和下一步动作；不要编造 Keep Going 没有返回的结论。

🔴 CHECKPOINT / STOP：

- 只有用户明确要求外部模型生成最终回复时，才允许追加 `--generate`。否则保持本地检索 / prompt 包输出。
- Keep Going 返回 `escalate=true` 或 Stop hook 返回 `action=escalate` 时，停止代用户授权，转人工确认。
- 涉及 commit、push、删除、生产操作、付费 API 或敏感数据发送时，Keep Going 不能代替用户授权。

## 项目级控制入口

默认目标项目是触发 `$keep-going` 时所在的工作目录。必须先保存 `TASK_PROJECT="$PWD"`，再切换到 Keep Going repo。

```bash
KEEP_GOING_REPO="${KEEP_GOING_REPO:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
TASK_PROJECT="$PWD"
cd "$KEEP_GOING_REPO"
```

意图映射：

- `$keep-going 开启` / `$keep-going start`：执行 `uv run keep-going start --project "$TASK_PROJECT"`
- `$keep-going enable`：执行 `uv run keep-going bridge enable --project "$TASK_PROJECT" --host codex`
- `$keep-going 关闭` / `$keep-going disable`：执行 `uv run keep-going bridge disable --project "$TASK_PROJECT"`
- `$keep-going 查询` / `$keep-going 状态` / `$keep-going status`：执行 `uv run keep-going bridge status --project "$TASK_PROJECT"`
- `$keep-going 自检` / `$keep-going self-test`：执行 `uv run keep-going bridge self-test --project "$TASK_PROJECT"`
- `$keep-going 概览` / `$keep-going 看 agent` / `$keep-going agents` / `$keep-going 用哪个 decision policy`：执行 `uv run keep-going status --project "$TASK_PROJECT"`，把「你本人的 decision policy 健康度 + 命名 agents + 当前项目绑定」一次性转述给用户

默认开启后会经由宿主对应的本机 CLI 做 Stop decision 决策：`--host codex` 默认调用 `codex exec`，`--host claude-code` 默认调用 `claude -p`。需要改用本机 alias 或 wrapper（如 `c 0`、`omxm`）时，在开启命令后追加用户给出的 backend 参数：

```bash
uv run keep-going bridge enable \
  --project "$TASK_PROJECT" \
  --host codex \
  --backend cli \
  --command "omxm" \
  --shell
```

控制命令执行后，直接把 bridge 输出转述给用户；不要额外调用 Keep Going 生成回复。

## 普通 Keep Going 询问入口

沿用“执行总流程”里保存的 `TASK_PROJECT` 和 `KEEP_GOING_REPO`，在 Keep Going repo 根目录调用：

```bash
cd "$KEEP_GOING_REPO"
scripts/03-reply.sh \
  --question "<AI 的问题>" \
  --project "$TASK_PROJECT" \
  --reply-only
```

需要给下游 Agent 完整上下文包时去掉 `--reply-only`，输出 JSON：

```bash
cd "$KEEP_GOING_REPO"
scripts/03-reply.sh \
  --question "<AI 的问题>" \
  --project "$TASK_PROJECT" \
  --recent-context /path/to/context.md
```

如需让 Keep Going 直接调用 Claude 生成最终回复，且用户已授权外部 API，追加 `--generate`。

hook / agent 更适合使用 stdin JSON：

```bash
cd "$KEEP_GOING_REPO"
printf '{"question":"<AI 的问题>","project":"%s"}' "$TASK_PROJECT" | scripts/03-reply.sh --input-json
```

hook 事件策略入口会先过滤非决策事件；高风险工具操作会返回 `escalate=true`：

```bash
cd "$KEEP_GOING_REPO"
printf '{"question":"<AI 的问题>","project":"%s"}' "$TASK_PROJECT" | uv run keep-going hook --input-json
```

Stop hook 桥接支持按项目开启 / 关闭。默认会真实执行本机 `codex` / `claude` CLI，输出 `{action, reply, reason, confidence, evidence, category}`。如需经由 alias 或 wrapper 执行，使用 `--backend cli --command ... --shell`：

```bash
cd "$KEEP_GOING_REPO"
uv run keep-going bridge enable --project "$TASK_PROJECT" --host codex
uv run keep-going bridge status --project "$TASK_PROJECT"
printf '{"cwd":"%s","last_assistant_message":"要不要继续最终验证？"}' "$TASK_PROJECT" | uv run keep-going bridge stop-hook --input-json --synthetic --json-output
uv run keep-going bridge disable --project "$TASK_PROJECT"
uv run keep-going bridge self-test --project "$TASK_PROJECT"
```

MCP 客户端使用 stdio server：

```bash
cd "$KEEP_GOING_REPO"
scripts/04-mcp.sh
```

当前 MCP tools：

- `keep_going_reply`（可选 `generate=true`，会调用 Anthropic API）
- `keep_going_eval`（可选 `generate=true`，会调用 Anthropic API）

## 查看 / 启动特定 agent

**查看当前有哪些 decision policy、当前项目用的是哪个**（只读，一条命令）：

```bash
cd "$KEEP_GOING_REPO"
uv run keep-going status --project "$TASK_PROJECT"          # 人类视图
uv run keep-going status --project "$TASK_PROJECT" --json-output
uv run keep-going agent list                                # 只看命名 agent 列表
```

`status` 输出三段：你本人的 decision policy（`default` / myself）健康度、命名 agents、当前项目绑定哪个 decision policy。canonical policy 被占位或被覆盖时标 `⚠️`。

默认 Keep Going 的完整事实源是本地私有、Git ignored 的 `artifacts/decision-policy.yaml`，实际加载的是同样本地私有、可直接 review 的持久化 `artifacts/decision-policy.runtime.yaml`；公开仓库和发布包只包含 `decision-policy.template.yaml`。修改 canonical 后先运行 `uv run keep-going compile-policy`；runtime 缺失、过期或被手改时禁止静默回退。

**启动特定 agent 来回复**——`--agent <name>`；**不指定就是你本人的 canonical policy（`default` / myself）**：

```bash
cd "$KEEP_GOING_REPO"
# 你本人的 decision policy（默认）
scripts/03-reply.sh --question "<AI 的问题>" --project "$TASK_PROJECT" --reply-only
# 某个命名 agent 的 decision policy
uv run keep-going reply -q "<AI 的问题>" --agent qa-reviewer --project "$TASK_PROJECT" --reply-only
# stdin JSON 也支持 agent 字段
printf '{"question":"<AI 的问题>","project":"%s","agent":"qa-reviewer"}' "$TASK_PROJECT" | uv run keep-going reply --input-json --reply-only
```

优先级：显式 `--policy-path` > `--agent` > canonical（myself）。`--agent` 指向不存在的 agent 时报错并提示 `keep-going agent list`。

## 失败模式处理

| 触发条件 | 处理动作 | 输出要求 |
|---|---|---|
| `$KEEP_GOING_REPO` 不存在 | 停止执行，提示本机 Keep Going repo 路径不可用 | 给出检查过的路径 |
| `uv run keep-going ...` 失败 | 不切换到其它未确认入口；转述 stderr/stdout 关键行 | 标明命令失败，要求用户或任务 Agent 修复环境 |
| 用户输入是控制词 | 只跑 bridge/start 控制命令 | 不调用 `scripts/03-reply.sh` |
| 用户输入是决策问题 | 只跑 `scripts/03-reply.sh` 或 `uv run keep-going hook` | 不启用/禁用 bridge |
| `--generate` 缺少 API key 或用户未授权 | 不追加 `--generate` | 返回本地检索结果或 prompt 包 |
| Stop hook 手工 probe | 使用 `--synthetic` 或 `--no-metrics` | 避免污染 `~/.keep-going/events/stop-hook.jsonl` |

## 输出语义

- `reply`：可直接转发给任务 Agent 的用户口吻回复。
- `confidence`：Keep Going 对当前回复或裁决的置信度。
- Stop hook 裁决统一输出 `{action, reply, reason, confidence, evidence, category}`；`category` 是五类分诊结果（preference/verification/authorization/capability/information/other），落入事件日志供 `keep-going overrides` 按类目统计推翻率。`action=block` 时 bridge 才会把 `reply` 注入上游 agent，`action=escalate` 时转人工确认。
- `escalate`：仅适用于 `keep-going reply` / `keep-going hook` 旧接口；为 `true` 时不要代用户继续授权。
- `prompt`：可交给更强 LLM 继续生成最终回复的 Keep Going system/context 包。

## 反例黑名单

- 不要把 `$keep-going 查询/状态/自检/开启/关闭` 交给 Keep Going 回答；这些是控制命令。
- 不要在切换到 Keep Going repo 后用新的 `$PWD` 覆盖 `TASK_PROJECT`。
- 不要把 Keep Going 回复当作 commit / push / 删除 / 生产操作授权。
- 不要伪造验证、状态、置信度、证据或 hook 裁决。
- 不要在用户未明确授权时追加 `--generate` 或发送敏感上下文给外部 API。
- 不要在 Stop hook probe 中写入真实 metrics，除非用户正在验证真实运行态。

## 一键蒸馏并部署你的个人 DNA

默认只从当前宿主最近 5 个本地 session 中选择最多 40 条高信号决策，经脱敏后交给同一已认证宿主 CLI 蒸馏；随后确定性编译 runtime、安装调用面、绑定当前项目并运行自检。

```bash
plugins/keep-going/scripts/onboard.sh --project "$TASK_PROJECT" --host codex
```

**隐私边界（hard requirement）：**

1. 原始 session 只读；只读取当前宿主自己的 session，并把脱敏、有界的决策样本发送给同一宿主 CLI。
2. canonical、runtime 和证据包只保存在本机私有、Git ignored 路径。
3. 公开 baseline 的授权、隐私和不可逆操作门必须强制合并，模型不能删除。
4. 现有个人 DNA 默认拒绝覆盖；明确重新蒸馏才追加 `--replace`。

**约束：**

- 少于 3 条决策时显式失败并给出扩大时间窗的恢复方法。
- 完成后必须报告 profile、样本数量、产物路径、部署/自检状态和立即体验方法。
- 命名专家 agent 仍可用 `keep-going distill-mine --name <agent-name>`，不替代默认 personal DNA onboarding。

## 边界

- 不做 commit / push / 删除 / 生产操作授权。
- 不伪造验证；没有证据时要求任务 Agent 补验证。
- Stop hook 默认会调用宿主 CLI 对应的模型后端；普通 `keep-going reply` / MCP 仍只有显式传 `--generate` / `generate=true` 才会调用 Anthropic API。
