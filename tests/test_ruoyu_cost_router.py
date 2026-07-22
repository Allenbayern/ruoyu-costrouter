"""Standalone contract tests for the Kanban-backed ruoyu-cost-router."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

BUNDLE = Path(__file__).resolve().parents[1] / "ruoyu-cost-router"
PLUGIN_ID = "ruoyu-cost-router"


def _routes() -> dict:
    return {
        "luna": {"provider": "custom", "model": "gpt-5.6-luna", "worker_profile": "worker-luna", "pricing": {"input_per_million_usd": 0.2, "output_per_million_usd": 0.8}, "max_output_tokens": 1000, "budget_fallbacks": []},
        "luna_economy": {"provider": "deepseek", "model": "deepseek-v4-flash", "worker_profile": "worker-luna-economy", "pricing": {"input_per_million_usd": 0.1, "output_per_million_usd": 0.4}, "max_output_tokens": 1500, "budget_fallbacks": ["luna"]},
        "terra": {"provider": "custom", "model": "gpt-5.6-terra", "worker_profile": "worker-terra", "pricing": {"input_per_million_usd": 2.0, "output_per_million_usd": 8.0}, "max_output_tokens": 2500, "budget_fallbacks": ["luna_economy", "luna"]},
        "sol": {"provider": "custom", "model": "gpt-5.6-sol", "worker_profile": "worker-sol", "pricing": {"input_per_million_usd": 5.0, "output_per_million_usd": 20.0}, "max_output_tokens": 3000, "budget_fallbacks": ["terra", "luna_economy", "luna"]},
    }


def _config(*, routes: dict | None = None, llm: dict | None = None, routing: dict | None = None, budget: dict | None = None) -> dict:
    return {
        "catalog_version": 2,
        "llm": llm if llm is not None else {"allow_provider_override": True, "allow_model_override": True, "allowed_providers": ["custom", "deepseek"], "allowed_models": ["gpt-5.6-luna", "deepseek-v4-flash", "gpt-5.6-terra", "gpt-5.6-sol"]},
        "routes": routes if routes is not None else _routes(),
        "routing": routing if routing is not None else {"keyword_fallback_enabled": False, "keyword_tiers": {}},
        "budget": budget if budget is not None else {},
    }


class _RecordingContext:
    def __init__(self, *, response: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.response = response or {"ok": True, "task_id": "t_queued", "status": "ready", "created": True}

    def dispatch_tool(self, tool_name: str, args: dict, **kwargs) -> str:
        self.calls.append((tool_name, args))
        return json.dumps(self.response)


@pytest.fixture
def module_loader(monkeypatch):
    config_holder = {"value": _config()}
    agent = types.ModuleType("agent")
    redact = types.ModuleType("agent.redact")
    redact.redact_sensitive_text = lambda text, force=True: text.replace("secret", "[REDACTED]")
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []  # type: ignore[attr-defined]
    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: {"plugins": {"entries": {PLUGIN_ID: config_holder["value"]}}}
    tools = types.ModuleType("tools")
    tools.__path__ = []  # type: ignore[attr-defined]
    kanban_tools = types.ModuleType("tools.kanban_tools")
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.redact", redact)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.kanban_tools", kanban_tools)

    def load(plugin_config: dict | None = None):
        config_holder["value"] = plugin_config if plugin_config is not None else _config()
        spec = importlib.util.spec_from_file_location("standalone_ruoyu_cost_router", BUNDLE / "__init__.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load


def _call(module, context, args):
    return json.loads(module._handler(context, args))


def test_explicit_route_queues_real_worker_profile_with_target_workspace(module_loader):
    module, context = module_loader(), _RecordingContext()
    result = _call(module, context, {"goal": "Modify plugin", "route": "terra", "workspace_path": "/tmp/project", "board": "cost-router-plugin"})
    assert result["routing_status"] == "queued"
    assert result["task_id"] == "t_queued"
    assert result["worker_profile"] == "worker-terra"
    assert context.calls[0][0] == "kanban_create"
    task = context.calls[0][1]
    assert task["assignee"] == "worker-terra"
    assert task["workspace_kind"] == "dir"
    assert task["workspace_path"] == "/tmp/project"
    assert task["board"] == "cost-router-plugin"
    assert "Kanban-dispatched worker with real Hermes tools" in task["body"]


def test_catalog_model_migration_is_allowed_when_trusted(module_loader):
    config = _config()
    config["routes"]["luna"]["model"] = "gpt-5.7-luna"
    config["llm"]["allowed_models"].append("gpt-5.7-luna")
    module, context = module_loader(config), _RecordingContext()
    result = _call(module, context, {"goal": "Classify one record", "route": "luna"})
    assert result["routing_status"] == "queued"
    assert result["model"] == "gpt-5.7-luna"
    assert context.calls[0][1]["assignee"] == "worker-luna"


def test_untrusted_catalog_pair_denies_before_task_creation(module_loader):
    config = _config()
    config["routes"]["luna"]["model"] = "untrusted-model"
    module, context = module_loader(config), _RecordingContext()
    result = _call(module, context, {"goal": "Classify one record", "route": "luna"})
    assert result["routing_status"] == "denied"
    assert context.calls == []


def test_budget_fallback_queues_lower_cost_worker_with_explanation(module_loader):
    config = _config()
    config["routes"]["terra"]["pricing"] = {"input_per_million_usd": 1000, "output_per_million_usd": 1000}
    module, context = module_loader(config), _RecordingContext()
    result = _call(module, context, {"goal": "Implement a small fix", "route": "terra", "max_cost_usd": 0.01})
    assert result["routing_status"] == "queued"
    assert result["requested_tier"] == "terra"
    assert result["tier"] in {"luna_economy", "luna"}
    assert result["budget"]["fallback_applied"] is True
    assert context.calls[0][1]["assignee"] != "worker-terra"


def test_budget_exhaustion_denies_before_task_creation(module_loader):
    module, context = module_loader(), _RecordingContext()
    result = _call(module, context, {"goal": "Classify one record", "route": "luna", "max_cost_usd": 0})
    assert result["routing_status"] == "denied"
    assert "no configured route fits" in result["error"]
    assert context.calls == []


def test_keywords_do_not_upgrade_route_by_default(module_loader):
    module, context = module_loader(), _RecordingContext()
    result = _call(module, context, {"goal": "Classify this note", "context": "security final review"})
    assert result["tier"] == "terra"
    assert result["selection_mode"] == "default"


def test_input_limits_and_idempotency_are_passed_to_task(module_loader):
    module, context = module_loader(), _RecordingContext()
    result = _call(module, context, {"goal": "g" * 13_000, "context": "secret " + "c" * 13_000, "route": "luna"})
    task = context.calls[0][1]
    assert result["truncated"] == {"goal": True, "context": True, "output": False}
    assert len(task["body"].encode("utf-8")) <= module._MAX_TASK_BODY_BYTES
    assert task["idempotency_key"].startswith("ruoyu-cost-router:")
    assert "secret" not in task["body"]
    assert '"truncated": {"goal": true, "context": true, "output": false}' in task["body"]


def test_maximum_body_contract_is_utf8_safe(module_loader):
    module, context = module_loader(), _RecordingContext()
    result = _call(module, context, {"goal": "界" * 12_000, "context": "文" * 12_000, "route": "terra"})
    task = context.calls[0][1]
    assert len(task["body"].encode("utf-8")) <= 8 * 1024
    assert result["truncated"]["goal"] is True
    assert result["truncated"]["context"] is True
    assert '"truncated": {"goal": true, "context": true, "output": false}' in task["body"]


def test_kanban_creation_failure_is_returned_as_denial(module_loader):
    module, context = module_loader(), _RecordingContext(response={"ok": False, "error": "dispatcher unavailable"})
    result = _call(module, context, {"goal": "Do work", "route": "luna"})
    assert result["routing_status"] == "denied"
    assert "dispatcher unavailable" in result["error"]


def test_idempotency_key_is_scoped_to_board_and_workspace(module_loader):
    module = module_loader()
    first, second, third = _RecordingContext(), _RecordingContext(), _RecordingContext()
    base = {"goal": "Apply change", "route": "terra"}
    _call(module, first, {**base, "workspace_path": "/tmp/a", "board": "one"})
    _call(module, second, {**base, "workspace_path": "/tmp/b", "board": "one"})
    _call(module, third, {**base, "workspace_path": "/tmp/a", "board": "two"})
    keys = [context.calls[0][1]["idempotency_key"] for context in (first, second, third)]
    assert len(set(keys)) == 3


def test_existing_kanban_card_is_not_reported_as_queued(module_loader):
    module = module_loader()
    context = _RecordingContext(response={"ok": True, "task_id": "t_ready", "status": "ready", "created": False})
    result = _call(module, context, {"goal": "Do work", "route": "luna"})
    assert result["routing_status"] == "existing"
    assert result["task_status"] == "ready"
    assert result["worker_result"]["status"] == "existing"


def test_unknown_create_provenance_fails_closed_as_existing(module_loader):
    module = module_loader()
    context = _RecordingContext(response={"ok": True, "task_id": "t_legacy", "status": "ready"})
    result = _call(module, context, {"goal": "Do work", "route": "luna"})
    assert result["routing_status"] == "existing"
    assert result["task_status"] == "ready"


def test_explicit_route_rejects_untrusted_or_unbounded_task_type(module_loader):
    module, context = module_loader(), _RecordingContext()
    result = _call(module, context, {"goal": "Classify", "route": "luna", "task_type": "x" * 20_000})
    assert result["routing_status"] == "denied"
    assert "unsupported task_type" in result["error"]
    assert context.calls == []


def test_supplied_idempotency_key_is_scoped_to_board_and_workspace(module_loader):
    module = module_loader()
    first, second, third = _RecordingContext(), _RecordingContext(), _RecordingContext()
    base = {"goal": "Apply change", "route": "terra", "idempotency_key": "retry-42"}
    _call(module, first, {**base, "workspace_path": "/tmp/a", "board": "one"})
    _call(module, second, {**base, "workspace_path": "/tmp/b", "board": "one"})
    _call(module, third, {**base, "workspace_path": "/tmp/a", "board": "two"})
    keys = [context.calls[0][1]["idempotency_key"] for context in (first, second, third)]
    assert all(key.startswith("ruoyu-cost-router:") for key in keys)
    assert len(set(keys)) == 3


def test_scratch_workspace_rejects_explicit_path(module_loader):
    module, context = module_loader(), _RecordingContext()
    result = _call(module, context, {"goal": "Inspect", "route": "luna", "workspace_kind": "scratch", "workspace_path": "/tmp/not-scratch"})
    assert result["routing_status"] == "denied"
    assert "not allowed" in result["error"]
    assert context.calls == []


def test_worker_profile_cannot_be_retargeted_in_catalog(module_loader):
    config = _config()
    config["routes"]["sol"]["worker_profile"] = "worker-terra"
    module, context = module_loader(config), _RecordingContext()
    result = _call(module, context, {"goal": "Review", "route": "sol"})
    assert result["routing_status"] == "denied"
    assert context.calls == []
