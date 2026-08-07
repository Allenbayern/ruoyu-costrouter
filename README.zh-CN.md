# ruoyu-costrouter — 独立发布仓库

私有 **ruoyu-cost-router** Hermes 插件的独立仓库根（flat catalog **v5**，版本 0.4.0）。安装与运行时配置由运维控制；本仓库不含任何凭据，且不修改 Hermes Core。

## 目录结构

- `ruoyu-cost-router/`：可安装的插件目录（`__init__.py`、`plugin.yaml`、`config.template.yaml`，以及完整的 v5 契约 README）。
- `tests/`：独立的 router→Kanban 提交契约测试（12 个文件，v5 路由：`flash` / `luna` / `terra` / `terra_pro` / `sol`）。
- `pyproject.toml`：包元数据与测试依赖范围。
- `scripts/run_tests.sh`：可复现的测试入口。

## 克隆后验证

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
scripts/run_tests.sh
```

契约测试验证：选定的一条精确路由会创建绑定正确 worker 配置、provider 与模型的 `kanban_create` 请求——绝不是裸模型调用。其中一项集成测试（`test_flat_v4.py::FlatCatalogV5Tests::test_router_reports_queued_then_existing_with_real_host_create_response`）针对真实 Hermes `kanban_create` host 响应验证 queued-vs-existing 幂等契约；它需要 Hermes Core 源码树，源码树不存在时自动跳过。

真实安装额外要求：正在运行的 Kanban 调度器，以及已配置的 worker 配置 `worker-flash`、`worker-luna`、`worker-terra`、`worker-sol`；通过检查生成的任务 ID、worker 运行记录与日志来验证实际执行。完整的 v5 路由表、任务类型绑定与回滚说明见 `ruoyu-cost-router/README.md`。

安装、配置、远端仓库、发布、合并与验收决策均由 controller 保留。
