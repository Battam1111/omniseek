import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "src" / "omniseek" / "core" / "contracts"
SCHEMA_PATH = CONTRACTS / "scheduler-heartbeat-v1.json"
POLICY_PATH = CONTRACTS / "scheduler-heartbeat-policy-v1.json"


class SchedulerContractArtifactTests(unittest.TestCase):
    def test_packaged_schema_and_policy_are_bound_in_probe_mode(self):
        schema_bytes = SCHEMA_PATH.read_bytes()
        schema = json.loads(schema_bytes)
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["schema"]["const"],
            "omniseek.core-scheduler-heartbeat/v1",
        )
        self.assertEqual(schema["properties"]["phase"]["enum"], ["starting", "running"])
        self.assertEqual(policy["schema"], "omniseek.scheduler-heartbeat-policy/v1")
        self.assertEqual(policy["mode"], "calibration-probe")
        self.assertEqual(
            policy["heartbeat_schema_digest"], hashlib.sha256(schema_bytes).hexdigest()
        )
        for field in (
            "startup_grace_s",
            "stale_after_s",
            "recovery_deadline_s",
            "probe_timeout_s",
            "calibration_record_digest",
        ):
            self.assertIsNone(policy[field])

    def test_reference_canonical_artifacts_are_byte_identical_when_present(self):
        reference_root = ROOT.parent / "reference_sibling_checkout"
        # The guard is the FILES, not the directory. A gutted leftover checkout (a retired
        # project left one on the live host with empty config/ and schemas/) satisfies is_dir() and then crashes
        # here with FileNotFoundError, turning "no sibling to compare against" into a red test. The
        # comparison is opportunistic by design; its precondition must be too.
        reference_schema = reference_root / "schemas" / SCHEMA_PATH.name
        reference_policy = reference_root / "config" / POLICY_PATH.name
        if not (reference_schema.is_file() and reference_policy.is_file()):
            self.skipTest("Reference sibling checkout is not present")

        self.assertEqual(SCHEMA_PATH.read_bytes(), reference_schema.read_bytes())
        self.assertEqual(POLICY_PATH.read_bytes(), reference_policy.read_bytes())


if __name__ == "__main__":
    unittest.main()
