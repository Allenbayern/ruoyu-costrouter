"""Sol admission keeps its public transport fail-closed behavior under v5."""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

agent = types.ModuleType("agent"); redact = types.ModuleType("agent.redact"); redact.redact_sensitive_text = lambda value, force=False: value
sys.modules["agent"] = agent; sys.modules["agent.redact"] = redact
hermes_cli = types.ModuleType("hermes_cli"); config_mod = types.ModuleType("hermes_cli.config"); config_mod.load_config = lambda: {}
sys.modules["hermes_cli"] = hermes_cli; sys.modules["hermes_cli.config"] = config_mod
sys.modules["tools"] = types.ModuleType("tools"); sys.modules["tools.kanban_tools"] = types.ModuleType("tools.kanban_tools")
PLUGIN = Path(__file__).resolve().parents[1] / "ruoyu-cost-router" / "__init__.py"; spec = importlib.util.spec_from_file_location("cost_router_sol_f1_v5", PLUGIN); router = importlib.util.module_from_spec(spec); spec.loader.exec_module(router)


def config(ledger, *, sol_enabled=True):
    models = {"flash": ("deepseek-v4-flash", "worker-flash"), "luna": ("deepseek-v4-flash", "worker-luna"),
              "terra": ("deepseek-v4-flash", "worker-terra"), "terra_pro": ("deepseek-v4-pro", "worker-terra"),
              "sol": ("gpt-5.6-sol", "worker-sol")}
    routes = {name: {"enabled": True, "provider": "custom:new-api", "model": model, "worker_profile": worker, "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 1}, "max_output_tokens": 1000, "budget_fallbacks": []} for name, (model, worker) in models.items()}; routes["sol"]["enabled"] = sol_enabled
    return {"catalog_version": 5, "reviewer_ledger_path": ledger, "routes": routes, "llm": {"allow_provider_override": True, "allow_model_override": True, "allowed_providers": ["custom:new-api"], "allowed_models": [model for model, _ in models.values()]}, "budget": {"max_cost_usd": 1, "max_tokens": 2000, "remaining_budget_usd": 1}}


class Context:
    def __init__(self): self.calls = []
    def dispatch_tool(self, name, args): self.calls.append((name, args)); return json.dumps({"ok": True, "created": True, "task_id": "unexpected"})


def hard_l2_review():
    return {"role": "sol", "risk": "hard-L2", "stage": "protected_final", "root_key": "root-f1", "producer_profiles": ["worker-terra"], "exclusions": ["worker-luna"]}


class SolAdmissionF1Tests(unittest.TestCase):
    def invoke(self, args, *, sol_enabled=True):
        with tempfile.TemporaryDirectory(dir=PLUGIN.parent) as tmp:
            ctx = Context()
            with patch.object(router, "_plugin_config", return_value=config(str(Path(tmp) / "ledger.sqlite3"), sol_enabled=sol_enabled)):
                result = json.loads(router._handler(ctx, {"goal": "review", **args}))
        return result, ctx

    def test_sol_without_review_is_denied_without_dispatch(self):
        result, ctx = self.invoke({"route": "sol"})
        self.assertEqual("denied", result["routing_status"]); self.assertIn("Sol admission", result["error"]); self.assertEqual([], ctx.calls)

    def test_disabled_sol_hard_l2_is_denied_without_dispatch(self):
        result, ctx = self.invoke({"review": hard_l2_review()}, sol_enabled=False)
        self.assertEqual("denied", result["routing_status"]); self.assertIn("disabled", result["error"]); self.assertEqual([], ctx.calls)

    def test_valid_hard_l2_still_returns_controller_handoff_without_dispatch(self):
        result, ctx = self.invoke({"review": hard_l2_review()})
        self.assertEqual("deny_controller_handoff", result["admission_status"])
        self.assertIn("lossless Sol packet transport", result["error"]); self.assertEqual([], ctx.calls)

    def test_under_budget_hard_l2_sol_denies_without_dispatch_or_fallback(self):
        result, ctx = self.invoke({"review": hard_l2_review(), "max_cost_usd": 0})
        self.assertEqual("denied", result["routing_status"])
        self.assertTrue(result["controller_decision_required"])
        self.assertFalse(result["controller_handoff_contract"]["fallback_allowed"])
        self.assertEqual([], ctx.calls)


if __name__ == "__main__": unittest.main()
