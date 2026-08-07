"""Controller-recovery admission must have independently verifiable provenance."""
import hashlib
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
spec = importlib.util.spec_from_file_location("cost_router_recovery", PLUGIN)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)


def config(ledger):
    models = {"flash": ("deepseek-v4-flash", "worker-flash"), "terra": ("gpt-5.6-terra", "worker-terra"), "sol": ("gpt-5.6-sol", "worker-sol")}
    routes = {tier: {"enabled": True, "provider": "custom:new-api", "model": model, "worker_profile": worker, "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 1}, "max_output_tokens": 1000, "budget_fallbacks": []} for tier, (model, worker) in models.items()}
    return {"catalog_version": 4, "reviewer_ledger_path": ledger, "routes": routes, "llm": {"allow_provider_override": True, "allow_model_override": True, "allowed_providers": ["custom:new-api"], "allowed_models": [model for model, _ in models.values()]}, "budget": {"max_cost_usd": 1, "max_tokens": 100000, "remaining_budget_usd": 1}}


class Context:
    def __init__(self): self.calls = []
    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"ok": True, "created": True, "task_id": "unexpected", "status": "ready"})


class ControllerRecoveryTests(unittest.TestCase):
    def _recorded_recovery(self, tmp, *, root_key="recovery-root"):
        artifact = Path(tmp) / "artifact.md"
        artifact.write_text("review artifact", encoding="utf-8")
        digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        ledger = Path(tmp) / "ledger.sqlite3"
        record = {"record_id": "record-1", "board": "unit", "root_key": root_key,
                  "controller_identity": "controller", "failed_task_id": "t_failed",
                  "recovery_reason": "interrupted", "artifact_ref": str(artifact),
                  "artifact_digest": digest, "created_at": 100, "expires_at": 200}
        router._record_controller_recovery_for_test(
            ledger, record, trusted_context=router._TEST_CONTROLLER_CONTEXT,
        )
        return ledger, artifact, digest

    def test_unrecorded_recovery_provenance_fails_closed_before_card_creation(self):
        with tempfile.TemporaryDirectory(dir=PLUGIN.parent) as tmp:
            artifact = Path(tmp) / "artifact.md"
            artifact.write_text("review artifact", encoding="utf-8")
            digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
            metadata = {"role": "sol", "risk": "hard-L2", "stage": "protected_final", "root_key": "recovery-root", "exclusions": ["worker-luna"], "controller_recovery": {"authority": "controller-recovery-v1", "authority_record_id": "missing-record", "failed_task_id": "t_failed", "recovery_reason": "worker interrupted", "artifact_ref": str(artifact), "artifact_digest": digest}}
            ctx = Context()
            with patch.object(router, "_plugin_config", return_value=config(str(Path(tmp) / "ledger.sqlite3"))):
                result = json.loads(router._handler(ctx, {"goal": "review", "board": "unit", "review": metadata}))
            self.assertEqual("deny_controller_handoff", result["admission_status"])
            self.assertIn("controller recovery", result["error"])
            self.assertEqual([], ctx.calls)

    def test_recovery_provenance_is_structurally_separate_from_worker_runs(self):
        recovery = {"authority": "controller-recovery-v1", "authority_record_id": "record-1", "failed_task_id": "t_failed", "recovery_reason": "interrupted", "artifact_ref": "/tmp/artifact", "artifact_digest": "sha256:" + "a" * 64}
        review = {"role": "sol", "risk": "hard-L2", "stage": "protected_final", "root_key": "recovery-root", "exclusions": ["worker-luna"], "controller_recovery": recovery}
        self.assertEqual(recovery, router._review_provenance(review, "r1")["controller_recovery"])
        self.assertNotIn("producer_runs", router._review_provenance(review, "r1"))

    def test_recorded_recovery_requires_exact_review_root_key(self):
        with tempfile.TemporaryDirectory(dir=PLUGIN.parent) as tmp:
            ledger, artifact, digest = self._recorded_recovery(tmp)
            metadata = {"role": "sol", "risk": "hard-L2", "stage": "protected_final",
                        "root_key": "recovery-root", "exclusions": ["worker-luna"],
                        "controller_recovery": {"authority": "controller-recovery-v1",
                            "authority_record_id": "record-1", "failed_task_id": "t_failed",
                            "recovery_reason": "interrupted", "artifact_ref": str(artifact),
                            "artifact_digest": digest}}
            self.assertTrue(router._verify_controller_recovery(ledger, metadata, "unit", now=150))
            metadata["root_key"] = "different-root"
            self.assertFalse(router._verify_controller_recovery(ledger, metadata, "unit", now=150))


if __name__ == "__main__":
    unittest.main()
