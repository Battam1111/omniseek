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

    def test_survival_canonical_artifacts_are_byte_identical_when_present(self):
        survival_root = ROOT.parent / "survival_28597ac_fresh"
        # The guard is the FILES, not the directory. A gutted leftover checkout (RK's dissolution
        # left one on the mini with empty config/ and schemas/) satisfies is_dir() and then crashes
        # here with FileNotFoundError, turning "no sibling to compare against" into a red test. The
        # comparison is opportunistic by design; its precondition must be too.
        survival_schema = survival_root / "schemas" / SCHEMA_PATH.name
        survival_policy = survival_root / "config" / POLICY_PATH.name
        if not (survival_schema.is_file() and survival_policy.is_file()):
            self.skipTest("Survival sibling checkout is not present")

        self.assertEqual(SCHEMA_PATH.read_bytes(), survival_schema.read_bytes())
        self.assertEqual(POLICY_PATH.read_bytes(), survival_policy.read_bytes())


if __name__ == "__main__":
    unittest.main()
