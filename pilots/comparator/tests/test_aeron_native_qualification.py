from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


PILOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PILOT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import aeron_native_qualification as native  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64
IMAGE = f"ghcr.io/kungfu-systems/build-images/aeron-native-kit@sha256:{'1' * 64}"


def valid_plan() -> dict:
    return {
        "plan_schema": native.PLAN_SCHEMA_ID,
        "schema_version": 1,
        "test_only": False,
        "evidence_class": native.EVIDENCE_CLASS,
        "native_performance_authority_requested": True,
        "kit": {
            "image": IMAGE,
            "platform": "linux/amd64",
            "files": {
                "kit/kit-manifest.json": SHA_A,
                "kit/lib/aeron-all.jar": SHA_B,
                "kit/lib/aeron-qualification-harness.jar": SHA_A,
                "kit/bin/aeron-native-harness": SHA_B,
                "jre/release": SHA_A,
            },
            "source_files": {
                "images/aeron-native-kit/src/io/kungfu/aeron/QualificationHarness.java": SHA_A,
                "images/aeron-native-kit/bin/aeron-native-harness": SHA_B,
            },
        },
        "configuration": {
            "aeron_version": "1.52.2",
            "java_vendor": "Eclipse Adoptium",
            "java_version": "21.0.11",
            "ipc_channel": "aeron:ipc",
            "archive_control_channel": "aeron:udp?endpoint=localhost:8010",
            "archive_control_response_channel": "aeron:udp?endpoint=localhost:0",
            "archive_replication_channel": "aeron:udp?endpoint=localhost:0",
            "driver_threading": "SHARED_NETWORK",
            "archive_threading": "SHARED",
            "idle_strategy": "backoff-100-10-1-100000ns",
            "term_buffer_length": 16777216,
            "segment_file_length": 16777216,
            "sparse": False,
            "file_sync_level": 1,
            "catalog_sync_level": 1,
            "spies_simulate_connection": True,
            "receipt_timeout_seconds": 30,
            "coordinated_omission": "expected-interval-correction",
        },
        "host_policy": {
            "sudo_allowed": False,
            "host_tuning_allowed": False,
            "perf_counters": "optional-record-availability",
            "required_facts": [
                "cpu_topology", "governor", "energy_policy", "affinity", "load",
                "memory", "filesystem", "cgroup", "perf_event_paranoid",
            ],
            "interference_preflight": "record-load-no-mutation",
        },
        "calibration": {
            "repetitions": 3,
            "warmup": 10,
            "messages": 20,
            "payload": 64,
            "rate": 1000,
            "max_p99_drift_ratio": 0.5,
        },
        "ipc": {
            "repetitions": 3,
            "warmup": 10,
            "messages": 20,
            "payloads": [64],
            "rates": [1000],
        },
        "receipts": {
            "repetitions": 3,
            "messages": 5,
            "payload": 128,
            "modes": ["visible", "durable_group", "durable_sync"],
        },
        "recovery": {
            "repetitions": 3,
            "messages": 5,
            "payload": 128,
            "receipt": "durable_group",
            "expiry_wait_seconds": 11,
            "modes": ["crash-replay", "whole-root-restore"],
        },
        "soak": {
            "repetitions": 1,
            "warmup": 10,
            "messages": 100,
            "payload": 64,
            "rate": 1000,
        },
        "advocate_review": {
            "reviewer": "kungfu-origin",
            "status": "approved",
            "scope": [
                "version", "channels", "threading", "idle", "sync", "payload",
                "topology", "receipt_mapping", "exclusions",
            ],
        },
        "claim_boundary": {
            "native_performance_authority_requested": True,
            "containerized_user_outcome_authority": False,
            "fresh_install_cost_authority": False,
            "final_scoring_authority": False,
        },
    }


def metrics() -> dict:
    return {
        "user_seconds": 0.1,
        "system_seconds": 0.1,
        "max_rss_kib": 1024,
        "minor_faults": 1,
        "major_faults": 0,
        "input_blocks": 0,
        "output_blocks": 1,
        "voluntary_context_switches": 1,
        "involuntary_context_switches": 0,
    }


