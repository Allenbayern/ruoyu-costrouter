# ruoyu-costrouter

Hermes 多模型路由工具包：把一个主控模型（controller）和多个 worker profile 组织起来，让 Hermes 可以通过 `cost_router(...)` 把不同类型的任务分发给不同模型。

This repository packages a Hermes `cost_router` integration so other Hermes users can reproduce a routing-first multi-model setup.

## 适合谁用

如果你希望在 Hermes 里这样工作：

- 主模型只负责拆任务、审核结果、做最终判断；
- 低成本模型处理粗筛、聚类、表格、字段抽取；
- 强模型处理技术判断、复杂压缩、关键推理；
- 写作模型处理中文润色、成稿、改写；
- 所有 worker 都通过独立 Hermes profile 配置 provider/model；

那这个仓库就是给你用的。

## 它做了什么

安装后，Hermes 会多一个面向模型可调用的工具：`cost_router`。

`cost_router` 会：

1. 接收 `profile`、`goal`、`context` 或 `tasks`；
2. 读取 `<HERMES_HOME>/profiles/<profile>/config.yaml`；
3. 解析该 worker profile 的 provider、model、base_url、api_mode、api_key；
4. 把任务交给 Hermes 原生 `delegate_task` 运行时执行；
5. 让主控模型拿回 worker 的结构化结果，再做最终判断。

## 重要说明：为什么是 patch 包

当前 Hermes 的 `delegate_task` 需要 live `parent_agent` 对象，而普通第三方工具无法稳定拿到这个对象。

所以本仓库不是伪装成“纯插件”，而是一个可复现的 Hermes core patch 包。它会补齐这些集成点：

- `tools/delegate_tool.py`：注册 `cost_router` schema 和 handler；
- `run_agent.py`：增加 `AIAgent._dispatch_cost_router(...)`；
- `agent/agent_runtime_helpers.py`：让 agent-loop 工具路径支持 `cost_router`；
- `agent/tool_executor.py`：让顺序工具执行器正确展示和调度 `cost_router`；
- `toolsets.py` / `model_tools.py`：把 `cost_router` 暴露到 delegation 相关 toolset。

等 Hermes 未来提供稳定的 agent-loop 第三方工具 API 后，这个仓库可以再改成纯插件形态。

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
  "profile": "worker-dspro",
  "goal": "Inspect this module and identify the narrow root cause.",
  "context": "Include file paths, constraints, observed errors, and requested output shape.",
  "role": "leaf"
}
```

批量任务：

```json
{
  "profile": "worker-dsflash",
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
python -m pytest tests/tools/test_delegate.py::TestCostRouter -q
python -m pytest tests/run_agent/test_run_agent.py -k cost_router -q
```

如果你的 Hermes checkout 有自己的测试入口，请优先使用项目内测试脚本。

本仓库整理时已在干净 Hermes worktree 上验证过：

- `scripts/install.sh` 可应用 patch；
- patch 后相关 Python 文件可 `py_compile`；
- focused tests 通过：`8 passed, 463 deselected`；
- `scripts/uninstall.sh` 可逆卸载，卸载后 `git diff --quiet` 通过。

## 卸载

```bash
./scripts/uninstall.sh /path/to/hermes-agent
```

卸载脚本会用 `git apply -R` 反向撤销 `patches/cost-router.patch`。

## 安全说明

- 不要在 prompt 或工具参数里直接传 API key；
- `cost_router` 只接收 worker profile 名称，不接收 provider 密钥；
- 真实凭据应由 Hermes profile/provider 配置解析；
- 提交前请运行 `git diff`，确认没有把本地密钥、`.env`、token 写入仓库。

## English summary

`ruoyu-costrouter` adds a Hermes `cost_router(...)` tool that routes delegated subtasks through named worker profiles. It reads each worker profile's Hermes config, resolves model/provider settings, and dispatches through the existing `delegate_task` runtime.

Because current Hermes requires live `parent_agent` state for delegation, this repository ships as a reproducible core patch bundle rather than a pure third-party plugin.

Quick start:

```bash
git clone https://github.com/Allenbayern/ruoyu-costrouter.git
cd ruoyu-costrouter
./scripts/install.sh ~/.hermes/hermes-agent
mkdir -p ~/.hermes/profiles
cp -R profiles/worker-* ~/.hermes/profiles/
```

Then edit each copied `config.yaml` for your own providers and models.
