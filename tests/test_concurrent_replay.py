"""Concurrent replay safety for the flat v5 router (P2-3 closure).

The queued-vs-existing determination is a best-effort pre-query; under
concurrent replay two callers can both see "not pre-existing" and both
dispatch. The contract that must hold is:

1. Neither caller is denied or errors (no routing corruption).
2. The host's idempotency-key uniqueness is the final arbiter: exactly one
   task row survives, and the second caller is told the truth by the host.

This test simulates a real SQLite-backed host tasks table (unique
idempotency_key) and runs two threads through the router at the same time.
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
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
spec = importlib.util.spec_from_file_location("cost_router_concurrent_v5", PLUGIN)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

_ROUTE_TABLE = {
    "flash": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-flash", "worker_profile": "worker-flash", "pricing": {"input_per_million_usd": 0.0, "output_per_million_usd": 0.0}, "max_output_tokens": 65536, "budget_fallbacks": []},
    "luna": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-flash", "worker_profile": "worker-luna", "pricing": {"input_per_million_usd": 0.0, "output_per_million_usd": 0.0}, "max_output_tokens": 12000, "budget_fallbacks": []},
    "terra": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-flash", "worker_profile": "worker-terra", "pricing": {"input_per_million_usd": 0.0, "output_per_million_usd": 0.0}, "max_output_tokens": 65536, "budget_fallbacks": []},
    "terra_pro": {"enabled": True, "provider": "custom:new-api", "model": "deepseek-v4-pro", "worker_profile": "worker-terra", "pricing": {"input_per_million_usd": 0.435, "output_per_million_usd": 0.87}, "max_output_tokens": 12000, "budget_fallbacks": []},
    "sol": {"enabled": True, "provider": "custom:new-api", "model": "gpt-5.6-sol", "worker_profile": "worker-sol", "pricing": {"input_per_million_usd": 0.9, "output_per_million_usd": 4.05}, "max_output_tokens": 6000, "budget_fallbacks": []},
}


def config():
    routes = {name: dict(entry) for name, entry in _ROUTE_TABLE.items()}
    return {"catalog_version": 5, "routes": routes,
            "reviewer_ledger_path": str(PLUGIN.parent / ".unit-concurrent-v5.sqlite3"),
            "llm": {"allow_provider_override": True, "allow_model_override": True,
                    "allowed_providers": ["custom:new-api"],
                    "allowed_models": [route["model"] for route in routes.values()]},
            "budget": {"max_cost_usd": 1, "max_tokens": 131072, "remaining_budget_usd": 1},
            "routing": {"keyword_fallback_enabled": False, "keyword_tiers": {}}}


class HostKanban:
    """Thread-safe SQLite-backed host with idempotency-key uniqueness."""

    def __init__(self, db_path):
        self.path = db_path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, "
            "status TEXT, assignee TEXT, model TEXT, provider TEXT)")
        self.conn.commit()
        self.seq = 0
        self.create_calls = 0

    def connect(self, board=None):
        # Real host returns a fresh connection per call; the router closes the
        # pre-query connection itself, so a shared handle would break create.
        conn = sqlite3.connect(self.path, check_same_thread=False)
        return conn

    def create_task(self, args):
        with self._lock:
            key = args["idempotency_key"]
            row = self.conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ?", (key,)).fetchone()
            if row is not None:
                # Host idempotency replay: same task, no `created` flag.
                return {"ok": True, "task_id": row[0], "status": "ready"}
            self.seq += 1
            task_id = f"t_conc_{self.seq}"
            self.conn.execute(
                "INSERT INTO tasks (id, idempotency_key, status, assignee, model, provider) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, key, "ready", args.get("assignee"), args.get("model"),
                 args.get("provider")))
            self.conn.commit()
            self.create_calls += 1
            return {"ok": True, "task_id": task_id, "status": "ready"}


class Context:
    def __init__(self, host):
        self.host = host
        self.calls = []

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        if name == "kanban_create":
            return json.dumps(self.host.create_task(args))
        return json.dumps({"ok": True})


class ConcurrentReplayTests(unittest.TestCase):
    def test_concurrent_same_key_no_denied_and_single_surviving_task(self):
        tmp = tempfile.TemporaryDirectory(dir=PLUGIN.parent)
        try:
            host = HostKanban(str(Path(tmp.name) / "host.sqlite3"))
            # Patch the router's kanban pre-query to the same host DB so both
            # threads race on a real (unlocked SELECT) table.
            with patch.object(router, "_plugin_config", return_value=config()), \
                 patch.dict(sys.modules, {"hermes_cli.kanban_db": types.SimpleNamespace(connect=host.connect)}):
                barrier = threading.Barrier(2)
                results = []
                errors = []

                def call_once():
                    ctx = Context(host)
                    try:
                        barrier.wait(timeout=5)
                        result = json.loads(router._handler(ctx, {
                            "goal": "architecture", "route": "terra",
                            "idempotency_key": "conc-replay-key-1",
                        }))
                        results.append((result, ctx))
                    except Exception as exc:  # pragma: no cover - diagnostic
                        errors.append(exc)

                threads = [threading.Thread(target=call_once) for _ in range(2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=15)

            self.assertEqual([], errors, f"concurrent dispatch raised: {errors}")
            self.assertEqual(2, len(results))
            # No denial, no routing corruption.
            for result, _ in results:
                self.assertIn(result["routing_status"], ("queued", "existing"))
                self.assertNotEqual("denied", result["routing_status"])
                self.assertEqual("terra", result["route"])
            # Host idempotency is the final arbiter: exactly one task row
            # survives for the replayed key (router re-hashes the supplied key
            # into a canonical `ruoyu-cost-router:<sha256>` form).
            rows = host.conn.execute("SELECT id, idempotency_key FROM tasks").fetchall()
            self.assertEqual(1, len(rows), f"host allowed duplicate tasks: {rows}")
            self.assertTrue(rows[0][1].startswith("ruoyu-cost-router:"),
                            f"unexpected idempotency key form: {rows[0][1]}")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
