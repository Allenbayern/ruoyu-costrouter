"""Flat v5 five-route catalog contract tests.

Routes: flash / luna / terra / terra_pro / sol.
- flash: mechanical low-risk execution (classify/dedupe/normalize/fixed_extraction/router)
- luna: Luna Economy — bounded bulk low-risk preprocessing (cleanup)
- terra: default production execution (coding/rag_answer/evidence_synthesis/repair/draft/final_chinese/architecture/cross_artifact_analysis/difficult_debugging)
- terra_pro: explicit quality upgrade only (reachable solely by explicit route, paired with an upgrade task type)
- sol: protected independent review (final_review)

The test context records dispatch requests and never creates real Kanban tasks.
"""
import importlib.util
import json
import os
import subprocess
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
tools = types.ModuleType("tools")
kanban_tools = types.ModuleType("tools.kanban_tools")
sys.modules["tools"] = tools
sys.modules["tools.kanban_tools"] = kanban_tools

PLUGIN = Path(__file__).resolve().parents[1] / "ruoyu-cost-router" / "__init__.py"
spec = importlib.util.spec_from_file_location("cost_router_flat_v5", PLUGIN)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

_ROUTE_TABLE = {
    "flash": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-flash", "worker_profile": "worker-flash", "pricing": {"input_per_million_usd": 0.0, "output_per_million_usd": 0.0}, "max_output_tokens": 65536, "budget_fallbacks": []},
    "luna": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-flash", "worker_profile": "worker-luna", "pricing": {"input_per_million_usd": 0.0, "output_per_million_usd": 0.0}, "max_output_tokens": 12000, "budget_fallbacks": []},
    "terra": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-flash", "worker_profile": "worker-terra", "pricing": {"input_per_million_usd": 0.0, "output_per_million_usd": 0.0}, "max_output_tokens": 65536, "budget_fallbacks": []},
    "terra_pro": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-pro", "worker_profile": "worker-terra", "pricing": {"input_per_million_usd": 0.435, "output_per_million_usd": 0.87}, "max_output_tokens": 12000, "budget_fallbacks": []},
    "sol": {"enabled": True, "provider": "custom:new-api", "model": "gpt-5.6-sol", "worker_profile": "worker-sol", "pricing": {"input_per_million_usd": 0.9, "output_per_million_usd": 4.05}, "max_output_tokens": 6000, "budget_fallbacks": []},
}


def config(ledger=None):
    routes = {name: dict(entry) for name, entry in _ROUTE_TABLE.items()}
    return {"catalog_version": 5, "routes": routes,
            "reviewer_ledger_path": ledger or str(PLUGIN.parent / ".unit-flat-v5.sqlite3"),
            "llm": {"allow_provider_override": True, "allow_model_override": True,
                    "allowed_providers": ["custom:new-api"],
                    "allowed_models": [route["model"] for route in routes.values()]},
            "budget": {"max_cost_usd": 1, "max_tokens": 131072, "remaining_budget_usd": 1},
            "routing": {"keyword_fallback_enabled": False, "keyword_tiers": {}}}


class Context:
    def __init__(self):
        self.calls = []

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"ok": True, "created": True, "task_id": "mock-flat-v5", "status": "ready"})


