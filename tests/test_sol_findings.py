"""Flat v5 routing behavior and Sol fail-closed regressions."""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

agent = types.ModuleType("agent")
redact = types.ModuleType("agent.redact")
redact.redact_sensitive_text = lambda value, force=False: value
sys.modules["agent"] = agent
sys.modules["agent.redact"] = redact
hermes_cli = types.ModuleType("hermes_cli")
config_mod = types.ModuleType("hermes_cli.config")
config_mod.load_config = lambda: {}
sys.modules["hermes_cli"] = hermes_cli
sys.modules["hermes_cli.config"] = config_mod
sys.modules["tools"] = types.ModuleType("tools")
sys.modules["tools.kanban_tools"] = types.ModuleType("tools.kanban_tools")

PLUGIN = Path(__file__).resolve().parents[1] / "ruoyu-cost-router" / "__init__.py"
spec = importlib.util.spec_from_file_location("cost_router_findings_v5", PLUGIN)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)


def config(ledger):
    routes = {
        "flash": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-flash", "worker_profile": "worker-flash", "pricing": {"input_per_million_usd": .0, "output_per_million_usd": .0}, "max_output_tokens": 2500, "budget_fallbacks": []},
        "luna": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-flash", "worker_profile": "worker-luna", "pricing": {"input_per_million_usd": .0, "output_per_million_usd": .0}, "max_output_tokens": 2000, "budget_fallbacks": []},
        "terra": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-flash", "worker_profile": "worker-terra", "pricing": {"input_per_million_usd": .0, "output_per_million_usd": .0}, "max_output_tokens": 4000, "budget_fallbacks": []},
        "terra_pro": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-pro", "worker_profile": "worker-terra", "pricing": {"input_per_million_usd": .435, "output_per_million_usd": .87}, "max_output_tokens": 3000, "budget_fallbacks": []},
        "sol": {"enabled": True, "provider": "custom:new-api", "model": "gpt-5.6-sol", "worker_profile": "worker-sol", "pricing": {"input_per_million_usd": .9, "output_per_million_usd": 4.05}, "max_output_tokens": 3000, "budget_fallbacks": []},
    }
    return {"catalog_version": 5, "reviewer_ledger_path": ledger, "routes": routes,
            "llm": {"allow_provider_override": True, "allow_model_override": True, "allowed_providers": ["custom:new-api"], "allowed_models": [route["model"] for route in routes.values()]},
            "budget": {"max_cost_usd": 1, "max_tokens": 100000, "remaining_budget_usd": 1}}


class Context:
    def __init__(self): self.calls = []
    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"ok": True, "created": True, "task_id": "t_v5", "status": "ready"})


class SolFindingsTests(unittest.TestCase):
    def invoke(self, args):
        with tempfile.TemporaryDirectory(dir=PLUGIN.parent) as tmp:
            ctx = Context()
            with patch.object(router, "_plugin_config", return_value=config(str(Path(tmp) / "ledger.sqlite3"))):
                return json.loads(router._handler(ctx, {"goal": "bounded work", **args})), ctx

    def test_flat_catalog_has_no_budget_fallbacks(self):
        catalog = router._route_catalog(config(str(PLUGIN.parent / ".unit.sqlite3")))
        self.assertEqual({"flash", "luna", "terra", "terra_pro", "sol"}, set(catalog))
        self.assertTrue(all(route["budget_fallbacks"] == [] for route in catalog.values()))

    def test_variant_with_route_is_rejected_before_dispatch(self):
        result, ctx = self.invoke({"route": "luna", "variant": "tier2_luna"})
        self.assertEqual("denied", result["routing_status"])
        self.assertIn("variants were removed", result["error"])
        self.assertEqual([], ctx.calls)

    def test_sol_without_protected_review_remains_denied(self):
        result, ctx = self.invoke({"route": "sol"})
        self.assertEqual("denied", result["routing_status"])
        self.assertIn("validated hard-L2 protected-final review metadata", result["error"])
        self.assertEqual([], ctx.calls)


if __name__ == "__main__": unittest.main()
