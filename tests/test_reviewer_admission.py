"""v5 reviewer-admission ledger, reconciliation, and fail-closed regressions.

Sol hard-L2 is deliberately not queueable over the public Kanban transport.
Terra L1 remains the queueable reviewer path used to exercise ledger safety.
"""
import importlib.util
import json
import sqlite3
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
spec = importlib.util.spec_from_file_location("cost_router_admission_v5", PLUGIN)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)


def config(ledger):
    tier_to_model = {
        "flash": ("deepseek-v4-flash", "worker-flash"),
        "luna": ("deepseek-v4-flash", "worker-luna"),
        "terra": ("deepseek-v4-flash", "worker-terra"),
        "terra_pro": ("deepseek-v4-pro", "worker-terra"),
        "sol": ("gpt-5.6-sol", "worker-sol"),
    }
    routes = {}
    for tier, (model, worker) in tier_to_model.items():
        routes[tier] = {
            "enabled": True,
            "provider": "custom:new-api",
            "model": model,
            "worker_profile": worker,
            "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 1},
            "max_output_tokens": 1000,
            "budget_fallbacks": [],
        }

    return {
        "catalog_version": 5,
        "reviewer_ledger_path": ledger,
        "routes": routes,
        "llm": {
            "allow_provider_override": True,
            "allow_model_override": True,
            "allowed_providers": ["custom:new-api"],
            "allowed_models": [model for model, _ in tier_to_model.values()],
        },
        "budget": {"max_cost_usd": 1, "max_tokens": 2000, "remaining_budget_usd": 1},
    }


class Context:
    def __init__(self):
        self.calls = []
        self.next_id = 1
        self.task_states = {}
        self.ledger_path = None
        self.attachments = {}

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        if name == "kanban_show":
            task = self.task_states.get(args["task_id"])
            return json.dumps({"ok": True, "task": task}) if task is not None else json.dumps({"ok": False, "error": "not found"})
        elif name == "kanban_attach":
            import base64, os, tempfile
            task_id = args["task_id"]
            filename = args["filename"]
            content = base64.b64decode(args["content_base64"])
            if task_id not in self.attachments:
                self.attachments[task_id] = {}
            fd, path = tempfile.mkstemp(dir=self.ledger_path or '/tmp',
                                        prefix=f'attach_{task_id}_', suffix=f'_{filename}')
            try:
                os.write(fd, content)
                os.close(fd)
                self.attachments[task_id][filename] = {"path": path, "bytes": len(content)}
                return json.dumps({"ok": True})
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                raise
        elif name == "kanban_attachments":
            task_id = args["task_id"]
            if task_id not in self.attachments:
                return json.dumps({"ok": True, "attachments": []})
            atts = []
            for fname, info in self.attachments[task_id].items():
                atts.append({
                    "id": f"att_{len(atts)}",
                    "filename": fname,
                    "path": info["path"],
                    "content_type": "application/octet-stream",
                    "size": info["bytes"],
                    "created_at": 1700000000,
                })
            return json.dumps({"ok": True, "attachments": atts})
        elif name == "kanban_create":
            task_id = f"t_review_{self.next_id}"
            self.next_id += 1
            self.task_states[task_id] = {
                "id": task_id,
                "status": args.get("initial_status", "ready"),
                "body": args.get("body", ""),
                "assignee": args.get("assignee"),
                "board": args.get("board"),
                "workspace_kind": args.get("workspace_kind", "scratch"),
                "workspace_path": args.get("workspace_path"),
                "priority": args.get("priority", 0),
                "max_runtime_seconds": args.get("max_runtime_seconds", 900),
                "model_override": args.get("model"),
                "provider_override": args.get("provider"),
            }
            created = self.task_states[task_id]["status"] not in ("blocked", "reserved")
            return json.dumps({
                "ok": True,
                "created": created,
                "task_id": task_id,
                "status": self.task_states[task_id]["status"],
            })
        else:
            return json.dumps({"ok": True})


def review(role="luna", risk="low", stage="review", root="root-v5"):
    return {
        "role": role, "risk": risk, "stage": stage, "root_key": root,
        "producer_profiles": ["worker-terra"], "exclusions": ["worker-sol"],
    }


class ReviewerAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=PLUGIN.parent)
        self.ledger = str(Path(self.tmp.name) / "ledger.sqlite3")
        self.ctx = Context()
        self.ctx.ledger_path = self.tmp.name
        self.loader = patch.object(router, "_plugin_config", side_effect=lambda: config(self.ledger))
        self.loader.start()

    def tearDown(self):
        self.loader.stop()
        self.tmp.cleanup()

    def invoke(self, metadata, *, ctx=None):
        return json.loads(router._handler(ctx or self.ctx, {"goal": "review", "board": "unit", "review": metadata}))

    def queue_row(self, metadata, *, task_id="t_existing", updated_at=1):
        conn = router._ledger_connection(Path(self.ledger))
        try:
            conn.execute(
                "INSERT INTO reviewer_admissions VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                ("r_existing", "unit", metadata["role"], metadata["root_key"],
                 metadata["stage"], task_id, 2, updated_at),
            )
        finally:
            conn.close()

    def task_for(self, metadata, *, status="ready"):
        provenance = router._review_provenance(metadata, "r_existing")
        body = "Review provenance (protected router input):\n```json\n" + json.dumps(provenance, sort_keys=True) + "\n```\n"
        return {"id": "t_existing", "status": status, "body": body}

    def state(self):
        conn = sqlite3.connect(self.ledger)
        try:
            return conn.execute("SELECT state FROM reviewer_admissions WHERE reservation_id='r_existing'").fetchone()
        finally:
            conn.close()

    def only_admission_row(self):
        conn = sqlite3.connect(self.ledger)
        try:
            return conn.execute("SELECT role, state, task_id FROM reviewer_admissions").fetchone()
        finally:
            conn.close()

    def test_luna_admission_uses_v5_catalog(self):
        result = self.invoke(review())
        self.assertEqual("queued", result["admission_status"])
        self.assertEqual("worker-luna", self.ctx.calls[0][1]["assignee"])
        self.assertEqual(("luna", "queued", "t_review_1"), self.only_admission_row())

    def test_valid_queued_luna_admission_returns_existing_after_host_reconciliation(self):
        metadata = review()
        self.queue_row(metadata, updated_at=2_000_000_000)
        self.ctx.task_states["t_existing"] = self.task_for(metadata)
        result = self.invoke(metadata)
        self.assertEqual("existing", result["admission_status"])
        self.assertEqual(["kanban_show"], [name for name, _ in self.ctx.calls])
        self.assertEqual(("queued",), self.state())

    def test_producer_run_requires_route(self):
        metadata = review()
        metadata.pop("producer_profiles")
        metadata["producer_runs"] = [{
            "task_id": "t", "run_id": "r", "profile": "worker-terra",
            # Missing 'route' -- should fail
            "provider": "custom:new-api",
            "model": "deepseek-v4-flash",
            "artifact_ref": "/tmp/a", "artifact_digest": "sha256:" + "0" * 64,
        }]
        result = self.invoke(metadata)
        self.assertIn("required field 'route'", result["error"])
        self.assertEqual([], self.ctx.calls)

    def test_producer_run_with_route_reaches_artifact_verification(self):
        metadata = review()
        metadata.pop("producer_profiles")
        metadata["producer_runs"] = [{
            "task_id": "t", "run_id": "r", "profile": "worker-terra",
            "route": "terra", "provider": "custom:new-api",
            "model": "deepseek-v4-flash",
            "artifact_ref": "/tmp/a", "artifact_digest": "sha256:" + "0" * 64,
        }]
        result = self.invoke(metadata)
        self.assertIn("artifact_ref must resolve to a regular local file", result["error"])
        self.assertEqual([], self.ctx.calls)

    def test_producer_run_route_must_bind_verified_profile_provider_and_model(self):
        artifact = Path(self.tmp.name) / "producer.txt"
        artifact.write_text("producer", encoding="utf-8")
        digest = router._artifact_digest(str(artifact))
        metadata = review()
        metadata.pop("producer_profiles")
        metadata["artifact_ref"] = str(artifact)
        metadata["artifact_digest"] = digest
        metadata["review_identity"] = {
            "logical_artifacts": [],
            "acceptance_criteria_version": "1",
            "acceptance_criteria_sha256": "sha256:" + "0" * 64,
        }
        metadata["producer_runs"] = [{
            "task_id": "t_producer", "run_id": "r_producer",
            "profile": "worker-terra", "route": "terra",
            "provider": "custom:new-api",
            "model": "deepseek-v4-flash",
            "artifact_ref": str(artifact), "artifact_digest": digest,
        }]
        conn = router._ledger_connection(Path(self.ledger))
        try:
            conn.execute("INSERT INTO router_producers VALUES (?, ?, ?)", ("t_producer", "unit", 0))
        finally:
            conn.close()
        self.ctx.task_states["t_producer"] = {
            "id": "t_producer", "status": "completed", "assignee": "worker-terra",
            "model_override": "deepseek-v4-flash", "provider_override": "custom:new-api",
        }
        original = self.ctx.dispatch_tool

        def dispatch(name, args):
            if name == "kanban_show" and args["task_id"] == "t_producer":
                self.ctx.calls.append((name, args))
                return json.dumps({
                    "ok": True, "task": self.ctx.task_states["t_producer"],
                    "runs": [{"id": "r_producer", "profile": "worker-terra",
                              "status": "completed", "outcome": "completed"}],
                    "events": [{"kind": "completed", "run_id": "r_producer",
                                "payload": {"metadata": {"artifacts": [str(artifact)]}}}],
                })
            return original(name, args)

        self.ctx.dispatch_tool = dispatch
        # NOTE: Task id is t_review_1 because the mock Context assigns
        # sequential ids starting at 1 for newly created review tasks.
        # This previously asserted "t_producer" which was incorrect — the
        # result task_id is the *new* review task, not the producer run task.
        result = self.invoke(metadata)
        self.assertEqual("queued", result["admission_status"])
        self.assertIsInstance(result.get("task_id"), str)
        self.assertEqual("t_review_1", result.get("task_id"))

    def test_producer_run_variant_is_rejected_before_dispatch(self):
        metadata = review()
        metadata.pop("producer_profiles")
        metadata["producer_runs"] = [{
            "task_id": "t", "run_id": "r", "profile": "worker-terra",
            "route": "terra", "variant": "tier1_flash", "provider": "custom:new-api",
            "model": "gpt-5.6-terra", "artifact_ref": "/tmp/a",
            "artifact_digest": "sha256:" + "0" * 64,
        }]
        result = self.invoke(metadata)
        self.assertIn("unsupported fields: variant", result["error"])
        self.assertEqual([], self.ctx.calls)

    def test_missing_protected_metadata_denies_without_ledger_or_card(self):
        metadata = review()
        del metadata["risk"]
        result = self.invoke(metadata)
        self.assertEqual("denied", result["admission_status"])
        self.assertIn("missing protected metadata", result["error"])
        self.assertEqual([], self.ctx.calls)
        self.assertFalse(Path(self.ledger).exists())

    def test_illegal_and_legacy_roles_are_denied_without_card(self):
        for role in ("terra", "dspro"):
            with self.subTest(role=role):
                self.ctx.calls.clear()
                result = self.invoke(review(role=role))
                self.assertEqual("denied", result["admission_status"])
                self.assertIn("review.role", result["error"])
                self.assertEqual([], self.ctx.calls)

    def test_protected_final_review_is_sol_only_and_fails_closed(self):
        result = self.invoke(review(role="luna", risk="hard-L2", stage="protected_final"))
        self.assertEqual("denied", result["admission_status"])
        self.assertIn("Sol-only", result["error"])
        self.assertEqual([], self.ctx.calls)

    def test_hard_l2_sol_returns_controller_handoff_with_zero_cards(self):
        result = self.invoke(review(role="sol", risk="hard-L2", stage="protected_final"))
        self.assertEqual("deny_controller_handoff", result["admission_status"])
        self.assertEqual("deny_controller_handoff", result["controller_handoff_contract"]["decision"])
        self.assertFalse(result["controller_handoff_contract"]["fallback_allowed"])
        self.assertEqual([], self.ctx.calls)
        self.assertFalse(Path(self.ledger).exists())

    def test_stale_queued_terra_admission_is_quarantined_without_new_card(self):
        metadata = review()
        self.queue_row(metadata, updated_at=0)
        result = self.invoke(metadata)
        self.assertEqual("denied", result["admission_status"])
        self.assertIn("quarantined", result["error"])
        self.assertEqual([], [name for name, _ in self.ctx.calls if name == "kanban_create"])
        self.assertEqual(("quarantined",), self.state())

    def test_mismatched_queued_terra_admission_is_quarantined_without_new_card(self):
        metadata = review()
        self.queue_row(metadata, updated_at=2_000_000_000)
        mismatched = self.task_for(metadata)
        mismatched["body"] = mismatched["body"].replace('"root_key": "root-v5"', '"root_key": "wrong-root"')
        self.ctx.task_states["t_existing"] = mismatched
        result = self.invoke(metadata)
        self.assertEqual("denied", result["admission_status"])
        self.assertIn("quarantined", result["error"])
        self.assertEqual(["kanban_show"], [name for name, _ in self.ctx.calls])
        self.assertEqual(("quarantined",), self.state())

    def test_malformed_protected_metadata_denies_without_ledger_or_card(self):
        for field, value in (("role", []), ("role", False), ("risk", False),
                             ("stage", []), ("root_key", False),
                             ("producer_profiles", False), ("exclusions", [False])):
            with self.subTest(field=field, value=value):
                self.ctx.calls.clear()
                metadata = review()
                metadata[field] = value
                result = self.invoke(metadata)
                self.assertEqual("denied", result["admission_status"])
                self.assertEqual("denied", result["routing_status"])
                self.assertEqual([], self.ctx.calls)
                self.assertFalse(Path(self.ledger).exists())

    def test_terra_limit_counts_queued_without_auto_release(self):
        self.invoke(review(root="root-1"))
        self.invoke(review(root="root-2"))
        self.invoke(review(root="root-3"))
        result = self.invoke(review(root="root-4"))
        self.assertEqual("denied", result["admission_status"])
        self.assertIn("limit reached", result["error"])
        self.assertEqual(3, sum(name == "kanban_create" for name, _ in self.ctx.calls))

    def test_completed_legacy_reservation_backfills_provenance_and_releases_once(self):
        metadata = review(root="root-legacy")
        metadata["semantic_review_key"] = "semantic-legacy"
        metadata["review_kind"] = "initial"
        self.queue_row(metadata, task_id="t_legacy", updated_at=2_000_000_000)
        connection = router._ledger_connection(Path(self.ledger))
        try:
            connection.execute(
                "INSERT INTO reviewer_admission_semantics VALUES (?, ?, ?, ?)",
                ("r_existing", "unit", metadata["semantic_review_key"], metadata["review_kind"]),
            )
        finally:
            connection.close()
        provenance = router._review_provenance(metadata, "r_existing")
        self.ctx.task_states["t_legacy"] = {
            "id": "t_legacy", "status": "completed",
            "body": "Review provenance (protected router input):\n```json\n"
            + json.dumps(provenance, sort_keys=True) + "\n```\n",
        }

        result = self.invoke(review(root="root-after-legacy"))

        self.assertEqual("queued", result["admission_status"])
        connection = sqlite3.connect(self.ledger)
        try:
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM reviewer_admissions").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM reviewer_admission_semantics").fetchone()[0])
            self.assertEqual((router._canonical_json_digest(provenance),), connection.execute(
                "SELECT provenance_digest FROM reviewer_admission_provenance WHERE reservation_id='r_existing'"
            ).fetchone())
            self.assertEqual(("r_existing", "unit", "luna", "t_legacy"), connection.execute(
                "SELECT reservation_id, board, role, task_id FROM reviewer_admission_releases WHERE reservation_id='r_existing'"
            ).fetchone())
        finally:
            connection.close()

    def test_completed_legacy_reservation_accepts_canonical_host_envelope(self):
        metadata = review(root="root-envelope")
        metadata["semantic_review_key"] = "semantic-envelope"
        metadata["review_kind"] = "initial"
        self.queue_row(metadata, task_id="t_envelope", updated_at=2_000_000_000)
        connection = router._ledger_connection(Path(self.ledger))
        try:
            connection.execute(
                "INSERT INTO reviewer_admission_semantics VALUES (?, ?, ?, ?)",
                ("r_existing", "unit", metadata["semantic_review_key"], metadata["review_kind"]),
            )
        finally:
            connection.close()
        provenance = router._review_provenance(metadata, "r_existing")
        envelope = json.dumps({
            "assignee": "worker-sol", "protected_review": True,
            "risk": "hard-L2", "stage": "protected_final", "version": 1,
        }, sort_keys=True, separators=(",", ":"))
        self.ctx.task_states["t_envelope"] = {
            "id": "t_envelope", "status": "completed",
            "body": envelope + "\nReview provenance (protected router input):\n```json\n"
            + json.dumps(provenance, sort_keys=True) + "\n```\n",
        }

        result = self.invoke(review(root="root-after-envelope"))

        self.assertEqual("queued", result["admission_status"])
        connection = sqlite3.connect(self.ledger)
        try:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM reviewer_admission_provenance WHERE reservation_id='r_existing'"
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM reviewer_admission_releases WHERE reservation_id='r_existing'"
            ).fetchone()[0])
        finally:
            connection.close()

    def test_completed_legacy_envelope_variants_do_not_backfill_or_release(self):
        metadata = review(root="root-envelope-negative")
        metadata["semantic_review_key"] = "semantic-envelope-negative"
        metadata["review_kind"] = "initial"
        provenance = router._review_provenance(metadata, "r_existing")
        canonical = json.dumps({
            "assignee": "worker-sol", "protected_review": True,
            "risk": "hard-L2", "stage": "protected_final", "version": 1,
        }, sort_keys=True, separators=(",", ":"))
        variants = {
            "extra_field": json.dumps({**json.loads(canonical), "extra": 1}, sort_keys=True, separators=(",", ":")),
            "wrong_value": canonical.replace("worker-sol", "worker-terra"),
            "whitespace": json.dumps(json.loads(canonical), sort_keys=True),
            "inserted_line": canonical + "\ninserted",
            "misplaced_marker": "prefix\n" + canonical,
        }
        for name, envelope in variants.items():
            with self.subTest(name=name):
                self.ctx.calls.clear()
                self.tmp.cleanup()
                self.tmp = tempfile.TemporaryDirectory(dir=PLUGIN.parent)
                self.ledger = str(Path(self.tmp.name) / "ledger.sqlite3")
                self.ctx.ledger_path = self.tmp.name
                self.queue_row(metadata, task_id="t_envelope_negative", updated_at=2_000_000_000)
                connection = router._ledger_connection(Path(self.ledger))
                try:
                    connection.execute(
                        "INSERT INTO reviewer_admission_semantics VALUES (?, ?, ?, ?)",
                        ("r_existing", "unit", metadata["semantic_review_key"], metadata["review_kind"]),
                    )
                finally:
                    connection.close()
                self.ctx.task_states = {"t_envelope_negative": {
                    "id": "t_envelope_negative", "status": "completed",
                    "body": envelope + "\nReview provenance (protected router input):\n```json\n"
                    + json.dumps(provenance, sort_keys=True) + "\n```\n",
                }}
                result = self.invoke(review(root="root-after-envelope-negative"))
                self.assertEqual("queued", result["admission_status"])
                connection = sqlite3.connect(self.ledger)
                try:
                    self.assertEqual(0, connection.execute(
                        "SELECT COUNT(*) FROM reviewer_admission_provenance WHERE reservation_id='r_existing'"
                    ).fetchone()[0])
                    self.assertEqual(0, connection.execute(
                        "SELECT COUNT(*) FROM reviewer_admission_releases WHERE reservation_id='r_existing'"
                    ).fetchone()[0])
                finally:
                    connection.close()

    def test_invalid_legacy_completed_tasks_do_not_backfill_or_release_capacity(self):
        cases = {
            "parse_failure": lambda metadata, reservation_id: {
                "id": f"t_{reservation_id}", "status": "completed",
                "body": "Review provenance (protected router input):\n```json\nnot-json\n```\n",
            },
            "non_completed": lambda metadata, reservation_id: {
                "id": f"t_{reservation_id}", "status": "running",
                "body": "",
            },
            "task_mismatch": lambda metadata, reservation_id: {
                "id": f"wrong_{reservation_id}", "status": "completed",
                "body": "",
            },
            "lookup_failure": lambda metadata, reservation_id: {
                "id": f"t_{reservation_id}", "status": "completed", "body": "",
            },
            "malformed_body": lambda metadata, reservation_id: {
                "id": f"t_{reservation_id}", "status": "completed", "body": "not protected provenance",
            },
            "missing_semantic_binding": lambda metadata, reservation_id: {
                "id": f"t_{reservation_id}", "status": "completed",
                "body": "Review provenance (protected router input):\n```json\n"
                + json.dumps({key: value for key, value in router._review_provenance(metadata, reservation_id).items()
                              if key != "semantic_review_key"}, sort_keys=True) + "\n```\n",
            },
            "mismatched_root_key": lambda metadata, reservation_id: {
                "id": f"t_{reservation_id}", "status": "completed",
                "body": "Review provenance (protected router input):\n```json\n"
                + json.dumps({**router._review_provenance(metadata, reservation_id), "root_key": "wrong-root"}, sort_keys=True) + "\n```\n",
            },
        }
        for name, task_factory in cases.items():
            with self.subTest(name=name):
                self.ctx.calls.clear()
                self.ctx.task_states.clear()
                self.tmp.cleanup()
                self.tmp = tempfile.TemporaryDirectory(dir=PLUGIN.parent)
                self.ledger = str(Path(self.tmp.name) / "ledger.sqlite3")
                self.ctx.ledger_path = self.tmp.name
                for index in range(3):
                    metadata = review(root=f"root-invalid-{name}-{index}")
                    metadata["semantic_review_key"] = f"semantic-invalid-{name}-{index}"
                    metadata["review_kind"] = "initial"
                    reservation_id = f"r_invalid_{index}"
                    task_id = f"t_{reservation_id}"
                    connection = router._ledger_connection(Path(self.ledger))
                    try:
                        connection.execute(
                            "INSERT INTO reviewer_admissions VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                            (reservation_id, "unit", "luna", metadata["root_key"], metadata["stage"], task_id, 2, 2_000_000_000),
                        )
                        connection.execute(
                            "INSERT INTO reviewer_admission_semantics VALUES (?, ?, ?, ?)",
                            (reservation_id, "unit", metadata["semantic_review_key"], metadata["review_kind"]),
                        )
                    finally:
                        connection.close()
                    self.ctx.task_states[task_id] = task_factory(metadata, reservation_id)

                original = self.ctx.dispatch_tool
                if name == "lookup_failure":
                    def dispatch(tool_name, args):
                        if tool_name == "kanban_show":
                            raise RuntimeError("host unavailable")
                        return original(tool_name, args)
                    self.ctx.dispatch_tool = dispatch
                try:
                    result = self.invoke(review(root=f"root-after-invalid-{name}"))
                finally:
                    self.ctx.dispatch_tool = original

                self.assertEqual("denied", result["admission_status"])
                self.assertIn("limit reached", result["error"])
                connection = sqlite3.connect(self.ledger)
                try:
                    self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM reviewer_admission_provenance").fetchone()[0])
                    self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM reviewer_admission_releases").fetchone()[0])
                finally:
                    connection.close()

    def test_completed_queued_reservations_release_capacity_for_new_admission(self):
        admitted = []
        for index in range(1, 4):
            metadata = review(root=f"root-completed-{index}")
            result = self.invoke(metadata)
            self.assertEqual("queued", result["admission_status"])
            admitted.append((f"t_review_{index}", metadata, result["reservation_id"]))
        for task_id, metadata, reservation_id in admitted:
            provenance = router._review_provenance(metadata, reservation_id)
            self.ctx.task_states[task_id] = {
                "id": task_id,
                "status": "completed",
                "body": "Review provenance (protected router input):\n```json\n"
                + json.dumps(provenance, sort_keys=True) + "\n```\n",
            }
        result = self.invoke(review(root="root-after-release"))
        self.assertEqual("queued", result["admission_status"])

    def test_non_completed_reservations_remain_capacity_consuming(self):
        for status in ("blocked", "running", "ready", "todo", "archived", "unknown"):
            with self.subTest(status=status):
                self.ctx.calls.clear()
                self.tmp.cleanup()
                self.tmp = tempfile.TemporaryDirectory(dir=PLUGIN.parent)
                self.ledger = str(Path(self.tmp.name) / "ledger.sqlite3")
                self.ctx.ledger_path = self.tmp.name
                for index in range(1, 4):
                    metadata = review(root=f"root-{status}-{index}")
                    result = self.invoke(metadata)
                    self.assertEqual("queued", result["admission_status"])
                    provenance = router._review_provenance(metadata, result["reservation_id"])
                    self.ctx.task_states[result["task_id"]] = {
                        "id": result["task_id"], "status": status,
                        "body": "Review provenance (protected router input):\\n```json\\n"
                        + json.dumps(provenance, sort_keys=True) + "\\n```\\n",
                    }
                result = self.invoke(review(root=f"root-{status}-after"))
                self.assertEqual("denied", result["admission_status"])
                self.assertIn("limit reached", result["error"])

    def test_lookup_error_and_provenance_mismatch_do_not_release_capacity(self):
        admitted = []
        for index in range(1, 4):
            metadata = review(root=f"root-negative-{index}")
            result = self.invoke(metadata)
            admitted.append((metadata, result))
        for index, (metadata, result) in enumerate(admitted):
            if index == 0:
                continue
            provenance = router._review_provenance(metadata, result["reservation_id"])
            if index == 1:
                provenance["root_key"] = "wrong-root"
            self.ctx.task_states[result["task_id"]] = {
                "id": result["task_id"], "status": "completed",
                "body": "Review provenance (protected router input):\\n```json\\n"
                + json.dumps(provenance, sort_keys=True) + "\\n```\\n",
            }
        original = self.ctx.dispatch_tool

        def dispatch(name, args):
            if name == "kanban_show" and args["task_id"] == admitted[0][1]["task_id"]:
                raise RuntimeError("host unavailable")
            return original(name, args)

        self.ctx.dispatch_tool = dispatch
        result = self.invoke(review(root="root-negative-after"))
        self.assertEqual("denied", result["admission_status"])
        self.assertIn("limit reached", result["error"])

    def test_completed_release_reconciliation_is_idempotent(self):
        metadata = review(root="root-idempotent")
        result = self.invoke(metadata)
        provenance = router._review_provenance(metadata, result["reservation_id"])
        self.ctx.task_states[result["task_id"]] = {
            "id": result["task_id"], "status": "completed",
            "body": "Review provenance (protected router input):\n```json\n"
            + json.dumps(provenance, sort_keys=True) + "\n```\n",
        }
        self.assertTrue(router._completed_reservation_matches(
            self.ctx, result["task_id"], result["reservation_id"],
            router._canonical_json_digest(provenance), "unit",
        ))
        connection = router._ledger_connection(Path(self.ledger))
        try:
            connection.execute("BEGIN IMMEDIATE")
            router._reconcile_completed_reservations(self.ctx, connection, "unit", "luna")
            router._reconcile_completed_reservations(self.ctx, connection, "unit", "luna")
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM reviewer_admission_releases").fetchone()[0])
            self.assertEqual(("queued",), connection.execute(
                "SELECT state FROM reviewer_admissions WHERE reservation_id=?", (result["reservation_id"],)
            ).fetchone())
            connection.execute("COMMIT")
        finally:
            connection.close()

    def test_quarantines_when_post_create_ledger_write_fails(self):
        original = router._ledger_connection

        class FailingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.begins = 0

            def execute(self, statement, parameters=()):
                if statement == "BEGIN IMMEDIATE":
                    self.begins += 1
                    if self.begins == 2:
                        raise sqlite3.OperationalError("forced post-create failure")
                return self.connection.execute(statement, parameters)

            def rollback(self):
                return self.connection.rollback()

            def close(self):
                return self.connection.close()

        with patch.object(router, "_ledger_connection",
                          side_effect=lambda path: FailingConnection(original(path))):
            result = self.invoke(review())
        self.assertEqual("denied", result["admission_status"])
        self.assertIn("quarantined", result["error"])
        conn = sqlite3.connect(self.ledger)
        try:
            row = conn.execute("SELECT state FROM reviewer_admissions").fetchone()
        finally:
            conn.close()
        self.assertEqual(("quarantined",), row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
