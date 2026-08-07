"""Focused tests for the host-atomic protected Sol review transport."""

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
kanban_db = types.ModuleType("hermes_cli.kanban_db")
hermes_cli.kanban_db = kanban_db
sys.modules["hermes_cli"] = hermes_cli
sys.modules["hermes_cli.config"] = config_mod
sys.modules["hermes_cli.kanban_db"] = kanban_db

PLUGIN = Path(__file__).resolve().parents[1] / "ruoyu-cost-router" / "__init__.py"
spec = importlib.util.spec_from_file_location("cost_router_atomic_f5", PLUGIN)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)


def hard_l2_review(source_path):
    return {
        "role": "sol", "risk": "hard-L2", "stage": "protected_final", "root_key": "root-f5",
        "review_kind": "initial",
        "review_identity": {
            "logical_artifacts": [{"logical_id": "artifact-1", "sha256": "sha256:" + "a" * 64, "byte_count": 100}],
            "acceptance_criteria_version": "criteria-v1", "acceptance_criteria_sha256": "sha256:" + "b" * 64,
        },
        "required_evidence_paths": [str(source_path)], "producer_profiles": ["worker-terra"],
        "exclusions": ["worker-luna"],
    }


def config(ledger):
    routes = {tier: {"enabled": True, "provider": "custom:new-api", "model": f"{tier}-model",
                     "worker_profile": f"worker-{tier}",
                     "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 1},
                     "max_output_tokens": 1000, "budget_fallbacks": []}
              for tier in ("flash", "luna", "terra", "terra_pro", "sol")}
    return {"catalog_version": 5, "reviewer_ledger_path": ledger, "routes": routes,
            "llm": {"allow_provider_override": True, "allow_model_override": True,
                    "allowed_providers": ["custom:new-api"], "allowed_models": [f"{tier}-model" for tier in routes]},
            "budget": {"max_cost_usd": 1, "max_tokens": 2000, "remaining_budget_usd": 1}}


class Context:
    def __init__(self): self.calls = []
    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"ok": True})


class FakeConnection:
    def close(self): pass


class FakeHost:
    def __init__(self): self.calls = []; self.tasks = {}; self.attachments = {}
    def connect(self, board=None): return FakeConnection()
    def close(self): pass
    def create(self, conn, **kwargs):
        self.calls.append(kwargs)
        task_id = "t_atomic"
        self.tasks[task_id] = types.SimpleNamespace(assignee=kwargs["assignee"], status="blocked")
        self.attachments[task_id] = [types.SimpleNamespace(filename=item["filename"], stored_path=self._write(item)) for item in kwargs["attachments"]]
        return task_id
    def _write(self, item):
        path = Path(self.tmp.name) / item["filename"]
        path.write_bytes(item["data"])
        return str(path)
    def get_task(self, conn, task_id): return self.tasks.get(task_id)
    def list_attachments(self, conn, task_id): return self.attachments.get(task_id, [])


class SolAtomicTransportF5Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=PLUGIN.parent)
        self.ledger = str(Path(self.tmp.name) / "ledger.sqlite3")
        self.ctx = Context()
        self.host = FakeHost(); self.host.tmp = self.tmp
        self.loader = patch.object(router, "_plugin_config", return_value=config(self.ledger)); self.loader.start()
        self.host_patch = patch.multiple(kanban_db, create=True, connect=self.host.connect,
                                        create_protected_sol_review_task=self.host.create,
                                        get_task=self.host.get_task, list_attachments=self.host.list_attachments)
        self.host_patch.start()
    def tearDown(self): self.host_patch.stop(); self.loader.stop(); self.tmp.cleanup()
    def invoke(self, review): return json.loads(router._handler(self.ctx, {"goal": "review", "board": "unit", "review": review}))
    def source(self):
        path = Path(self.tmp.name) / "source.txt"; path.write_text("source evidence", encoding="utf-8"); return path

    def test_calls_host_creator_once_and_never_legacy_tools(self):
        result = self.invoke(hard_l2_review(self.source()))
        self.assertEqual("queued", result["admission_status"])
        self.assertEqual("blocked", result["task_status"])
        self.assertEqual(1, len(self.host.calls))
        call = self.host.calls[0]
        self.assertEqual(("worker-sol", "hard-L2", "protected_final"), (call["assignee"], call["risk"], call["stage"]))
        self.assertEqual({"packet.json", "source-0.json"}, {item["filename"] for item in call["attachments"]})
        self.assertTrue(all(hashlib.sha256(item["data"]).hexdigest() == item["sha256"] and len(item["data"]) == item["byte_count"] for item in call["attachments"]))
        self.assertEqual([], self.ctx.calls, "protected path must not call legacy Kanban tools")

    def test_host_failure_denies_without_legacy_fallback(self):
        def fail(*args, **kwargs): raise ValueError("injected host failure")
        with patch.object(kanban_db, "create_protected_sol_review_task", fail):
            result = self.invoke(hard_l2_review(self.source()))
        self.assertEqual("deny_controller_handoff", result["admission_status"])
        self.assertEqual([], self.ctx.calls)
        self.assertEqual({}, self.host.tasks)

    def test_missing_host_creator_denies_without_legacy_fallback(self):
        with patch.object(kanban_db, "create_protected_sol_review_task", None):
            result = self.invoke(hard_l2_review(self.source()))
        self.assertEqual("deny_controller_handoff", result["admission_status"])
        self.assertEqual([], self.ctx.calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