class NativeQualificationTests(unittest.TestCase):
    def test_valid_plan(self) -> None:
        native.validate_plan_document(valid_plan())

    def test_plan_rejects_placeholder_digest(self) -> None:
        plan = valid_plan()
        plan["kit"]["image"] = (
            "ghcr.io/kungfu-systems/build-images/aeron-native-kit@sha256:" + "0" * 64
        )
        with self.assertRaisesRegex(native.NativeQualificationError, "placeholder"):
            native.validate_plan_document(plan)

    def test_plan_rejects_docker_authority_relabel(self) -> None:
        plan = valid_plan()
        plan["evidence_class"] = "containerized-user-outcome-qualification"
        with self.assertRaises(native.NativeQualificationError):
            native.validate_plan_document(plan)

    def test_plan_rejects_configuration_and_review_drift(self) -> None:
        plan = valid_plan()
        plan["configuration"]["file_sync_level"] = 2
        with self.assertRaisesRegex(native.NativeQualificationError, "configuration"):
            native.validate_plan_document(plan)
        plan = valid_plan()
        plan["advocate_review"]["reviewer"] = "anonymous"
        with self.assertRaisesRegex(native.NativeQualificationError, "advocate"):
            native.validate_plan_document(plan)

    def test_ipc_run_binds_a_deterministic_seed_to_the_marker(self) -> None:
        parameters = {"warmup": 10, "messages": 20, "payload": 64, "rate": 1000}
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "ipc"

            def fake_harness(*args, **kwargs):
                (output / "latency.hlog").write_text("histogram\n", encoding="utf-8")
                arguments = args[2]
                seed = int(arguments[arguments.index("--seed") + 1])
                return {
                    "coordinated_omission": "expected-interval-correction",
                    "messages": 20,
                    "samples": 20,
                    "seed": seed,
                }, metrics()

            with (
                mock.patch.object(native, "harness_json", side_effect=fake_harness) as harness,
                mock.patch.object(native, "finalize_result", return_value=output / "result.json"),
            ):
                native.run_ipc_result(
                    pathlib.Path("launcher"), pathlib.Path("java"), SHA_A,
                    "ipc", 2, "p64-r1000", parameters, output,
                )

        arguments = harness.call_args.args[2]
        marker = native.result_marker(SHA_A, "ipc", 2, "p64-r1000")
        self.assertEqual(arguments[arguments.index("--seed") + 1], str(native.ipc_seed(marker)))

    def _write_result(
        self,
        bundle: pathlib.Path,
        tier: str,
        repetition: int,
        variant: str,
        *,
        p99: int = 100,
    ) -> pathlib.Path:
        directory = bundle / "results" / tier / variant / f"r{repetition:03d}"
        directory.mkdir(parents=True)
        marker = native.sha256_json({"tier": tier, "repetition": repetition, "variant": variant})
        if tier in ("calibration", "ipc", "soak"):
            (directory / "latency.hlog").write_text("#[Histogram log format version 1.3]\n", encoding="utf-8")
            measurement = {
                "p50_ns": 50,
                "p95_ns": 90,
                "p99_ns": p99,
                "p999_ns": p99 + 5,
                "max_ns": p99 + 10,
                "offered_rate": 1000,
                "messages": 20,
                "samples": 20,
                "seed": native.ipc_seed(marker),
                "coordinated_omission": "expected-interval-correction",
            }
        elif tier == "receipts":
            measurement = {
                "mode": variant,
                "record": {
                    "receipt": variant,
                    "marker": marker,
                    "duplicates": 0,
                    "reordered": 0,
                    "marker_mismatches": 0,
                    "receipt_duration_ns": 100,
                    "completion_duration_ns": 110,
                },
                "replay": {
                    "marker": marker,
                    "duplicates": 0,
                    "reordered": 0,
                    "marker_mismatches": 0,
                },
            }
        else:
            measurement = {
                "mode": variant,
                "stale_health_rejected": True,
                "expiry_wait_seconds": 11,
                "replay": {
                    "marker": marker,
                    "expected": 5,
                    "observed": 5,
                    "duplicates": 0,
                    "reordered": 0,
                    "marker_mismatches": 0,
                },
            }
        result = {
            "run_schema": native.RUN_SCHEMA_ID,
            "schema_version": 1,
            "status": "passed",
            "evidence_class": native.EVIDENCE_CLASS,
            "native_performance_authority": False,
            "tier": tier,
            "repetition": repetition,
            "started_at": "2026-07-16T00:00:00Z",
            "finished_at": "2026-07-16T00:00:01Z",
            "marker": marker,
            "measurement": measurement,
            "process_metrics": metrics(),
            "raw_artifacts": [],
            "container_guard": {
                "measured_in_container": False,
                "extraction_containers": [],
                "passed": True,
            },
        }
        path = directory / "result.json"
        native.write_json(path, result)
        return path

    def _bundle(self, root: pathlib.Path) -> pathlib.Path:
        plan = valid_plan()
        inputs = root / "inputs"
        inputs.mkdir(parents=True)
        plan_path = inputs / "aeron-native-phase-a-v1.json"
        native.write_json(plan_path, plan)
        contracts_dir = root / "contracts"
        contracts_dir.mkdir()
        contracts = []
        for source in (native.PLAN_SCHEMA_PATH, native.RUN_SCHEMA_PATH, native.BUNDLE_SCHEMA_PATH):
            target = contracts_dir / source.name
            target.write_bytes(source.read_bytes())
            contracts.append({
                "path": target.relative_to(root).as_posix(),
                "sha256": native.sha256_file(target),
            })
        preparation_dir = root / "preparation"
        preparation_dir.mkdir()
        preparation_path = preparation_dir / "preparation.json"
        native.write_json(preparation_path, {
            "image": IMAGE,
            "extraction_container_removed": True,
            "measurement_runtime": "host-process",
        })
        host_dir = root / "host"
        host_dir.mkdir()
        host_path = host_dir / "host-facts.json"
        native.write_json(host_path, {
            "host_state_mutated": False,
            "perf_counters_required": False,
        })

        result_paths = []
        for repetition, p99 in enumerate((95, 100, 105), start=1):
            result_paths.append(self._write_result(root, "calibration", repetition, "canary", p99=p99))
        for repetition in range(1, 4):
            result_paths.append(self._write_result(root, "ipc", repetition, "p64-r1000"))
        for mode in plan["receipts"]["modes"]:
            for repetition in range(1, 4):
                result_paths.append(self._write_result(root, "receipts", repetition, mode))
        for mode in plan["recovery"]["modes"]:
            for repetition in range(1, 4):
                result_paths.append(self._write_result(root, "recovery", repetition, mode))
        result_paths.append(self._write_result(root, "soak", 1, "bounded-soak"))
        records = [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": native.sha256_file(path),
                "tier": native.load_json(path)["tier"],
                "repetition": native.load_json(path)["repetition"],
                "status": "passed",
            }
            for path in result_paths
        ]
        bundle_path = root / "bundle-manifest.json"
        manifest = {
            "bundle_schema": native.BUNDLE_SCHEMA_ID,
            "schema_version": 1,
            "status": "complete",
            "evidence_class": native.EVIDENCE_CLASS,
            "native_performance_authority": True,
            "containerized_user_outcome_authority": False,
            "fresh_install_cost_authority": False,
            "final_scoring_authority": False,
            "bundle_id": "synthetic-native-bundle",
            "generated_at": "2026-07-16T00:00:00Z",
            "build_images_git_sha": "c" * 40,
            "plan": {
                "path": plan_path.relative_to(root).as_posix(),
                "sha256": native.sha256_file(plan_path),
            },
            "preparation": {
                "path": preparation_path.relative_to(root).as_posix(),
                "sha256": native.sha256_file(preparation_path),
                "image": IMAGE,
            },
            "host_facts": {
                "path": host_path.relative_to(root).as_posix(),
                "sha256": native.sha256_file(host_path),
            },
            "contracts": contracts,
            "expected_results": len(records),
            "completed_results": len(records),
            "results": records,
            "artifacts": native.artifact_records(root, exclude={bundle_path}),
            "claim_boundary": native.CLAIM_BOUNDARY,
        }
        native.write_json(bundle_path, manifest)
        return bundle_path

    def test_offline_bundle_verifier_and_histogram_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            self._bundle(root)
            bundle = native.verify_bundle(root)
            self.assertTrue(bundle["native_performance_authority"])
            histogram = next(root.rglob("latency.hlog"))
            histogram.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(native.NativeQualificationError, "integrity"):
                native.verify_bundle(root)

    def test_result_rejects_container_relabel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            path = self._write_result(root, "ipc", 1, "p64-r1000")
            result = native.load_json(path)
            result["container_guard"]["measured_in_container"] = True
            result["container_guard"]["passed"] = False
            native.write_json(path, result)
            record = {
                "path": path.relative_to(root).as_posix(),
                "sha256": native.sha256_file(path),
                "tier": "ipc",
                "repetition": 1,
                "status": "passed",
            }
            with self.assertRaisesRegex(native.NativeQualificationError, "container guard"):
                native.verify_result(valid_plan(), path, record)


if __name__ == "__main__":
    unittest.main()
