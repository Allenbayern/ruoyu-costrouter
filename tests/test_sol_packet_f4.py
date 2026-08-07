"""Sol review-packet preparation remains lossless and independent of catalog legacy shapes."""
import hashlib
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

agent = types.ModuleType("agent"); redact = types.ModuleType("agent.redact"); redact.redact_sensitive_text = lambda value, force=False: value
sys.modules["agent"] = agent; sys.modules["agent.redact"] = redact
hermes_cli = types.ModuleType("hermes_cli"); config_mod = types.ModuleType("hermes_cli.config"); config_mod.load_config = lambda: {}
sys.modules["hermes_cli"] = hermes_cli; sys.modules["hermes_cli.config"] = config_mod
PLUGIN = Path(__file__).resolve().parents[1] / "ruoyu-cost-router" / "__init__.py"; spec = importlib.util.spec_from_file_location("cost_router_f4_v4", PLUGIN); router = importlib.util.module_from_spec(spec); spec.loader.exec_module(router)


class SolPacketF4Tests(unittest.TestCase):
    def test_prepares_complete_manifest(self):
        with tempfile.TemporaryDirectory(dir=PLUGIN.parent) as tmp:
            packet = Path(tmp) / "packet.json"; source = Path(tmp) / "source.txt"; packet.write_bytes(b'{"packet":"review"}'); source.write_bytes(b"source evidence")
            key = "sha256:" + hashlib.sha256(b"semantic").hexdigest()
            prepared = router._prepare_review_packet({"version": 1, "semantic_review_key": key, "packet": {"logical_id": "packet", "attachment_id": "packet-1", "path": str(packet)}, "sources": [{"logical_id": "source", "attachment_id": "source-1", "path": str(source), "relevance": "required", "omission": "none"}], "input_tokens": 60000})
        self.assertEqual(key, prepared["semantic_review_key"])
        self.assertIsInstance(prepared["manifest"], str)
        self.assertGreater(len(prepared["manifest"]), 2)
        self.assertTrue(all(item["sha256"].startswith("sha256:") for item in prepared["sources"]))

    def test_rejects_missing_packet(self):
        with self.assertRaises(ValueError): router._prepare_review_packet({"version": 1, "semantic_review_key": "bad", "packet": {}, "sources": [], "input_tokens": 1})


if __name__ == "__main__": unittest.main()
