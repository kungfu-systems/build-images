from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "qualification-plans"
sys.path.insert(0, str(SCRIPT_DIR))

import comparator_qualification as qualification  # noqa: E402


class QualificationPlanTests(unittest.TestCase):
    def test_same_runner_resolves_all_profiles(self) -> None:
        for profile in qualification.PROFILES:
            with self.subTest(profile=profile):
                plan, resolved = qualification.resolve_plan(FIXTURE_DIR / f"{profile}.json")
                self.assertTrue(plan["test_only"])
                self.assertEqual(resolved["profile"], profile)
                self.assertEqual(resolved["repetitions"], 3)
                self.assertFalse(resolved["subject"].get("native_performance_authority", False))

    def test_replacement_compose_is_rejected(self) -> None:
        plan = qualification.load_json(FIXTURE_DIR / "aeron.json")
        plan["environment"]["compose_path"] = "replacement.yaml"
        with self.assertRaisesRegex(qualification.QualificationError, "replacement Compose"):
            qualification.validate_plan_document(plan)

    def test_expert_tuned_is_unavailable(self) -> None:
        plan = qualification.load_json(FIXTURE_DIR / "aeron.json")
        plan["configuration_slot"] = "expert-tuned"
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "plan.json"
            qualification.write_json(path, plan)
            with self.assertRaisesRegex(qualification.QualificationError, "expert-tuned"):
                qualification.resolve_plan(path)

    def test_test_only_plan_cannot_execute(self) -> None:
        with self.assertRaisesRegex(qualification.QualificationError, "test-only"):
            qualification.run_plan(FIXTURE_DIR / "aeron.json")

    def test_kungfu_requires_the_fixed_package_filename(self) -> None:
        plan = qualification.load_json(FIXTURE_DIR / "kungfu.json")
        plan["subject"]["artifact_name"] = "source-checkout.tar.gz"
        with self.assertRaisesRegex(qualification.QualificationError, "fixed Kungfu package filename"):
            qualification.validate_plan_document(plan)


class QualificationExecutionTests(unittest.TestCase):
    def test_controlled_environment_drops_ad_hoc_compose_inputs(self) -> None:
        with mock.patch.dict(
            qualification.os.environ,
            {"PATH": "/usr/bin", "HOME": "/tmp/home", "COMPOSE_FILE": "replacement.yaml", "UNRELATED": "value"},
            clear=True,
        ):
            environment = qualification.controlled_environment()
        self.assertEqual(environment, {"PATH": "/usr/bin", "HOME": "/tmp/home"})

    def test_timeout_still_runs_project_scoped_cleanup(self) -> None:
        scenario = {"id": "timeout-scenario", "job_id": "timeout-job"}
        step = {
            "id": "smoke",
            "action": "profile-smoke",
            "timeout_seconds": 30,
            "oracles": [
                {"type": "exit-code", "expected": 0},
                {"type": "artifact-exists", "path": "pilot/evidence.txt", "expected": True},
            ],
        }
        timeout = subprocess.TimeoutExpired(["bash", "pilot.sh"], 30, output="partial", stderr="timeout")
        cleanup = mock.Mock(returncode=0, stdout="cleanup ok\n", stderr="")
        with tempfile.TemporaryDirectory() as temporary:
            repetition_dir = pathlib.Path(temporary) / "repetition-001"
            with mock.patch.object(qualification.subprocess, "run", side_effect=[timeout, cleanup]) as run:
                result = qualification.execute_step(
                    "aeron",
                    {},
                    "qualification-timeout-test",
                    1,
                    scenario,
                    step,
                    repetition_dir,
                )
            cleanup_command = run.call_args_list[1].args[0]
            self.assertEqual(result["exit_code"], 124)
            self.assertEqual(result["cleanup_exit_code"], 0)
            self.assertFalse(result["passed"])
            self.assertIn("--project-name", cleanup_command)
            self.assertEqual(cleanup_command[-3:], ["down", "--volumes", "--remove-orphans"])
            self.assertEqual(
                (repetition_dir / "raw" / "timeout-scenario" / "smoke" / "cleanup.stdout.log").read_text(),
                "cleanup ok\n",
            )


class QualificationBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.bundle_dir = pathlib.Path(self.temporary.name) / "bundle"
        self.bundle_dir.mkdir()
        fixture_plan = qualification.load_json(FIXTURE_DIR / "aeron.json")
        fixture_plan["test_only"] = False
        fixture_plan["scenarios"][0]["steps"][0]["oracles"] = [
            {"type": "exit-code", "expected": 0},
            {"type": "artifact-exists", "path": "evidence.txt", "expected": True},
            {"type": "artifact-text-contains", "path": "evidence.txt", "expected": "evidence-"},
        ]
        fixture_plan["artifact_retention"]["required_patterns"] = ["raw/**/evidence.txt"]
        self.plan_path = self.bundle_dir / "qualification-plan.json"
        qualification.write_json(self.plan_path, fixture_plan)
        self.plan_sha = qualification.sha256_file(self.plan_path)
        self.contracts = qualification.copy_contracts(self.bundle_dir)
        locked_subject = qualification.load_json(qualification.LOCK_PATH)["profiles"]["aeron"]
        self.bundle_inputs = qualification.copy_bundle_inputs(
            self.bundle_dir,
            {"runner_image": fixture_plan["environment"]["runner_image"], "subject": locked_subject},
        )
        self.build_sha = "c" * 40
        self.bundle_id = "synthetic-offline-verification"
        self.run_records = []
        for repetition in range(1, 4):
            repetition_dir = self.bundle_dir / f"repetition-{repetition:03d}"
            raw = repetition_dir / "raw" / "archive-recovery" / "smoke"
            raw.mkdir(parents=True)
            (raw / "evidence.txt").write_text(f"evidence-{repetition}\n", encoding="utf-8")
            manifest = {
                "run_schema": qualification.RUN_SCHEMA_ID,
                "schema_version": 1,
                "status": "passed",
                "evidence_class": qualification.EVIDENCE_CLASS,
                "qualification_candidate": True,
                "native_performance_authority": False,
                "user_outcome_qualification_authority": False,
                "fresh_install_cost_authority": False,
                "final_scoring_authority": False,
                "bundle_id": self.bundle_id,
                "run_id": f"synthetic-{repetition}",
                "repetition": repetition,
                "profile": "aeron",
                "configuration_slot": "realistic-default",
                "started_at": "2026-07-14T00:00:00Z",
                "finished_at": "2026-07-14T00:00:01Z",
                "duration_seconds": 1.0,
                "inputs": {
                    "build_images_git_sha": self.build_sha,
                    "plan_sha256": self.plan_sha,
                    "charter": fixture_plan["charter"],
                    "fixture_set": fixture_plan["fixture_set"],
                    "compose_sha256": fixture_plan["environment"]["compose_sha256"],
                    "environment_lock_sha256": fixture_plan["environment"]["environment_lock_sha256"],
                    "runner_image": fixture_plan["environment"]["runner_image"],
                    "pilot_script_sha256": self.bundle_inputs["pilot_script"]["sha256"],
                    "qualification_runner_sha256": self.bundle_inputs["qualification_runner"]["sha256"],
                    "subject": locked_subject,
                    "subject_sha256": qualification.sha256_json(locked_subject),
                },
                "runtime": {
                    "platform": "linux-test",
                    "machine": "x86_64",
                    "kernel": "test",
                    "cpu_count": 2,
                    "memory_bytes": 1024,
                    "docker_version": {"Client": {"Version": "test"}},
                    "docker_compose_version": "test",
                    "docker_info": {"Driver": "test"},
                    "cgroup": {"version": "v2", "files": {}},
                    "resource_limits": {
                        "runner": {"mem_limit": "256m", "cpus": "0.50"},
                        "aeron": {"mem_limit": "1g", "cpus": "1.00"},
                    },
                },
                "steps": [
                    {
                        "scenario_id": "archive-recovery",
                        "job_id": "aeron-qualification",
                        "step_id": "smoke",
                        "action": "profile-smoke",
                        "project": f"synthetic-{repetition}",
                        "started_at": "2026-07-14T00:00:00Z",
                        "finished_at": "2026-07-14T00:00:01Z",
                        "duration_seconds": 1.0,
                        "timeout_seconds": 1200,
                        "timed_out": False,
                        "exit_code": 0,
                        "cleanup_exit_code": 0,
                        "oracles": qualification.evaluate_oracles(
                            raw,
                            0,
                            fixture_plan["scenarios"][0]["steps"][0]["oracles"],
                        ),
                        "passed": True,
                    }
                ],
                "retention": qualification.retention_results(
                    repetition_dir,
                    fixture_plan["artifact_retention"]["required_patterns"],
                ),
                "artifacts": qualification.artifact_records(repetition_dir, exclude={"run-manifest.json"}),
                "claim_boundary": qualification.CLAIM_BOUNDARY,
            }
            manifest_path = repetition_dir / "run-manifest.json"
            qualification.write_json(manifest_path, manifest)
            self.run_records.append(
                {
                    "repetition": repetition,
                    "path": manifest_path.relative_to(self.bundle_dir).as_posix(),
                    "sha256": qualification.sha256_file(manifest_path),
                    "status": "passed",
                }
            )
        bundle = {
            "bundle_schema": qualification.BUNDLE_SCHEMA_ID,
            "schema_version": 1,
            "status": "complete",
            "evidence_class": qualification.EVIDENCE_CLASS,
            "native_performance_authority": False,
            "user_outcome_qualification_authority": True,
            "fresh_install_cost_authority": False,
            "final_scoring_authority": False,
            "bundle_id": self.bundle_id,
            "generated_at": "2026-07-14T00:00:03Z",
            "profile": "aeron",
            "configuration_slot": "realistic-default",
            "build_images_git_sha": self.build_sha,
            "inputs": self.bundle_inputs,
            "plan": {"path": self.plan_path.name, "sha256": self.plan_sha},
            "expected_repetitions": 3,
            "completed_repetitions": 3,
            "contracts": self.contracts,
            "run_manifests": self.run_records,
            "claim_boundary": qualification.CLAIM_BOUNDARY,
        }
        qualification.write_json(self.bundle_dir / "bundle-manifest.json", bundle)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_offline_verification_accepts_complete_three_repetition_bundle(self) -> None:
        bundle = qualification.verify_bundle(self.bundle_dir)
        self.assertTrue(bundle["user_outcome_qualification_authority"])

    def test_offline_verification_rejects_altered_raw_evidence(self) -> None:
        target = self.bundle_dir / "repetition-002" / "raw" / "archive-recovery" / "smoke" / "evidence.txt"
        target.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(qualification.QualificationError, "oracle evidence"):
            qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_missing_repetition(self) -> None:
        bundle_path = self.bundle_dir / "bundle-manifest.json"
        bundle = qualification.load_json(bundle_path)
        bundle["run_manifests"] = bundle["run_manifests"][:2]
        qualification.write_json(bundle_path, bundle)
        with self.assertRaisesRegex(qualification.QualificationError, "exactly one run manifest"):
            qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_unrecorded_artifact(self) -> None:
        target = self.bundle_dir / "repetition-003" / "raw" / "archive-recovery" / "smoke" / "unrecorded.txt"
        target.write_text("not in the manifest\n", encoding="utf-8")
        with self.assertRaisesRegex(qualification.QualificationError, "artifact inventory is incomplete"):
            qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_altered_frozen_input(self) -> None:
        target = self.bundle_dir / "inputs" / "compose.yaml"
        target.write_text(target.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(qualification.QualificationError, "bundle input digest mismatch"):
            qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_rebound_subject(self) -> None:
        bundle_path = self.bundle_dir / "bundle-manifest.json"
        bundle = qualification.load_json(bundle_path)
        bundle["inputs"]["subject"]["status"] = "tampered"
        bundle["inputs"]["subject_sha256"] = qualification.sha256_json(bundle["inputs"]["subject"])
        qualification.write_json(bundle_path, bundle)
        with self.assertRaisesRegex(qualification.QualificationError, "subject does not match"):
            qualification.verify_bundle(self.bundle_dir)


if __name__ == "__main__":
    unittest.main()
