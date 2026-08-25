# Keep Going

[English](README.md) | 简体中文

**让长任务持续推进，在真正重要时再交还给人。**

Keep Going 是面向 Claude Code 和 Codex 的策略驱动型 Stop-hook harness。每次 Agent 准备停止时，它会结合有界的 session 上下文和用户拥有的决策策略，选择注入一条低风险的继续指令，或将控制权交还给人。

![概念时序图：AI 工作、人介入、AI 恢复工作、人再次介入；使用 Keep Going 后，AI 可以跨越低风险 Stop 检查持续工作](docs/assets/keep-going-concept.svg)

> 这是概念交互模型，不是实测性能数据。图中的横轴表示标准化任务进度，不表示实际耗时。Keep Going 不声称具体减少了多少次人类介入，也不声称缩短了完成时间或提高了成功率。

## 一条命令获得你的个人 DNA

环境要求：通过 `npx` 使用时需要 Node.js 18+；两种入口都需要 Python 3.11+、[`uv`](https://docs.astral.sh/uv/)，以及已登录的 Claude Code 或 Codex CLI。npm wrapper 会用 `uv` 运行随包提供的 Python runtime。

从源码仓库使用：

```bash
uv sync
uv run keep-going onboard --project "$PWD" --host auto
```

或者使用已打包的 CLI：

```bash
npx keep-going onboard --project "$PWD" --host auto
```

这一条命令只会从所选宿主自己的近期 session 中选择有界的决策样本，先做脱敏，再通过该已登录宿主 CLI 蒸馏稳定的个人决策偏好；随后把可 review 的 canonical 与编译后 runtime 持久化到不随程序版本变化的本机用户目录，安装调用面、启用当前项目并运行 Stop-hook 自检。

完成输出会直接展示你的画像摘要、采用了多少个 session 和决策、全部本地产物路径、部署状态，以及一个可以马上体验的问题。之后可以运行 `$keep-going 状态` 或 `npx keep-going status --project "$PWD"` 查看当前真正加载的策略。

只有所选宿主自身 session 中经过选择和脱敏的有界样本会发送给该后端；不会读取另一个宿主的 session。原始 session 始终只读并保留在本机。已有个人 DNA 默认不会被覆盖，只有明确追加 `--replace` 才会重新蒸馏。

## Keep Going 做什么

Keep Going 位于宿主的 Stop hook。它不接管 Agent 的规划器、任务状态、检查点、权限系统或完成标准。

每次发生 Stop 事件时，它会：

1. 从当前 session transcript 构建有界的决策上下文。
2. 将选定上下文和用户拥有的决策策略发送给已配置的 Claude 或 Codex 模型后端。
3. 将模型响应规范化为 `allow`、`block` 或 `escalate`。
4. 执行模型无法绕过的确定性安全门。
5. 仅在决策低风险且证据充分时注入继续指令；否则将控制权交还给人。

```text
Stop 事件
  -> 有界 session 上下文 + 决策策略
  -> 已配置的模型后端
  -> 确定性安全门
     -> 低风险继续：注入回复
     -> 不确定或敏感操作：交还给人
```

## 确定性安全边界

安全门运行在模型输出之后、回复到达宿主 Agent 之前。遇到以下情况时，它会阻止自动继续：

- 决策类别是 `authorization` 或 `information`；
- session 上下文包含高风险或不可逆操作标记；
- `block` 决策的置信度低于 `0.6`；
- transcript 缺失或无法读取；
- 模型输出格式错误、相互矛盾，或继续指令为空；
- 已达到配置的连续继续深度上限。

Keep Going 不会绕过 Claude Code 或 Codex 的权限机制。宿主的权限提示和平台控制始终拥有最终决定权。

## 隐私模型

仓库只跟踪公开策略模板：[`artifacts/decision-policy.template.yaml`](artifacts/decision-policy.template.yaml)。以下本地文件会被 Git 忽略；一旦进入 Git，也会被隐私门拒绝：

- `data/` 和采集到的 session 内容；
- `artifacts/decision-policy.yaml`；
- `artifacts/decision-policy.runtime.yaml`；
- 候选策略、日志、本地配置和未经审定的媒体文件。

策略、事件和编译后的 runtime 产物会持久化保存在本机，便于人工 review。但每次进行 Stop 决策时，已配置的模型后端仍会收到选定策略和有界上下文。不要在策略中保存密钥；启用 hook 前，请先确认所用模型后端的数据处理规则。

runtime 策略是本地 canonical 策略经过确定性编译后的持久化产物。runtime 缺失、过期或被人工修改时会显式失败；Keep Going 不会临时投影，也不会静默回退。

## 手工配置与高级控制

要求：Python 3.11+、[`uv`](https://docs.astral.sh/uv/)，以及已完成身份认证的 Claude Code 或 Codex CLI，用于模型驱动的 Stop 决策。

```bash
uv sync
uv run keep-going onboard --project "$PWD" --host auto
```

安装或刷新宿主集成，启用项目级 Stop hook，并验证实际加载面：

```bash
uv run keep-going start --project "$PWD" --host codex
uv run keep-going bridge status --project "$PWD" --json-output
uv run keep-going bridge self-test --project "$PWD" --json-output
```

`keep-going start` 会写入用户级 agent、plugin、marketplace、原生 hook 集成和项目状态。在源码工作流中，当前 checkout 仍是实际运行时。如果暂时不希望执行这些用户级写入，请先运行 `uv run keep-going install` 查看安装计划。

如果要研究 deterministic baseline 或手工开发策略，仍可使用底层流水线：

```bash
uv run keep-going harvest --window-days 90
uv run keep-going classify
uv run keep-going sample-themes
uv run keep-going distill --out artifacts/decision-policy.candidate.yaml
# 人工 review 候选策略后，再更新本地 canonical 策略。
uv run keep-going compile-policy
```

## Review 与验证

```bash
uv run pytest -q
uv run keep-going audit --smoke --json-output
uv run python scripts/privacy-audit.py --history
npm --prefix packages/npm test
```

如需手工模拟 Stop 事件且不写入 metrics，请给 `keep-going bridge stop-hook --input-json` 传入 `--synthetic --json-output`，并在事件中提供宿主实际生成的 transcript 路径。

隐私审计会拒绝私有策略产物、session 或日志格式、真实用户主目录路径、非占位邮箱、密钥、压缩包，以及不在精确审定清单中的媒体文件。

## 集成入口

- `keep-going bridge`：启用项目级 Stop hook 并执行自检
- `keep-going onboard`：有界 session 蒸馏、个人 DNA 持久化、本地部署与验证
- `keep-going reply`：直接使用决策策略回答问题
- `keep-going hook`：宿主无关的 hook 策略入口
- `keep-going mcp`：MCP stdio server
- `keep-going eval`、`conformance`、`overrides`：质量反馈闭环
- `keep-going agent`：管理具名策略 Agent
- `keep-going install`、`sync-local`、`audit`：管理本机安装面的生命周期

开发边界和验证矩阵见 [`AGENTS.md`](AGENTS.md)。

## 许可证

本项目未授予任何许可证。仓库源码公开仅用于审阅；复用、修改和再分发均需获得版权所有者许可。
