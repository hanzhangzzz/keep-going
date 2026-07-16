# Keep Going 开发指南

## 项目目标

Keep Going 把用户本地 agent 对话蒸馏为决策 decision policy，并通过 CLI、skill、plugin、MCP 和 Stop hook 处理低风险执行期判断。项目使用 Python 3.11 与 uv。

## 工作边界

- 始终先读后写，以文件、命令和真实返回为证据。
- 只改当前任务范围，不顺手重构。
- 未经明确授权，不执行提交、推送、历史重写或不可逆删除。
- 错误必须显式暴露，不截断、不吞异常、不伪造完成。
- 产物问题优先修生成器、打包器或 prompt，不手改生成产物治标。
- 修改后必须按风险运行最终交付面验证；无法验证时标记为未完成。

## 隐私边界

本仓库的公开事实源只有 `artifacts/decision-policy.template.yaml`。以下文件均为本地私有运行数据，禁止跟踪、提交、打包或上传：

- `data/**`
- `artifacts/decision-policy.yaml`
- `artifacts/decision-policy.runtime.yaml`
- `artifacts/decision-policy.candidate*.yaml`
- 原始 session、日志、调试输出和本地配置
- 图片、音视频、Office/PDF 和归档文件，除非先通过独立人工隐私审查

发布物必须使用白名单，不得用“复制整个仓库再排除少数目录”的黑名单策略。提交前执行：

```bash
uv run python scripts/privacy-audit.py
uv run python scripts/privacy-audit.py --history
```

如果隐私审计失败，必须修复根因；禁止跳过门禁。

## decision policy 合同

- `artifacts/decision-policy.yaml`：本地 canonical，包含完整证据链。
- `artifacts/decision-policy.runtime.yaml`：确定性编译、持久化、可 diff 的实际加载内容。
- `artifacts/decision-policy.template.yaml`：唯一允许进入 Git 和公开发布物的 decision policy 文件。
- 运行入口只读取 runtime；runtime 缺失、过期或被修改时显式失败。

本地初始化与编译：

```bash
cp artifacts/decision-policy.template.yaml artifacts/decision-policy.yaml
uv run keep-going compile-policy
```

## 主要目录

```text
src/keep_going/
  corpus/          采集、适配、脱敏、分类、采样
  patterns/        deterministic candidate distill
  decision/        reply、hook、Stop decision、runtime policy
  eval/            replay、conformance、overrides
  integration/     install、package、bridge
artifacts/         公开模板 + ignored 私有 decision policy
packages/npm/      npm wrapper 与 runtime 白名单复制
plugins/keep-going/   repo-local plugin
tests/             单元、集成和隐私边界测试
```

## 常用命令

```bash
uv run keep-going harvest --window-days 90
uv run keep-going classify
uv run keep-going sample-themes
uv run keep-going distill --out artifacts/decision-policy.candidate.yaml
uv run keep-going compile-policy
uv run keep-going reply -q "要不要继续下一步？" --project "$PWD"
uv run keep-going conformance --json-output
uv run keep-going eval --holdout-ratio 0.1 --limit 30
uv run keep-going overrides --json-output
uv run keep-going audit --smoke --json-output
uv run keep-going package --out /tmp/keep-going-package
uv run keep-going install --verify
```

Stop hook 手工 probe 默认使用 synthetic，避免写真实 metrics：

```bash
printf '{"hook_event_name":"Stop","cwd":"%s","last_assistant_message":"要不要继续最终验证？"}' "$PWD" \
  | uv run keep-going bridge stop-hook --input-json --synthetic --json-output
```

## 本机同步

修改以下运行面后必须执行 `uv run keep-going sync-local`：

- `src/keep_going/**`
- `.codex/skills/**`、`.codex/agents/**`
- `plugins/**`、`.agents/plugins/**`、`.claude-plugin/**`
- `scripts/**`、`packages/npm/**`
- install、bridge、hook、MCP 与相关说明文档

修改本地 canonical 后必须先运行 `uv run keep-going compile-policy` 并 review runtime。`sync-local` 会校验 source/runtime 哈希并同步真实本机安装态。

## 验证矩阵

| 改动面 | 必跑验证 |
|---|---|
| runtime / CLI / bridge | `uv run pytest tests/test_bridge.py tests/test_loop_metrics.py -q` |
| reply / hook / policy | `uv run pytest tests/test_decision_reply.py tests/test_decision_hook.py tests/test_conformance.py tests/test_policy_runtime.py -q` |
| override / category | `uv run pytest tests/test_overrides.py tests/test_bridge.py tests/test_bridge_fanout.py -q` |
| install / plugin / npm | `uv run pytest tests/test_integration_assets.py tests/test_npm_package.py -q`；`npm --prefix packages/npm test`；`uv run keep-going sync-local`；`uv run keep-going install --verify` |
| package / MCP / audit | `uv run pytest tests/test_mcp_stdio.py tests/test_package.py tests/test_audit.py -q` |
| privacy / release | `uv run python scripts/privacy-audit.py --history`；检查 npm tarball；全新 clone 复验 |

共享逻辑或发布边界变化后追加：

```bash
uv run pytest -q
uv run keep-going audit --smoke --json-output
```

## 编码约束

- Python 3.11+；依赖由 uv 管理。
- 路径使用 `pathlib.Path`。
- 函数保持聚焦，避免无意义抽象和重复。
- 新机制必须回答“不引入会怎样”，只实现当前必要部分。
- 任何归因都要提供文件、命令、测试或日志证据。

## 完成报告

交付时简短报告：完成度、主线状态、实际改动、已运行验证、未完成项和下一步。验证任务使用多 Agent 对抗式审查，主动寻找最坏情况与遗漏。
