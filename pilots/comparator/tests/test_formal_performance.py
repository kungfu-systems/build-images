#!/usr/bin/env python3
"""Fail-closed tests for the formal-performance distribution contract."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
RUNNER = (
    ROOT
    / "images/comparator-formal-runner/opt/formal-performance/bin/formal-performance"
)
PROVIDER = RUNNER.with_name("formal-performance-provider")
PLAN = (
    ROOT
    / "images/comparator-formal-runner/opt/formal-performance/contracts/formal-performance-v1.json"
)
LOADER = importlib.machinery.SourceFileLoader("formal_performance", str(RUNNER))
SPEC = importlib.util.spec_from_loader("formal_performance", LOADER)
if SPEC is None:
    raise RuntimeError("cannot load formal-performance runner")
FORMAL = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(FORMAL)
PROVIDER_LOADER = importlib.machinery.SourceFileLoader(
    "formal_performance_provider", str(PROVIDER)
)
PROVIDER_SPEC = importlib.util.spec_from_loader(
    "formal_performance_provider", PROVIDER_LOADER
)
if PROVIDER_SPEC is None:
    raise RuntimeError("cannot load formal-performance provider")
PROVIDER_MODULE = importlib.util.module_from_spec(PROVIDER_SPEC)
PROVIDER_LOADER.exec_module(PROVIDER_MODULE)
RUNNER_IMAGE = (
    "ghcr.io/kungfu-systems/build-images/comparator-formal-runner@sha256:"
    + "a" * 64
)


class FormalPerformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = FORMAL.load_json(PLAN)

    def test_frozen_schedule_is_balanced_and_complete(self) -> None:
        schedule = FORMAL.expected_schedule(self.plan)
        self.assertEqual(len(schedule), 666)
        full_scored = [
            sample
            for sample in schedule
            if sample["lane"] == "full-stack" and sample["phase"] == "scored"
        ]
        self.assertEqual(len(full_scored), 315)
        for candidate in ("postgresql", "clickhouse", "kungfu"):
            positions = [
                sample["variant"]["position"]
                for sample in full_scored
                if sample["candidate"] == candidate
            ]
            self.assertEqual(
                {position: positions.count(position) for position in range(3)},
                {0: 35, 1: 35, 2: 35},
            )

    def test_bundle_and_offline_aggregate_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = pathlib.Path(directory) / "bundle"
            FORMAL.build_synthetic_bundle(bundle, RUNNER_IMAGE)
            verified = FORMAL.verify_bundle(bundle)
            self.assertFalse(verified["authority"]["winner_authority"])
            self.assertIsNone(verified["aggregates"]["winner"])

    def test_altered_artifact_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = pathlib.Path(directory) / "bundle"
            FORMAL.build_synthetic_bundle(bundle, RUNNER_IMAGE)
            run_path = bundle / "samples/0000/run.json"
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["timing"]["wall_ns"] += 1
            run_path.write_text(json.dumps(run), encoding="utf-8")
            with self.assertRaisesRegex(FORMAL.FormalError, "sample digest mismatch"):
                FORMAL.verify_bundle(bundle)

    def test_missing_extra_and_reordered_samples_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = pathlib.Path(directory) / "bundle"
            FORMAL.build_synthetic_bundle(bundle, RUNNER_IMAGE)
            manifest_path = bundle / "bundle.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["samples"][0], manifest["samples"][1] = (
                manifest["samples"][1],
                manifest["samples"][0],
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(FORMAL.FormalError, "reordered"):
                FORMAL.verify_bundle(bundle)

    def test_retry_residue_and_authority_widening_fail(self) -> None:
        mutations = (
            ("attempt", 2, "contract drifted"),
            ("cleanup", {"passed": False}, "residue"),
            ("authority", {"winner_authority": True}, "contract drifted"),
        )
        for field, value, message in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                bundle = pathlib.Path(directory) / "bundle"
                FORMAL.build_synthetic_bundle(bundle, RUNNER_IMAGE)
                run_path = bundle / "samples/0000/run.json"
                run = json.loads(run_path.read_text(encoding="utf-8"))
                run[field] = value
                run_path.write_text(
                    json.dumps(run, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                manifest_path = bundle / "bundle.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["samples"][0]["sha256"] = FORMAL.sha256_file(run_path)
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(FORMAL.FormalError, message):
                    FORMAL.verify_bundle(bundle)

    def test_production_provider_cannot_substitute_synthetic_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = pathlib.Path(directory) / "bundle"
            FORMAL.build_synthetic_bundle(bundle, RUNNER_IMAGE)
            manifest_path = bundle / "bundle.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["runner"]["image"] = (
                "ghcr.io/kungfu-systems/build-images/"
                "comparator-formal-runner@sha256:" + "b" * 64
            )
            manifest["runner"]["provider_sha256"] = FORMAL.sha256_file(
                FORMAL.PRODUCTION_PROVIDER
            )
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                FORMAL.FormalError, "raw cgroup-v2 samples"
            ):
                FORMAL.verify_bundle(bundle)

    def test_matched_timeout_covers_durable_sync_throughput(self) -> None:
        request = {
            "candidate": "kungfu",
            "variant": {
                "mode": "durable_sync",
                "workload": "throughput",
            },
        }
        self.assertEqual(
            PROVIDER_MODULE.matched_timeout_seconds(request),
            1800,
        )
        request["variant"]["workload"] = "recovery"
        self.assertEqual(
            PROVIDER_MODULE.matched_timeout_seconds(request),
            1800,
        )
        request["variant"]["mode"] = "visible"
        self.assertEqual(
            PROVIDER_MODULE.matched_timeout_seconds(request),
            600,
        )

    def test_recovery_metrics_must_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = pathlib.Path(directory) / "bundle"
            FORMAL.build_synthetic_bundle(bundle, RUNNER_IMAGE)
            run_path = bundle / "samples/0378/run.json"
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["correctness"]["metrics"]["recovery_ns"] += 1
            run_path.write_text(
                json.dumps(run, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest_path = bundle / "bundle.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["samples"][378]["sha256"] = FORMAL.sha256_file(run_path)
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                FORMAL.FormalError, "recovery metric relationship"
            ):
                FORMAL.verify_bundle(bundle)

    def test_zero_block_io_is_valid_for_a_cached_matched_sample(self) -> None:
        entry = FORMAL.expected_schedule(self.plan)[378]
        sample = FORMAL.synthetic_sample(entry, self.plan)
        sample["resources"]["start"]["read_bytes"] = 4096
        sample["resources"]["end"]["read_bytes"] = 4096
        sample["resources"]["read_bytes"] = 0
        FORMAL.validate_run(sample, entry, self.plan)


if __name__ == "__main__":
    unittest.main()
