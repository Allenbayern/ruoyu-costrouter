"""Flat v5 routing regressions; legacy v2/v3/v4 variant catalogs are unsupported."""
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
spec = importlib.util.spec_from_file_location("cost_router_routes_v5", PLUGIN)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)


def config(ledger):
    models = {"flash": "deepseek-v4-flash", "luna": "deepseek-v4-flash", "terra": "deepseek-v4-flash",
              "terra_pro": "deepseek-v4-pro", "sol": "gpt-5.6-sol"}
    workers = {"flash": "worker-flash", "luna": "worker-luna", "terra": "worker-terra",
               "terra_pro": "worker-terra", "sol": "worker-sol"}
    routes = {tier: {"enabled": True, "provider": "custom:new-api", "model": model,
                     "worker_profile": workers[tier], "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 1},
                     "max_output_tokens": 1000, "budget_fallbacks": []} for tier, model in models.items()}
    return {"catalog_version": 5, "reviewer_ledger_path": ledger, "routes": routes,
            "llm": {"allow_provider_override": True, "allow_model_override": True,
                    "allowed_providers": ["custom:new-api"], "allowed_models": list(models.values())},
            "budget": {"max_cost_usd": 1, "max_tokens": 10000, "remaining_budget_usd": 1}}


class Context:
    def __init__(self): self.calls = []
    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"ok": True, "created": True, "task_id": "t_v5", "status": "ready"})


class FlatV5RoutesTests(unittest.TestCase):
    def test_v2_v3_and_v4_catalogs_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=PLUGIN.parent) as tmp:
            cfg = config(str(Path(tmp) / "ledger.sqlite3"))
            for version in (2, 3, 4):
                cfg["catalog_version"] = version
                with self.assertRaisesRegex(ValueError, "catalog_version must be 5"):
                    router._route_catalog(cfg)

    def test_variant_only_request_is_explicitly_rejected(self):
        with tempfile.TemporaryDirectory(dir=PLUGIN.parent) as tmp:
            ctx = Context()
            with patch.object(router, "_plugin_config", return_value=config(str(Path(tmp) / "ledger.sqlite3"))):
                result = json.loads(router._handler(ctx, {"goal": "route this", "variant": "tier1_flash"}))
        self.assertEqual("denied", result["routing_status"])
        self.assertIn("variants were removed", result["error"])
        self.assertEqual([], ctx.calls)

    def test_direct_route_binds_the_v5_model_and_worker(self):
        with tempfile.TemporaryDirectory(dir=PLUGIN.parent) as tmp:
            ctx = Context()
            with patch.object(router, "_plugin_config", return_value=config(str(Path(tmp) / "ledger.sqlite3"))):
                result = json.loads(router._handler(ctx, {"goal": "architecture", "route": "terra"}))
        self.assertEqual("terra", result["route"])
        self.assertEqual("route", result["selection_mode"])
        self.assertEqual("worker-terra", ctx.calls[0][1]["assignee"])
        self.assertEqual("deepseek-v4-flash", ctx.calls[0][1]["model"])

    def test_v5_extra_entry_fields_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=PLUGIN.parent) as tmp:
            cfg = config(str(Path(tmp) / "ledger.sqlite3"))
            cfg["routes"]["terra"]["variants"] = {}
            with self.assertRaisesRegex(ValueError, "unsupported fields: variants"):
                router._route_catalog(cfg)


if __name__ == "__main__": unittest.main()
