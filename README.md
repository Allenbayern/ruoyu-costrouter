# ruoyu-costrouter

Hermes `cost_router` runtime patch 包：把已经注册在 Hermes delegation 工具集里的 `cost_router(...)` 走通 agent runtime、post-hook 和并发工具执行路径。

This repository packages the latest runtime integration patch from `Allenbayern/hermes-agent:fix/cost-router-concurrent-runtime` / `NousResearch/hermes-agent#58314` so it can be reproduced on a clean Hermes checkout.

## 适合谁用

如果你希望在 Hermes 里这样工作：

- controller 负责拆任务、审核结果、做最终判断；
- `cost_router(...)` 像 `delegate_task(...)` 一样拿到 live `parent_agent`；
- batch 里同时出现 `cost_router` 和其他并发安全工具时，不被迫降级到顺序路径；
- subagent / background delegation 的状态、post-hook、session flush 行为保持和 Hermes 原生 runtime 一致；

那这个仓库就是给你用的。

## 当前版本做了什么

安装后，patch 会补齐这些集成点：

1. `agent/agent_runtime_helpers.py`
   - 把 `cost_router` 加入 agent-runtime post-hook ownership；
   - 在 runtime helper 路径里用 live `parent_agent` 调用 `_handle_cost_router(...)`。
2. `agent/tool_dispatch_helpers.py`
   - 把 `cost_router` 标记为 batch 并发安全工具。
3. `agent/tool_executor.py`
   - 在顺序工具执行路径里特殊处理 `cost_router`，保证它不走普通 registry fallback，而是拿到 live `parent_agent`。
4. `tools/delegate_tool.py`
   - 增加 `_handle_cost_router(...)`，复用 `delegate_task(...)` 的 runtime-aware delegation 行为。
5. `tests/run_agent/test_run_agent.py`
   - 增加 focused regression tests，覆盖 batch concurrent path 和 parent-agent 注入。

> 注意：当前最新 patch 假设 Hermes 主线已经有 `cost_router` schema / toolset 注册基础。这个包不再携带旧版“从 worker profile 解析 provider/model/api_key”的完整实现；它只同步最新 runtime 修复。

## 仓库内容

```text
.
├── README.md
├── examples/
│   └── controller-policy.md
├── patches/
│   └── cost-router.patch
├── profiles/
│   ├── worker-dsflash/config.yaml
│   ├── worker-dspro/config.yaml
│   ├── worker-gpt54/config.yaml
│   └── worker-gpt55/config.yaml
└── scripts/
    ├── install.sh
    └── uninstall.sh
```

`profiles/` 和 `examples/` 保留为 controller / worker routing 配置参考；本 patch 本身不读取或安装这些 profile。

## 安装

先 clone 本仓库：

```bash
git clone https://github.com/Allenbayern/ruoyu-costrouter.git
cd ruoyu-costrouter
```

对你的 Hermes Agent 源码目录应用 patch：

```bash
./scripts/install.sh /path/to/hermes-agent
```

如果你使用默认源码安装位置，通常是：

```bash
./scripts/install.sh ~/.hermes/hermes-agent
```

安装脚本会：

- 检查目标目录是不是 git checkout；
- 检查 Hermes 关键文件是否存在；
- 创建一个备份分支 `ruoyu-costrouter-backup-<timestamp>`；
- 先执行 `git apply --check`，确认 patch 可应用；
- 再正式应用 `patches/cost-router.patch`。

## 配置 worker profiles

模板路径示例：`profiles/worker-dsflash/config.yaml`。

复制模板到 Hermes profiles 目录：

```bash
mkdir -p ~/.hermes/profiles
cp -R profiles/worker-dsflash ~/.hermes/profiles/
cp -R profiles/worker-dspro ~/.hermes/profiles/
cp -R profiles/worker-gpt54 ~/.hermes/profiles/
cp -R profiles/worker-gpt55 ~/.hermes/profiles/
```

然后编辑每个 `config.yaml`，换成你自己的 provider、model、base_url 和 API key 来源。

模板里不会包含真实 key。建议使用环境变量或你现有 Hermes provider 配置，不要把真实密钥提交到 git。

## 推荐路由策略

可以参考 `examples/controller-policy.md`。

一个常见分工是：

- `worker-dsflash`：粗筛、日志聚类、低价值预处理、表格/清单/字段抽取；
- `worker-dspro`：窄范围技术分析、复杂压缩、局部源码判断；
- `worker-gpt54`：中文草稿、润色、改写、面向用户的高质量文案；
- `worker-gpt55`：需要顶级推理能力的困难执行切片；
- controller：保留最终判断权，负责风险、阻塞、发布、配置变更和最终回复。

## 使用示例

单个任务：

```json
{
  "goal": "Inspect this module and identify the narrow root cause.",
  "context": "Include file paths, constraints, observed errors, and requested output shape.",
  "role": "leaf"
}
```

批量任务：

```json
{
  "tasks": [
    {
      "goal": "Cluster these warnings",
      "context": "Paste or reference logs. Return grouped causes and counts."
    },
    {
      "goal": "Extract fields into a checklist",
      "context": "Paste source text. Return a compact checklist only."
    }
  ]
}
```

## 验证安装

在 Hermes Agent 源码目录里运行：

```bash
python -m pytest tests/run_agent/test_run_agent.py -k cost_router -q
```

如果你的 Hermes checkout 有自己的测试入口，请优先使用项目内测试脚本。

本仓库更新时已在干净 Hermes worktree 上验证过：

- `scripts/install.sh` 可应用 patch；
- patch 后相关 Python 文件可 `py_compile`；
- focused test command 可启动执行；
- `scripts/uninstall.sh` 可逆卸载，卸载后 `git diff --quiet` 通过。

## 卸载

```bash
./scripts/uninstall.sh /path/to/hermes-agent
```

卸载脚本会用 `git apply -R` 反向撤销 `patches/cost-router.patch`。

## 安全说明

- 不要在 prompt 或工具参数里直接传 API key；
- 不要把真实 profile 配置、`.env`、token 或 credential 文件提交到本仓库；
- 提交前请运行 `git diff`，确认没有把本地密钥写入仓库。

## English summary

`ruoyu-costrouter` packages the latest Hermes `cost_router(...)` runtime patch. It routes `cost_router` through the same live-agent delegation path as `delegate_task`, allows concurrent tool batches that include `cost_router`, and preserves parent-agent context for runtime hooks.

Quick start:

```bash
git clone https://github.com/Allenbayern/ruoyu-costrouter.git
cd ruoyu-costrouter
./scripts/install.sh ~/.hermes/hermes-agent
mkdir -p ~/.hermes/profiles
cp -R profiles/worker-* ~/.hermes/profiles/
```

Then edit each copied `config.yaml` for your own providers and models if your Hermes setup uses worker profiles.
