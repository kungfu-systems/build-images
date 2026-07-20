#!/usr/bin/env python3
"""Fail-closed tests for the formal-performance distribution contract."""

from __future__ import annotations

import errno
import importlib.machinery
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


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
PREPARATION = ROOT / "pilots/comparator/scripts/prepare_formal_performance.py"
KUNGFU_DRIVER = (
    ROOT
    / "images/comparator-formal-runner/opt/formal-performance/drivers/kungfu/formal_performance_fixture.cpp"
)
AERON_DRIVER = (
    ROOT
    / "images/comparator-formal-runner/opt/formal-performance/drivers/aeron/src/io/kungfu/aeron/FormalPerformanceHarness.java"
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
PREPARATION_LOADER = importlib.machinery.SourceFileLoader(
    "prepare_formal_performance", str(PREPARATION)
)
PREPARATION_SPEC = importlib.util.spec_from_loader(
    "prepare_formal_performance", PREPARATION_LOADER
)
if PREPARATION_SPEC is None:
    raise RuntimeError("cannot load formal-performance preparation")
PREPARATION_MODULE = importlib.util.module_from_spec(PREPARATION_SPEC)
PREPARATION_LOADER.exec_module(PREPARATION_MODULE)
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

    def test_exact_image_is_normalized_to_docker_repo_digest(self) -> None:
        digest = "sha256:" + "a" * 64
        cases = {
            f"postgres:18.4-bookworm@{digest}": f"postgres@{digest}",
            (
                f"registry.example:5000/team/image:release@{digest}"
            ): f"registry.example:5000/team/image@{digest}",
            f"ghcr.io/team/image@{digest}": f"ghcr.io/team/image@{digest}",
        }
        for image, expected in cases.items():
            with self.subTest(image=image):
                self.assertEqual(
                    PREPARATION_MODULE.canonical_repo_digest(image),
                    expected,
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

    def test_matched_timeout_is_symmetric_for_durable_sync_workloads(
        self,
    ) -> None:
        for candidate in ("kungfu", "aeron"):
            with self.subTest(candidate=candidate):
                request = {
                    "candidate": candidate,
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

    def test_matched_soak_rate_is_frozen_and_symmetric(self) -> None:
        self.assertEqual(
            self.plan["matched_lane"]["soak_messages_per_second"],
            10000,
        )
        drifted = json.loads(json.dumps(self.plan))
        drifted["matched_lane"]["soak_messages_per_second"] = 10001
        with self.assertRaisesRegex(
            FORMAL.FormalError, "Kungfu/Aeron matched contract drifted"
        ):
            FORMAL.validate_plan(drifted)

        request = {
            "candidate": "kungfu",
            "variant": {"workload": "soak"},
            "soak_messages_per_second": 10000,
        }
        self.assertEqual(
            PROVIDER_MODULE.matched_arguments(request),
            (0, 60, 10000),
        )
        self.assertIn(
            "std::this_thread::sleep_until(target);",
            KUNGFU_DRIVER.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "LockSupport.parkNanos(remainingNs);",
            AERON_DRIVER.read_text(encoding="utf-8"),
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

    def test_sampler_skips_a_cgroup_removed_during_container_restart(self) -> None:
        sampler = PROVIDER_MODULE.CgroupSampler("fp0007-pg", "postgres")
        container = {
            "State": {"Pid": 1234, "Running": True},
            "Config": {
                "Labels": {"com.docker.compose.service": "postgres"}
            },
            "HostConfig": {"NanoCpus": 2_000_000_000, "Memory": 2_147_483_648},
        }
        with (
            mock.patch.object(PROVIDER_MODULE, "docker_ids", return_value=["abc"]),
            mock.patch.object(
                PROVIDER_MODULE, "inspect_containers", return_value=[container]
            ),
            mock.patch.object(
                PROVIDER_MODULE,
                "cgroup_path",
                side_effect=FileNotFoundError("removed"),
            ),
        ):
            sampler.sample()
        self.assertEqual(sampler.samples, [])

    def test_sampler_skips_cgroup_enodev_during_container_restart(self) -> None:
        sampler = PROVIDER_MODULE.CgroupSampler("fp0007-pg", "postgres")
        container = {
            "State": {"Pid": 1234, "Running": True},
            "Config": {
                "Labels": {"com.docker.compose.service": "postgres"}
            },
            "HostConfig": {"NanoCpus": 2_000_000_000, "Memory": 2_147_483_648},
        }
        with (
            mock.patch.object(PROVIDER_MODULE, "docker_ids", return_value=["abc"]),
            mock.patch.object(
                PROVIDER_MODULE, "inspect_containers", return_value=[container]
            ),
            mock.patch.object(
                PROVIDER_MODULE,
                "cgroup_path",
                return_value=pathlib.Path("/sys/fs/cgroup/restarting.scope"),
            ),
            mock.patch.object(
                PROVIDER_MODULE,
                "io_bytes",
                side_effect=OSError(errno.ENODEV, "No such device"),
            ),
        ):
            sampler.sample()
        self.assertEqual(sampler.samples, [])

    def test_sampler_skips_esrch_when_container_process_exits(self) -> None:
        sampler = PROVIDER_MODULE.CgroupSampler("fp0007-pg", "postgres")
        container = {
            "State": {"Pid": 1234, "Running": True},
            "Config": {
                "Labels": {"com.docker.compose.service": "postgres"}
            },
            "HostConfig": {"NanoCpus": 2_000_000_000, "Memory": 2_147_483_648},
        }
        with (
            mock.patch.object(PROVIDER_MODULE, "docker_ids", return_value=["abc"]),
            mock.patch.object(
                PROVIDER_MODULE, "inspect_containers", return_value=[container]
            ),
            mock.patch.object(
                PROVIDER_MODULE,
                "cgroup_path",
                side_effect=ProcessLookupError(errno.ESRCH, "No such process"),
            ),
        ):
            sampler.sample()
        self.assertEqual(sampler.samples, [])

    def test_sampler_rejects_unexpected_cgroup_io_errors(self) -> None:
        sampler = PROVIDER_MODULE.CgroupSampler("fp0007-pg", "postgres")
        container = {
            "State": {"Pid": 1234, "Running": True},
            "Config": {
                "Labels": {"com.docker.compose.service": "postgres"}
            },
            "HostConfig": {"NanoCpus": 2_000_000_000, "Memory": 2_147_483_648},
        }
        with (
            mock.patch.object(PROVIDER_MODULE, "docker_ids", return_value=["abc"]),
            mock.patch.object(
                PROVIDER_MODULE, "inspect_containers", return_value=[container]
            ),
            mock.patch.object(
                PROVIDER_MODULE,
                "cgroup_path",
                return_value=pathlib.Path("/sys/fs/cgroup/restarting.scope"),
            ),
            mock.patch.object(
                PROVIDER_MODULE,
                "io_bytes",
                side_effect=OSError(errno.EIO, "Input/output error"),
            ),
        ):
            with self.assertRaises(OSError) as raised:
                sampler.sample()
        self.assertEqual(raised.exception.errno, errno.EIO)

    def test_sampler_accounts_for_reused_cgroup_path_by_lifecycle(self) -> None:
        sampler = PROVIDER_MODULE.CgroupSampler("fp0007-pg", "postgres")
        first_lifecycle = {
            "Id": "a" * 64,
            "State": {
                "Pid": 1234,
                "Running": True,
                "StartedAt": "2026-07-19T10:00:00.000000000Z",
            },
            "Config": {
                "Labels": {"com.docker.compose.service": "postgres"}
            },
            "HostConfig": {
                "NanoCpus": 2_000_000_000,
                "Memory": 2_147_483_648,
            },
        }
        second_lifecycle = {
            **first_lifecycle,
            "State": {
                "Pid": 5678,
                "Running": True,
                "StartedAt": "2026-07-19T10:00:01.000000000Z",
            },
        }
        root = pathlib.Path("/sys/fs/cgroup/reused-container.scope")
        with (
            mock.patch.object(
                PROVIDER_MODULE, "docker_ids", return_value=["container"]
            ),
            mock.patch.object(
                PROVIDER_MODULE,
                "inspect_containers",
                side_effect=[
                    [first_lifecycle],
                    [first_lifecycle],
                    [second_lifecycle],
                    [second_lifecycle],
                ],
            ),
            mock.patch.object(PROVIDER_MODULE, "cgroup_path", return_value=root),
            mock.patch.object(
                PROVIDER_MODULE,
                "io_bytes",
                side_effect=[(100, 200), (150, 260), (5, 10), (20, 40)],
            ),
            mock.patch.object(
                PROVIDER_MODULE,
                "cpu_usage_ns",
                side_effect=[1000, 1800, 100, 500],
            ),
            mock.patch.object(
                PROVIDER_MODULE, "scalar", return_value=1024
            ),
            mock.patch.object(
                PROVIDER_MODULE, "rss_bytes", return_value=512
            ),
        ):
            for _ in range(4):
                sampler.sample()
        result = sampler.result(
            {
                "subject": {
                    "cpu_micros": 2_000_000,
                    "memory_bytes": 2_147_483_648,
                },
                "runner": {
                    "cpu_micros": 1_000_000,
                    "memory_bytes": 1_073_741_824,
                },
            }
        )
        self.assertEqual(result["cpu_usage_ns"], 1200)
        self.assertEqual(result["read_bytes"], 65)
        self.assertEqual(result["write_bytes"], 90)
        self.assertEqual(
            [sample["cgroups"][0]["pid"] for sample in sampler.samples],
            [1234, 1234, 5678, 5678],
        )

    def test_cleanup_falls_back_to_project_labeled_resources(self) -> None:
        compose_down = mock.Mock(returncode=1, stdout="", stderr="busy")
        residue = {
            "containers": ["container-id"],
            "networks": ["network-id"],
            "volumes": ["volume-id"],
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                PROVIDER_MODULE, "run_text", return_value=compose_down
            ),
            mock.patch.object(
                PROVIDER_MODULE,
                "project_residue",
                side_effect=[residue, {"containers": [], "networks": [], "volumes": []}],
            ),
            mock.patch.object(
                PROVIDER_MODULE, "remove_project_resources"
            ) as remove,
        ):
            evidence = pathlib.Path(directory) / "cleanup.json"
            result = PROVIDER_MODULE.cleanup_project(
                ROOT, "fp0007-pg", "postgres", {}, evidence_path=evidence
            )
            remove.assert_called_once_with(residue)
            self.assertEqual(
                result, {"containers": [], "networks": [], "volumes": []}
            )
            cleanup = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertTrue(cleanup["fallback_applied"])
            self.assertEqual(cleanup["compose_down"]["returncode"], 1)

    def test_sampling_failure_terminates_and_reaps_adapter_process(self) -> None:
        process = mock.Mock(pid=4321)
        process.poll.side_effect = [None, None]
        process.communicate.return_value = ("partial stdout", "partial stderr")
        sampler = mock.Mock(samples=[])
        sampler.sample.side_effect = PROVIDER_MODULE.ProviderError(
            "cgroup disappeared"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                PROVIDER_MODULE.subprocess, "Popen", return_value=process
            ),
            mock.patch.object(
                PROVIDER_MODULE, "CgroupSampler", return_value=sampler
            ),
            mock.patch.object(PROVIDER_MODULE.os, "killpg") as killpg,
        ):
            diagnostic_dir = pathlib.Path(directory)
            with self.assertRaisesRegex(
                PROVIDER_MODULE.ProviderError, "cgroup disappeared"
            ):
                PROVIDER_MODULE.measured_process(
                    ["adapter"],
                    project="fp0007-pg",
                    subject_service="postgres",
                    limits={
                        "subject": {
                            "cpu_micros": 2_000_000,
                            "memory_bytes": 2_147_483_648,
                        },
                        "runner": {
                            "cpu_micros": 1_000_000,
                            "memory_bytes": 1_073_741_824,
                        },
                    },
                    cwd=ROOT,
                    env={},
                    timeout_seconds=60,
                    diagnostic_dir=diagnostic_dir,
                )
            killpg.assert_called_once_with(4321, PROVIDER_MODULE.signal.SIGKILL)
            process.communicate.assert_called_once_with()
            self.assertEqual(
                (diagnostic_dir / "driver.stderr.log").read_text(
                    encoding="utf-8"
                ),
                "partial stderr",
            )

    def test_process_exit_race_preserves_sampling_failure(self) -> None:
        process = mock.Mock(pid=4321)
        process.poll.side_effect = [None, None]
        process.communicate.return_value = ("partial stdout", "partial stderr")
        sampler = mock.Mock(samples=[])
        sampler.sample.side_effect = PROVIDER_MODULE.ProviderError(
            "cgroup sampling failed"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                PROVIDER_MODULE.subprocess, "Popen", return_value=process
            ),
            mock.patch.object(
                PROVIDER_MODULE, "CgroupSampler", return_value=sampler
            ),
            mock.patch.object(
                PROVIDER_MODULE.os,
                "killpg",
                side_effect=ProcessLookupError("process group exited"),
            ) as killpg,
        ):
            with self.assertRaisesRegex(
                PROVIDER_MODULE.ProviderError, "cgroup sampling failed"
            ):
                PROVIDER_MODULE.measured_process(
                    ["adapter"],
                    project="fp0007-pg",
                    subject_service="postgres",
                    limits={
                        "subject": {
                            "cpu_micros": 2_000_000,
                            "memory_bytes": 2_147_483_648,
                        },
                        "runner": {
                            "cpu_micros": 1_000_000,
                            "memory_bytes": 1_073_741_824,
                        },
                    },
                    cwd=ROOT,
                    env={},
                    timeout_seconds=60,
                    diagnostic_dir=pathlib.Path(directory),
                )
            killpg.assert_called_once_with(4321, PROVIDER_MODULE.signal.SIGKILL)
            process.communicate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
