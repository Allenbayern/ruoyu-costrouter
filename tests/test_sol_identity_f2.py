"""F2 regression tests for semantic review identity and scoped re-review keys."""

import importlib.util
import hashlib
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
config = types.ModuleType("hermes_cli.config")
config.load_config = lambda: {}
sys.modules["hermes_cli"] = hermes_cli
sys.modules["hermes_cli.config"] = config

PLUGIN = Path(__file__).resolve().parents[1] / "ruoyu-cost-router" / "__init__.py"
spec = importlib.util.spec_from_file_location("cost_router_f2", PLUGIN)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)


def sha(label):
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def initial_identity(**overrides):
    identity = {
        "logical_artifacts": [
            {"logical_id": "artifact-a", "sha256": sha("artifact-a"), "byte_count": 17},
            {"logical_id": "artifact-b", "sha256": sha("artifact-b"), "byte_count": 23},
        ],
        "acceptance_criteria_version": "criteria-v1",
        "acceptance_criteria_sha256": sha("criteria-v1"),
    }
    identity.update(overrides)
    return identity


class SemanticReviewIdentityF2Tests(unittest.TestCase):
    def test_initial_semantic_key_is_provenance_independent_and_order_invariant(self):
        identity = initial_identity()
        reordered = initial_identity(logical_artifacts=list(reversed(identity["logical_artifacts"])))
        provenance_variants = (
            {"task_id": "t_1", "run_id": "r_1", "profile": "worker-terra", "path": "/tmp/a"},
            {"task_id": "t_99", "run_id": "r_99", "profile": "worker-luna", "path": "/other/b"},
        )
        expected = router._semantic_review_key(identity)
        self.assertEqual(expected, router._semantic_review_key(reordered))
        for provenance in provenance_variants:
            with self.subTest(provenance=provenance):
                self.assertEqual(expected, router._semantic_review_key(identity, provenance=provenance))

    def test_initial_semantic_key_changes_only_for_semantic_inputs(self):
        baseline = initial_identity()
        expected = router._semantic_review_key(baseline)
        cases = (
            ("artifact digest", initial_identity(logical_artifacts=[
                {"logical_id": "artifact-a", "sha256": sha("changed"), "byte_count": 17},
                baseline["logical_artifacts"][1],
            ])),
            ("artifact byte count", initial_identity(logical_artifacts=[
                {"logical_id": "artifact-a", "sha256": sha("artifact-a"), "byte_count": 18},
                baseline["logical_artifacts"][1],
            ])),
            ("criteria version", initial_identity(acceptance_criteria_version="criteria-v2")),
            ("criteria digest", initial_identity(acceptance_criteria_sha256=sha("criteria-v2"))),
        )
        for name, identity in cases:
            with self.subTest(name=name):
                self.assertNotEqual(expected, router._semantic_review_key(identity))

    def test_malformed_initial_identity_fails_closed(self):
        duplicate = initial_identity(logical_artifacts=[
            {"logical_id": "artifact-a", "sha256": sha("one"), "byte_count": 1},
            {"logical_id": "artifact-a", "sha256": sha("two"), "byte_count": 2},
        ])
        cases = (
            ("duplicate logical id", duplicate),
            ("invalid byte count", initial_identity(logical_artifacts=[
                {"logical_id": "artifact-a", "sha256": sha("artifact-a"), "byte_count": 0},
            ])),
            ("missing criteria digest", initial_identity(acceptance_criteria_sha256="bad")),
        )
        for name, identity in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "identity"):
                    router._semantic_review_key(identity)

    def test_re_review_key_is_finding_scoped_and_rejects_unchanged_or_unknown_inputs(self):
        base = router._semantic_review_key(initial_identity())
        repaired = [{"logical_id": "artifact-a", "sha256": sha("repaired"), "byte_count": 19}]
        expected = router._re_review_key(
            base_semantic_review_key=base,
            prior_review_digest=sha("prior-review"),
            accepted_finding_ids=["F2", "F1"],
            addressed_finding_ids=["F1", "F2"],
            repaired_logical_artifacts=repaired,
        )
        self.assertEqual(expected, router._re_review_key(
            base_semantic_review_key=base,
            prior_review_digest=sha("prior-review"),
            accepted_finding_ids=["F1", "F2"],
            addressed_finding_ids=["F2", "F1"],
            repaired_logical_artifacts=repaired,
        ))
        cases = (
            ("empty findings", [], ["F1"], repaired),
            ("unknown finding", ["F1"], ["F2"], repaired),
            ("unchanged repair", ["F1"], ["F1"], [{"logical_id": "artifact-a", "sha256": sha("prior-review"), "byte_count": 19}]),
        )
        for name, accepted, addressed, artifacts in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "re-review"):
                    router._re_review_key(
                        base_semantic_review_key=base,
                        prior_review_digest=sha("prior-review"),
                        accepted_finding_ids=accepted,
                        addressed_finding_ids=addressed,
                        repaired_logical_artifacts=artifacts,
                    )

    def test_initial_review_metadata_reserves_by_semantic_key_not_root_key(self):
        with tempfile.TemporaryDirectory(dir=PLUGIN.parent) as tmp:
            ledger = str(Path(tmp) / "admission.sqlite3")
            identity = initial_identity()
            review = {
                "role": "sol", "risk": "hard-L2", "stage": "protected_final",
                "root_key": "root-one", "producer_profiles": ["worker-terra"],
                "exclusions": ["worker-luna"], "review_kind": "initial", "review_identity": identity,
            }
            normalized = router._review_metadata({"review": review})
            connection = router._ledger_connection(Path(ledger))
            try:
                connection.execute("INSERT INTO reviewer_admissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("r-one", "unit", "sol", "root-one", "protected_final", "t_one", "queued", 1, 2_000_000_000))
                connection.execute("INSERT INTO reviewer_admission_semantics VALUES (?, ?, ?, ?)",
                    ("r-one", "unit", normalized["semantic_review_key"], "initial"))
                existing = connection.execute(
                    "SELECT admissions.reservation_id FROM reviewer_admissions AS admissions "
                    "JOIN reviewer_admission_semantics AS semantics "
                    "ON semantics.reservation_id=admissions.reservation_id "
                    "WHERE semantics.board=? AND semantics.semantic_review_key=?",
                    ("unit", normalized["semantic_review_key"]),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(("r-one",), existing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
