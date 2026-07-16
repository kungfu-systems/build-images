from __future__ import annotations

import copy
import json
import pathlib
import subprocess
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
BUILD_SHA = "c" * 40
SOURCE_SHA = "d" * 40
RELEASE_TAG = "v1.2.4-alpha.23"


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
            "ipc_poll_batch": 64,
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
                "poll_batch", "topology", "receipt_mapping", "exclusions",
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
        parameters = {
            "warmup": 10, "messages": 20, "payload": 64, "rate": 1000,
            "poll_batch": 64,
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "ipc"

            def fake_harness(*args, **kwargs):
                (output / "latency.hlog").write_text("histogram\n", encoding="utf-8")
                arguments = args[2]
                seed = int(arguments[arguments.index("--seed") + 1])
                poll_batch = int(arguments[arguments.index("--poll-batch") + 1])
                return {
                    "coordinated_omission": "expected-interval-correction",
                    "messages": 20,
                    "samples": 20,
                    "seed": seed,
                    "poll_batch": poll_batch,
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
        self.assertEqual(arguments[arguments.index("--poll-batch") + 1], "64")

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
        authority_dir = root / "authority"
        runner_path = authority_dir / "scripts" / "aeron_native_qualification.py"
        runner_path.parent.mkdir(parents=True)
        runner_path.write_bytes(native.SCRIPT_PATH.read_bytes())
        passport_path = authority_dir / "buildchain.release.json"
        native.write_json(passport_path, {
            "contract": native.RELEASE_PASSPORT_CONTRACT,
            "release": {
                "tag": RELEASE_TAG,
                "publicTag": RELEASE_TAG,
                "exactRef": f"refs/tags/{RELEASE_TAG}",
                "sourceSha": SOURCE_SHA,
                "releaseSha": BUILD_SHA,
                "releaseMaterialSha": BUILD_SHA,
            },
            "evidence": {"checkReport": "check-report.json"},
            "transaction": {
                "state": "complete",
                "exactTag": RELEASE_TAG,
                "releaseSha": BUILD_SHA,
                "releaseMaterialSha": BUILD_SHA,
                "result": {"validation": {"valid": True, "errors": []}},
            },
        })
        check_report_path = authority_dir / "check-report.json"
        native.write_json(check_report_path, {
            "contract": native.RELEASE_CHECK_REPORT_CONTRACT,
            "ok": True,
            "trust": "pass",
            "issues": [],
        })
        release_authority = {
            "tag": RELEASE_TAG,
            "material_sha": BUILD_SHA,
            "source_sha": SOURCE_SHA,
            "origin": "https://github.com/kungfu-systems/build-images.git",
            "tag_ref": f"refs/tags/{RELEASE_TAG}",
            "api_url": f"{native.GITHUB_RELEASE_API}/{RELEASE_TAG}",
            "passport_sha256": native.sha256_file(passport_path),
            "check_report_sha256": native.sha256_file(check_report_path),
        }
        receipt_path = authority_dir / "public-release.json"
        native.write_json(receipt_path, {
            "contract": native.PUBLIC_RELEASE_RECEIPT_CONTRACT,
            "schema_version": 1,
            **release_authority,
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
            "build_images_git_sha": BUILD_SHA,
            "release_tag": RELEASE_TAG,
            "release_authority": release_authority,
            "runner": {
                "path": runner_path.relative_to(root).as_posix(),
                "sha256": native.sha256_file(runner_path),
            },
            "release_passport": {
                "path": passport_path.relative_to(root).as_posix(),
                "sha256": native.sha256_file(passport_path),
            },
            "release_check_report": {
                "path": check_report_path.relative_to(root).as_posix(),
                "sha256": native.sha256_file(check_report_path),
            },
            "public_release_receipt": {
                "path": receipt_path.relative_to(root).as_posix(),
                "sha256": native.sha256_file(receipt_path),
            },
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
            runner_path = root / bundle["runner"]["path"]
            process = subprocess.run(
                [sys.executable, str(runner_path), "verify-bundle", "--bundle", str(root)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            histogram = next(root.rglob("latency.hlog"))
            histogram.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(native.NativeQualificationError, "integrity"):
                native.verify_bundle(root)

    def test_exact_release_tag_rejects_clean_non_release_commit(self) -> None:
        completed = mock.Mock(stdout="v1-alpha\nv1.2-alpha\n")
        with mock.patch.object(native, "run_command", return_value=completed):
            with self.assertRaisesRegex(native.NativeQualificationError, "public release tag"):
                native.exact_release_tag(BUILD_SHA)

    def test_tagged_runner_requires_exact_release_bytes(self) -> None:
        tagged_blob = "1" * 40
        with mock.patch.object(
            native,
            "run_command",
            side_effect=[mock.Mock(stdout=tagged_blob), mock.Mock(stdout=tagged_blob)],
        ):
            native.require_tagged_runner(BUILD_SHA)

        with mock.patch.object(
            native,
            "run_command",
            side_effect=[mock.Mock(stdout=tagged_blob), mock.Mock(stdout="2" * 40)],
        ):
            with self.assertRaisesRegex(native.NativeQualificationError, "runner bytes"):
                native.require_tagged_runner(BUILD_SHA)

    def test_release_authority_rejects_material_or_trust_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            manifest_path = self._bundle(root)
            manifest = native.load_json(manifest_path)
            passport_path = root / manifest["release_passport"]["path"]
            passport = native.load_json(passport_path)
            passport["release"]["releaseMaterialSha"] = "e" * 40
            native.write_json(passport_path, passport)
            manifest["release_passport"]["sha256"] = native.sha256_file(passport_path)
            manifest["artifacts"] = native.artifact_records(root, exclude={manifest_path})
            native.write_json(manifest_path, manifest)
            with self.assertRaisesRegex(native.NativeQualificationError, "exact reviewed material"):
                native.verify_bundle(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            manifest_path = self._bundle(root)
            manifest = native.load_json(manifest_path)
            passport_path = root / manifest["release_passport"]["path"]
            passport = native.load_json(passport_path)
            passport["release"]["sourceSha"] = "e" * 40
            native.write_json(passport_path, passport)
            manifest["release_passport"]["sha256"] = native.sha256_file(passport_path)
            manifest["artifacts"] = native.artifact_records(root, exclude={manifest_path})
            native.write_json(manifest_path, manifest)
            with self.assertRaisesRegex(native.NativeQualificationError, "inconsistent"):
                native.verify_bundle(root)

    def test_public_release_rejects_remote_or_asset_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            manifest_path = self._bundle(root)
            manifest = native.load_json(manifest_path)
            passport_path = root / manifest["release_passport"]["path"]
            check_path = root / manifest["release_check_report"]["path"]
            commands = [
                mock.Mock(stdout="https://github.com/kungfu-systems/build-images.git\n"),
                mock.Mock(stdout=f"{'e' * 40}\trefs/tags/{RELEASE_TAG}\n"),
            ]
            with mock.patch.object(native, "run_command", side_effect=commands):
                with self.assertRaisesRegex(native.NativeQualificationError, "public release tag"):
                    native.public_release_evidence(
                        passport_path, check_path, BUILD_SHA, RELEASE_TAG
                    )

            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            response.read.return_value = json.dumps({
                "tag_name": RELEASE_TAG,
                "draft": False,
                "published_at": "2026-07-16T00:00:00Z",
                "assets": [
                    {"name": "buildchain.release.json", "digest": f"sha256:{'0' * 64}"},
                    {"name": "check-report.json", "digest": f"sha256:{'0' * 64}"},
                ],
            }).encode()
            commands = [
                mock.Mock(stdout="https://github.com/kungfu-systems/build-images.git\n"),
                mock.Mock(stdout=f"{BUILD_SHA}\trefs/tags/{RELEASE_TAG}\n"),
            ]
            with (
                mock.patch.object(native, "run_command", side_effect=commands),
                mock.patch.object(native.urllib.request, "urlopen", return_value=response),
            ):
                with self.assertRaisesRegex(native.NativeQualificationError, "asset digest"):
                    native.public_release_evidence(
                        passport_path, check_path, BUILD_SHA, RELEASE_TAG
                    )

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            manifest_path = self._bundle(root)
            manifest = native.load_json(manifest_path)
            check_path = root / manifest["release_check_report"]["path"]
            report = native.load_json(check_path)
            report["trust"] = "fail"
            native.write_json(check_path, report)
            manifest["release_check_report"]["sha256"] = native.sha256_file(check_path)
            manifest["artifacts"] = native.artifact_records(root, exclude={manifest_path})
            native.write_json(manifest_path, manifest)
            with self.assertRaisesRegex(native.NativeQualificationError, "does not grant trust"):
                native.verify_bundle(root)

    def test_public_release_accepts_exact_tag_and_asset_digests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            manifest_path = self._bundle(root)
            manifest = native.load_json(manifest_path)
            passport_path = root / manifest["release_passport"]["path"]
            check_path = root / manifest["release_check_report"]["path"]
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            response.read.return_value = json.dumps({
                "tag_name": RELEASE_TAG,
                "draft": False,
                "published_at": "2026-07-16T00:00:00Z",
                "assets": [
                    {
                        "name": passport_path.name,
                        "digest": f"sha256:{native.sha256_file(passport_path)}",
                    },
                    {
                        "name": check_path.name,
                        "digest": f"sha256:{native.sha256_file(check_path)}",
                    },
                ],
            }).encode()
            commands = [
                mock.Mock(stdout="https://github.com/kungfu-systems/build-images.git\n"),
                mock.Mock(stdout=f"{BUILD_SHA}\trefs/tags/{RELEASE_TAG}\n"),
            ]
            with (
                mock.patch.object(native, "run_command", side_effect=commands),
                mock.patch.object(native.urllib.request, "urlopen", return_value=response),
            ):
                receipt = native.public_release_evidence(
                    passport_path, check_path, BUILD_SHA, RELEASE_TAG
                )
            self.assertEqual(receipt["passport_sha256"], native.sha256_file(passport_path))
            self.assertEqual(receipt["check_report_sha256"], native.sha256_file(check_path))

    def test_offline_bundle_rejects_missing_authority_inputs(self) -> None:
        for binding_name in (
            "runner", "release_passport", "release_check_report", "public_release_receipt"
        ):
            with self.subTest(binding=binding_name), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                manifest_path = self._bundle(root)
                manifest = native.load_json(manifest_path)
                path = root / manifest[binding_name]["path"]
                path.unlink()
                with self.assertRaisesRegex(native.NativeQualificationError, "binding|digest"):
                    native.verify_bundle(root)

    def test_offline_bundle_rejects_rebound_public_release_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            manifest_path = self._bundle(root)
            manifest = native.load_json(manifest_path)
            receipt_path = root / manifest["public_release_receipt"]["path"]
            receipt = native.load_json(receipt_path)
            receipt["source_sha"] = "e" * 40
            native.write_json(receipt_path, receipt)
            manifest["public_release_receipt"]["sha256"] = native.sha256_file(receipt_path)
            manifest["artifacts"] = native.artifact_records(root, exclude={manifest_path})
            native.write_json(manifest_path, manifest)
            with self.assertRaisesRegex(native.NativeQualificationError, "inconsistent"):
                native.verify_bundle(root)

    def test_offline_bundle_rejects_rebound_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            manifest_path = self._bundle(root)
            manifest = native.load_json(manifest_path)
            runner_path = root / manifest["runner"]["path"]
            runner_path.write_text("# forged runner\n", encoding="utf-8")
            manifest["runner"]["sha256"] = native.sha256_file(runner_path)
            manifest["artifacts"] = native.artifact_records(root, exclude={manifest_path})
            native.write_json(manifest_path, manifest)
            with self.assertRaisesRegex(native.NativeQualificationError, "executing native verifier"):
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
