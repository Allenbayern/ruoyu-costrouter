"""Flat v5 catalog validation regressions (upgraded from v4 fixture)."""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

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
spec = importlib.util.spec_from_file_location("cost_router_v5_fixes", PLUGIN)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)


def config():
    return {"catalog_version": 5, "routes": {
        tier: {"enabled": True, "provider": "custom:new-api", "model": model, "worker_profile": worker,
               "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 1}, "max_output_tokens": 1000,
               "budget_fallbacks": []}
        for tier, model, worker in (("flash", "deepseek-v4-flash", "worker-flash"),
                                    ("luna", "deepseek-v4-flash", "worker-luna"),
                                    ("terra", "deepseek-v4-flash", "worker-terra"),
                                    ("terra_pro", "deepseek-v4-pro", "worker-terra"),
                                    ("sol", "gpt-5.6-sol", "worker-sol"))
    }}


class V5CatalogFixTests(unittest.TestCase):
    def test_worker_models_match_v5_bindings(self):
        catalog = router._route_catalog(config())
        self.assertEqual("deepseek-v4-flash", catalog["flash"]["model"])
        self.assertEqual("worker-flash", catalog["flash"]["worker_profile"])
        self.assertEqual("deepseek-v4-flash", catalog["luna"]["model"])
        self.assertEqual("worker-luna", catalog["luna"]["worker_profile"])
        self.assertEqual("deepseek-v4-flash", catalog["terra"]["model"])
        self.assertEqual("worker-terra", catalog["terra"]["worker_profile"])
        self.assertEqual("deepseek-v4-pro", catalog["terra_pro"]["model"])
        self.assertEqual("worker-terra", catalog["terra_pro"]["worker_profile"])
        self.assertEqual("gpt-5.6-sol", catalog["sol"]["model"])
        self.assertEqual("worker-sol", catalog["sol"]["worker_profile"])

    def test_fallbacks_and_unknown_route_fields_are_rejected(self):
        cfg = config(); cfg["routes"]["terra"]["budget_fallbacks"] = ["flash"]
        with self.assertRaisesRegex(ValueError, "budget_fallbacks must be empty"):
            router._route_catalog(cfg)
        cfg = config(); cfg["routes"]["terra"]["fallback"] = "flash"
        with self.assertRaisesRegex(ValueError, "unsupported fields: fallback"):
            router._route_catalog(cfg)


if __name__ == "__main__": unittest.main()