class FlatCatalogV5Tests(unittest.TestCase):
    def test_catalog_accepts_only_exact_five_routes(self):
        cfg = config()
        self.assertEqual(("flash", "luna", "terra", "terra_pro", "sol"), router._TIERS)
        self.assertEqual(set(cfg["routes"]), set(router._route_catalog(cfg)))
        for version in (2, 3, 4):
            bad = config(); bad["catalog_version"] = version
            with self.assertRaisesRegex(ValueError, "catalog_version must be 5"):
                router._route_catalog(bad)
        for legacy in ("luna_economy", "tier1_flash", "gpt54"):
            bad = config(); bad["routes"][legacy] = bad["routes"]["flash"]
            with self.assertRaisesRegex(ValueError, "exactly: flash, luna, terra, terra_pro, sol"):
                router._route_catalog(bad)

    def test_catalog_rejects_fallbacks_and_variant_fields(self):
        bad = config(); bad["routes"]["terra"]["budget_fallbacks"] = ["flash"]
        with self.assertRaisesRegex(ValueError, "budget_fallbacks must be empty"):
            router._route_catalog(bad)
        for field in ("variant", "variants", "default_variant"):
            bad = config(); bad["routes"]["terra"][field] = {}
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                router._route_catalog(bad)

    def test_task_map_binds_each_type_to_its_v5_role(self):
        self.assertEqual({
            "router": "flash", "classify": "flash", "dedupe": "flash",
            "normalize": "flash", "fixed_extraction": "flash",
            "cleanup": "luna",
            "coding": "terra", "rag_answer": "terra", "evidence_synthesis": "terra",
            "repair": "terra", "draft": "terra", "final_chinese": "terra",
            "architecture": "terra", "cross_artifact_analysis": "terra",
            "difficult_debugging": "terra", "final_review": "sol",
        }, router._TASK_TYPE_TIERS)

    def test_exact_route_and_task_selection_has_no_default_or_legacy_alias(self):
        self.assertEqual(("flash", "route", "route:flash"), router._select_tier(" FLASH ", "", None, "classify", None, {}))
        self.assertEqual(("terra", "task_type", "task_type:architecture"), router._select_tier(None, "", None, "architecture", None, {}))
        self.assertEqual(("luna", "task_type", "task_type:cleanup"), router._select_tier(None, "", None, "cleanup", None, {}))
        self.assertEqual(("terra", "task_type", "task_type:coding"), router._select_tier(None, "", None, "coding", None, {}))
        for route in ("luna_economy", "tier1_flash", "gpt54", "deepseek-pro"):
            with self.subTest(route=route):
                with self.assertRaisesRegex(ValueError, "route must be one of"):
                    router._select_tier(route, "", None, None, None, {})
        with self.assertRaisesRegex(ValueError, "no default route"):
            router._select_tier(None, "", None, None, None, {})

    def test_terra_pro_is_explicit_upgrade_only(self):
        # Explicit upgrade route + upgrade task type is permitted.
        self.assertEqual(("terra_pro", "route", "route:terra_pro"),
                         router._select_tier("terra_pro", "", None, "architecture", None, {}))
        # Production task type cannot imply terra_pro (no automatic upgrades).
        self.assertEqual(("terra", "task_type", "task_type:architecture"),
                         router._select_tier(None, "", None, "architecture", None, {}))
        # Explicit terra_pro with a non-upgrade task type is a conflict.
        with self.assertRaisesRegex(ValueError, "route and task_type sources conflict"):
            router._select_tier("terra_pro", "", None, "coding", None, {})

    def test_direct_routes_pin_worker_model_and_provider(self):
        with tempfile.TemporaryDirectory(dir=PLUGIN.parent) as tmp:
            cfg = config(str(Path(tmp) / "ledger.sqlite3"))
            cases = (
                ("flash", "worker-flash", "deepseek-v4-flash"),
                ("luna", "worker-luna", "deepseek-v4-flash"),
                ("terra", "worker-terra", "deepseek-v4-flash"),
                ("terra_pro", "worker-terra", "deepseek-v4-pro"),
            )
            for route, worker, model in cases:
                with self.subTest(route=route):
                    ctx = Context()
                    with patch.object(router, "_plugin_config", return_value=cfg):
                        result = json.loads(router._handler(ctx, {"goal": "x", "route": route}))
                    self.assertEqual("queued", result["routing_status"])
                    self.assertEqual(route, result["route"])
                    self.assertEqual(worker, result["worker_profile"])
                    self.assertEqual(model, result["model"])
                    self.assertEqual("custom:new-api", result["provider"])
                    self.assertEqual(worker, ctx.calls[0][1]["assignee"])
                    self.assertEqual(model, ctx.calls[0][1]["model"])
                    self.assertEqual("custom:new-api", ctx.calls[0][1]["provider"])

    def test_router_reports_queued_then_existing_with_real_host_create_response(self):
        """The router must propagate the host's actual create/replay result."""
        host_root = PLUGIN.parents[2] / "hermes-agent"
        if not host_root.is_dir():
            self.skipTest("hermes-agent source tree not available in this checkout")
        script = r'''
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

from tools import kanban_tools

plugin = Path(os.environ["ROUTER_PLUGIN"])
spec = importlib.util.spec_from_file_location("router_host_contract", plugin)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

route_table = {
    "flash": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-flash", "worker_profile": "worker-flash", "pricing": {"input_per_million_usd": 0.0, "output_per_million_usd": 0.0}, "max_output_tokens": 65536, "budget_fallbacks": []},
    "luna": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-flash", "worker_profile": "worker-luna", "pricing": {"input_per_million_usd": 0.0, "output_per_million_usd": 0.0}, "max_output_tokens": 12000, "budget_fallbacks": []},
    "terra": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-flash", "worker_profile": "worker-terra", "pricing": {"input_per_million_usd": 0.0, "output_per_million_usd": 0.0}, "max_output_tokens": 65536, "budget_fallbacks": []},
    "terra_pro": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-pro", "worker_profile": "worker-terra", "pricing": {"input_per_million_usd": 0.435, "output_per_million_usd": 0.87}, "max_output_tokens": 12000, "budget_fallbacks": []},
    "sol": {"enabled": True, "provider": "custom:new-api", "model": "gpt-5.6-sol", "worker_profile": "worker-sol", "pricing": {"input_per_million_usd": 0.9, "output_per_million_usd": 4.05}, "max_output_tokens": 6000, "budget_fallbacks": []},
}
routes = {name: dict(entry) for name, entry in route_table.items()}
cfg = {"catalog_version": 5, "routes": routes, "reviewer_ledger_path": str(Path(os.environ["HERMES_HOME"]) / "ledger.sqlite3"), "llm": {"allow_provider_override": True, "allow_model_override": True, "allowed_providers": ["custom:new-api"], "allowed_models": [route["model"] for route in routes.values()]}, "budget": {"max_cost_usd": 1, "max_tokens": 131072, "remaining_budget_usd": 1}, "routing": {"keyword_fallback_enabled": False, "keyword_tiers": {}}}

class Context:
    def dispatch_tool(self, name, args):
        assert name == "kanban_create"
        return kanban_tools._handle_create(args)

ctx = Context()
with patch.object(router, "_plugin_config", return_value=cfg):
    first = json.loads(router._handler(ctx, {"goal": "host create contract", "route": "flash", "idempotency_key": "router-host-contract"}))
    second = json.loads(router._handler(ctx, {"goal": "host create contract", "route": "flash", "idempotency_key": "router-host-contract"}))
assert first["routing_status"] == "queued", first
assert second["routing_status"] == "existing", second
assert first["task_id"] == second["task_id"]
from hermes_cli import kanban_db as kb
conn = kb.connect()
try:
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?", ("ruoyu-cost-router:" + __import__("hashlib").sha256("\0".join(("supplied", "flash", "default", "scratch", "", "router-host-contract", "")).encode("utf-8")).hexdigest(),)).fetchone()[0] == 1
finally:
    conn.close()
'''
        with tempfile.TemporaryDirectory(dir=PLUGIN.parent) as tmp:
            env = {**os.environ, "PYTHONPATH": str(host_root), "HERMES_HOME": str(Path(tmp) / ".hermes"), "HERMES_KANBAN_HOME": str(Path(tmp) / ".hermes"), "HERMES_PROFILE": "test-router", "ROUTER_PLUGIN": str(PLUGIN), "HERMES_KANBAN_DB": str(Path(tmp) / "kanban.sqlite3")}
            for key in (
                "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DELEGATED_CHILD_CONTEXT",
            ):
                env.pop(key, None)
            result = subprocess.run([sys.executable, "-c", script], env=env, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)

    def test_legacy_variant_and_conflicting_route_inputs_are_denied_without_tasks(self):
        cases = (
            {"route": "luna_economy"}, {"route": "tier1_flash"}, {"route": "flash", "variant": "tier1_flash"},
            {"route": "flash", "project": {"route": "terra"}},
            {"route": "flash", "task_type": "architecture"},
            {"route": "terra_pro", "task_type": "coding"},
        )
        for args in cases:
            with self.subTest(args=args):
                ctx = Context()
                with patch.object(router, "_plugin_config", return_value=config()):
                    result = json.loads(router._handler(ctx, {"goal": "x", **args}))
                self.assertEqual("denied", result["routing_status"])
                self.assertEqual([], ctx.calls)

    def test_explicit_terra_pro_budget_exhaustion_returns_budget_exceeded_without_task(self):
        ctx = Context()
        with patch.object(router, "_plugin_config", return_value=config()):
            result = json.loads(router._handler(ctx, {"goal": "x", "route": "terra_pro", "max_cost_usd": 0}))
        self.assertEqual("budget_exceeded", result["routing_status"])
        self.assertEqual([], ctx.calls)

    def test_sol_still_requires_its_existing_protected_admission_contract(self):
        ctx = Context()
        with patch.object(router, "_plugin_config", return_value=config()):
            result = json.loads(router._handler(ctx, {"goal": "x", "route": "sol"}))
        self.assertEqual("denied", result["routing_status"])
        self.assertEqual([], ctx.calls)

    def test_schema_exposes_only_flat_routes_and_no_variant(self):
        props = router._SCHEMA["parameters"]["properties"]
        self.assertNotIn("variant", props)
        self.assertEqual(["flash", "luna", "terra", "terra_pro", "sol"], props["route"]["enum"])


if __name__ == "__main__":
    unittest.main()
