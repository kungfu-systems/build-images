from __future__ import annotations

import pathlib
import sys
import unittest

PILOT_DIR = pathlib.Path(__file__).resolve().parents[1]
ADAPTER_DIR = PILOT_DIR / "workload-adapters"
sys.path.insert(0, str(ADAPTER_DIR))

import aeron_phase_a_semantics as semantics  # noqa: E402


class AeronContainerSemanticsTests(unittest.TestCase):
    marker = "1" * 64

    def evidence(self, tier: str) -> dict:
        record = {
            "observed": 5,
            "duplicates": 0,
            "reordered": 0,
            "marker_mismatches": 0,
            "marker": self.marker,
        }
        replay = {
            "observed": 5,
            "duplicates": 0,
            "reordered": 0,
            "marker_mismatches": 0,
            "marker": self.marker,
        }
        value = {
            "tier": tier,
            "binding_sha256": self.marker,
            "live_health": True,
            "marker_bound": True,
            "records": [record],
            "replays": [replay],
        }
        if tier == "concurrent":
            value.update({"clients": 2, "records": [record, record], "replays": [replay, replay]})
        elif tier == "crash-recovery":
            value.update({"stale_health_rejected": True, "expiry_wait_seconds": 11})
        elif tier == "whole-root-restore":
            value.update({"root_restored": True, "backup_sha256": "2" * 64})
        elif tier == "schema-evolution":
            value.update({"envelope_versions": [1, 2], "records": [record, record], "replays": [replay, replay]})
        elif tier == "new-agent-takeover":
            value["fresh_client"] = True
        return value

    def test_every_frozen_tier_accepts_complete_record_replay_evidence(self) -> None:
        facts = {"expected_count": 5, "marker_policy": "binding-sha256"}
        for tier in sorted(semantics.TIERS):
            with self.subTest(tier=tier):
                verdict = semantics.derive_job_verdict(
                    semantics.JOB_IDS[0], facts, tier, self.evidence(tier)
                )
                self.assertEqual(verdict["state"], "ordered-complete")

    def test_crash_recovery_rejects_missing_stale_health_proof(self) -> None:
        evidence = self.evidence("crash-recovery")
        evidence["stale_health_rejected"] = False
        with self.assertRaisesRegex(semantics.SemanticError, "stale-health"):
            semantics.validate_tier_evidence("crash-recovery", evidence)

    def test_marker_substitution_is_rejected(self) -> None:
        evidence = self.evidence("normal")
        evidence["replays"][0]["marker"] = "3" * 64
        with self.assertRaisesRegex(semantics.SemanticError, "record/replay"):
            semantics.validate_tier_evidence("normal", evidence)

    def test_compose_uses_only_the_exact_published_kit(self) -> None:
        compose = (PILOT_DIR / "compose.yaml").read_text(encoding="utf-8")
        service = compose.split("  aeron:\n", 1)[1].split("  clickhouse:\n", 1)[0]
        self.assertNotIn("build:", service)
        self.assertIn(
            "ghcr.io/kungfu-systems/build-images/aeron-native-kit@sha256:"
            "684be4100866a3908020d1f368fe1100225a1101a65e9a0ee6d4da6a94963c76",
            service,
        )
        self.assertIn("aeron-native-harness\", \"health", service)

    def test_unscored_smoke_uses_the_managed_harness_contract(self) -> None:
        script = (PILOT_DIR / "scripts" / "pilot.sh").read_text(encoding="utf-8")
        self.assertNotIn("RecordedBasicPublisher", script)
        self.assertIn("aeron-native-harness", script)
        self.assertIn("aeron-record-receipt/v2", script)
        self.assertIn("aeron-replay-receipt/v2", script)


if __name__ == "__main__":
    unittest.main()
