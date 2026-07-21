"""Isolated verification for the private ruoyu-cost-router plugin bundle."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from agent.plugin_llm import PluginLlm, _TrustPolicy

BUNDLE = Path(__file__).resolve().parents[1] / "ruoyu-cost-router"
PLUGIN_ID = "ruoyu-cost-router"


def _default_routes() -> dict:
    return {
        "luna": {"provider": "custom", "model": "gpt-5.6-luna"},
        "luna_economy": {"provider": "deepseek", "model": "deepseek-v4-flash"},
        "terra": {"provider": "custom", "model": "gpt-5.6-terra"},
        "sol": {"provider": "custom", "model": "gpt-5.6-sol"},
    }


def _trusted_llm_config() -> dict:
    return {
        "allow_provider_override": True,
        "allowed_providers": ["custom", "deepseek"],
        "allow_model_override": True,
        "allowed_models": ["gpt-5.6-luna", "deepseek-v4-flash", "gpt-5.6-terra", "gpt-5.6-sol"],
    }


def _write_config(
    hermes_home: Path,
    *,
    enabled: bool,
    trust: bool,
    routes: dict | None = None,
    llm: dict | None = None,
) -> None:
    config = {"plugins": {"enabled": [PLUGIN_ID] if enabled else [], "entries": {}}}
    entry = {"routes": routes if routes is not None else _default_routes()}
    if trust:
        entry["llm"] = _trusted_llm_config() if llm is None else llm
    config["plugins"]["entries"][PLUGIN_ID] = entry
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _load_bundle_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    trust: bool,
    routes: dict | None = None,
    llm: dict | None = None,
) -> object:
    hermes_home = tmp_path / "hermes-home"
    destination = hermes_home / "plugins" / PLUGIN_ID
    shutil.copytree(BUNDLE, destination)
    _write_config(hermes_home, enabled=True, trust=trust, routes=routes, llm=llm)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    from hermes_cli import config as config_module

    config_module._config_cache = None  # type: ignore[attr-defined]
    spec = importlib.util.spec_from_file_location(
        f"test_{PLUGIN_ID.replace('-', '_')}", destination / "__init__.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingLlm:
    def __init__(self):
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            text="bounded result",
            provider=kwargs["provider"],
            model=kwargs["model"],
            usage=SimpleNamespace(input_tokens=2, output_tokens=3, total_tokens=5, cost_usd=None),
        )


def _direct_context(llm: _RecordingLlm):
    return SimpleNamespace(llm=llm)


def _context_with_llm(fake_caller, *, trusted: bool):
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    context = PluginContext(
        PluginManifest(name=PLUGIN_ID, source="test", key=PLUGIN_ID),
        PluginManager(),
    )
    context._llm = PluginLlm(  # type: ignore[attr-defined]
        plugin_id=PLUGIN_ID,
        policy_loader=lambda _: _TrustPolicy(
            plugin_id=PLUGIN_ID,
            allow_provider_override=trusted,
            allowed_providers=frozenset({"custom", "deepseek"}) if trusted else None,
            allow_model_override=trusted,
            allowed_models=frozenset({
                "gpt-5.6-luna", "deepseek-v4-flash", "gpt-5.6-terra", "gpt-5.6-sol",
            }) if trusted else None,
        ),
        sync_caller=fake_caller,
    )
    return context


def test_discovery_registers_private_tool_from_temp_hermes_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    shutil.copytree(BUNDLE, hermes_home / "plugins" / PLUGIN_ID)
    _write_config(hermes_home, enabled=True, trust=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    from hermes_cli import config as config_module
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    config_module._config_cache = None  # type: ignore[attr-defined]
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_ID]
    assert loaded.enabled is True
    assert "ruoyu_cost_router" in loaded.tools_registered
    assert registry.get_entry("ruoyu_cost_router") is not None


def test_denied_override_does_not_invoke_provider(tmp_path, monkeypatch):
    module = _load_bundle_module(tmp_path, monkeypatch, trust=False)
    calls = []

    def fake_caller(**kwargs):
        calls.append(kwargs)
        raise AssertionError("provider call must not occur when trust denies overrides")

    result = json.loads(module._handler(
        _context_with_llm(fake_caller, trusted=False),
        {"goal": "Classify a record", "route": "luna"},
    ))
    assert result["routing_status"] == "denied"
    assert "missing host LLM trust policy" in result["error"]
    assert calls == []


def test_authorized_route_uses_configured_pair_and_redacts_output(tmp_path, monkeypatch):
    module = _load_bundle_module(tmp_path, monkeypatch, trust=True)
    captured = []
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="result token=sk-test-redact-1234567890"))],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5),
    )

    def fake_caller(**kwargs):
        captured.append(kwargs)
        return "custom", "gpt-5.6-luna", response

    result = json.loads(module._handler(
        _context_with_llm(fake_caller, trusted=True),
        {"goal": "Classify a record", "route": "luna"},
    ))
    assert result["routing_status"] == "completed"
    assert result["tier"] == "luna"
    assert captured[0]["provider_override"] == "custom"
    assert captured[0]["model_override"] == "gpt-5.6-luna"
    assert "sk-test-redact-1234567890" not in result["output"]
    assert result["controller_decision_required"] is True


def test_missing_route_configuration_denies_without_provider_call(tmp_path, monkeypatch):
    module = _load_bundle_module(tmp_path, monkeypatch, trust=True, routes={})
    calls = []

    def fake_caller(**kwargs):
        calls.append(kwargs)
        raise AssertionError("missing route must deny before a provider call")

    result = json.loads(module._handler(
        _context_with_llm(fake_caller, trusted=True),
        {"goal": "Classify a record", "route": "luna"},
    ))
    assert result["routing_status"] == "denied"
    assert "missing route" in result["error"]
    assert calls == []


@pytest.mark.parametrize(
    ("tier", "provider", "model"),
    [
        ("luna", "custom", "gpt-5.6-luna"),
        ("luna_economy", "deepseek", "deepseek-v4-flash"),
        ("terra", "custom", "gpt-5.6-terra"),
        ("sol", "custom", "gpt-5.6-sol"),
    ],
)
def test_each_canonical_tier_invokes_its_immutable_exact_pair(tmp_path, monkeypatch, tier, provider, model):
    module = _load_bundle_module(tmp_path, monkeypatch, trust=True)
    llm = _RecordingLlm()

    result = json.loads(module._handler(_direct_context(llm), {"goal": "bounded task", "route": tier}))

    assert result["routing_status"] == "completed"
    assert [(call["provider"], call["model"]) for call in llm.calls] == [(provider, model)]


@pytest.mark.parametrize(
    "tier,configured_pair",
    [
        ("luna", {"provider": "deepseek", "model": "deepseek-v4-flash"}),
        ("luna_economy", {"provider": "custom", "model": "gpt-5.6-luna"}),
        ("terra", {"provider": "custom", "model": "gpt-5.6-sol"}),
        ("sol", {"provider": "custom", "model": "gpt-5.6-terra"}),
    ],
)
def test_cross_pair_configured_for_a_tier_denies_before_invocation(tmp_path, monkeypatch, tier, configured_pair):
    routes = _default_routes()
    routes[tier] = configured_pair
    module = _load_bundle_module(tmp_path, monkeypatch, trust=True, routes=routes)
    llm = _RecordingLlm()

    result = json.loads(module._handler(_direct_context(llm), {"goal": "bounded task", "route": tier}))

    assert result["routing_status"] == "denied"
    assert llm.calls == []


@pytest.mark.parametrize(
    "llm_config",
    [
        {},
        {"allow_provider_override": True, "allow_model_override": True},
        {"allow_provider_override": True, "allowed_providers": [], "allow_model_override": True, "allowed_models": []},
        {"allow_provider_override": True, "allowed_providers": "custom", "allow_model_override": True, "allowed_models": "gpt-5.6-luna"},
        {"allow_provider_override": True, "allowed_providers": ["*"], "allow_model_override": True, "allowed_models": ["*"]},
        {"allow_provider_override": True, "allowed_providers": ["unknown"], "allow_model_override": True, "allowed_models": ["unknown"]},
        {"allow_provider_override": True, "allowed_providers": ["custom"], "allow_model_override": True, "allowed_models": ["gpt-5.6-terra"]},
    ],
)
def test_absent_empty_or_malformed_host_allowlists_deny_before_invocation(tmp_path, monkeypatch, llm_config):
    module = _load_bundle_module(tmp_path, monkeypatch, trust=True, llm=llm_config)
    llm = _RecordingLlm()

    result = json.loads(module._handler(_direct_context(llm), {"goal": "bounded task", "route": "luna"}))

    assert result["routing_status"] == "denied"
    assert llm.calls == []


def test_altered_configured_pair_denies_before_invocation(tmp_path, monkeypatch):
    routes = _default_routes()
    routes["terra"] = {"provider": "custom", "model": "gpt-5.6-luna"}
    module = _load_bundle_module(tmp_path, monkeypatch, trust=True, routes=routes)
    llm = _RecordingLlm()

    result = json.loads(module._handler(_direct_context(llm), {"goal": "bounded task", "route": "terra"}))

    assert result["routing_status"] == "denied"
    assert llm.calls == []
