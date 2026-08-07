"""v4 config coverage for Sol authority helper behavior."""
import importlib.util
import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

agent = types.ModuleType("agent"); redact = types.ModuleType("agent.redact"); redact.redact_sensitive_text = lambda value, force=False: value
sys.modules["agent"] = agent; sys.modules["agent.redact"] = redact
hermes_cli = types.ModuleType("hermes_cli"); config_mod = types.ModuleType("hermes_cli.config"); config_mod.load_config = lambda: {}
sys.modules["hermes_cli"] = hermes_cli; sys.modules["hermes_cli.config"] = config_mod
PLUGIN = Path(__file__).resolve().parents[1] / "ruoyu-cost-router" / "__init__.py"; spec = importlib.util.spec_from_file_location("cost_router_f3_v4", PLUGIN); router = importlib.util.module_from_spec(spec); spec.loader.exec_module(router)


class ControllerAuthorityF3Tests(unittest.TestCase):
    def test_re_review_key_is_deterministic_and_changes_for_semantic_input(self):
        digest = "sha256:" + hashlib.sha256(b"prior").hexdigest(); base = "sha256:" + hashlib.sha256(b"base").hexdigest(); artifact = {"logical_id": "repair", "sha256": "sha256:" + hashlib.sha256(b"repair").hexdigest(), "byte_count": 6}
        first = router._re_review_key(base_semantic_review_key=base, prior_review_digest=digest, accepted_finding_ids=["F1"], addressed_finding_ids=["F1"], repaired_logical_artifacts=[artifact])
        self.assertEqual(first, router._re_review_key(base_semantic_review_key=base, prior_review_digest=digest, accepted_finding_ids=["F1"], addressed_finding_ids=["F1"], repaired_logical_artifacts=[artifact]))
        self.assertNotEqual(first, router._re_review_key(base_semantic_review_key=base, prior_review_digest=digest, accepted_finding_ids=["F1", "F2"], addressed_finding_ids=["F1"], repaired_logical_artifacts=[artifact]))


if __name__ == "__main__": unittest.main()
