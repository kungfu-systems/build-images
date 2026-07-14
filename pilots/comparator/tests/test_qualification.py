from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
PILOT_DIR = pathlib.Path(__file__).resolve().parents[1]
ADAPTER_DIR = PILOT_DIR / "workload-adapters"
FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "qualification-plans"
PRODUCTION_PLAN = PILOT_DIR / "plans" / "postgres-phase-a-v1.json"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(ADAPTER_DIR))

import comparator_qualification as qualification  # noqa: E402
import postgres_phase_a_v1 as postgres_adapter  # noqa: E402
import postgres_phase_a_semantics as postgres_semantics  # noqa: E402


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

    def test_production_plan_covers_every_declared_job_and_tier(self) -> None:
        plan, resolved = qualification.resolve_plan(PRODUCTION_PLAN)
        expected = {
            (mapping["job_id"], tier)
            for mapping in plan["workload_adapter"]["mappings"]
            for tier in mapping["tiers"]
        }
        observed = {(scenario["job_id"], scenario["tier"]) for scenario in plan["scenarios"]}
        self.assertEqual(observed, expected)
        self.assertEqual(len(observed), 21)
        self.assertEqual(resolved["workload_adapter"]["identity"], plan["workload_adapter"])

    def test_runner_and_adapter_compute_the_same_workload_binding(self) -> None:
        plan = qualification.load_json(PRODUCTION_PLAN)
        scenario = plan["scenarios"][0]
        step = scenario["steps"][0]
        runner_binding = qualification.workload_binding(
            plan["profile"],
            plan["adapter_registry"]["sha256"],
            plan["workload_adapter"],
            scenario,
            step,
        )
        adapter_binding = postgres_adapter.adapter_binding(
            plan["adapter_registry"]["sha256"],
            plan["workload_adapter"],
            scenario["id"],
            scenario["job_id"],
            scenario["tier"],
            step["id"],
        )
        self.assertEqual(adapter_binding, runner_binding)

    def test_production_plan_rejects_missing_adapter_mapping(self) -> None:
        plan = qualification.load_json(PRODUCTION_PLAN)
        plan["workload_adapter"]["mappings"][0]["tiers"].remove("normal")
        with self.assertRaisesRegex(qualification.QualificationError, "not mapped"):
            qualification.validate_plan_document(plan)

    def test_generic_smoke_cannot_be_relabelled_as_production_work(self) -> None:
        plan = qualification.load_json(PRODUCTION_PLAN)
        step = plan["scenarios"][0]["steps"][0]
        step["action"] = "profile-smoke"
        step.pop("adapter_id")
        with self.assertRaisesRegex(qualification.QualificationError, "declared workload adapter"):
            qualification.validate_plan_document(plan)


class WorkloadSemanticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = qualification.load_json(
            ADAPTER_DIR / "fixtures" / "postgres-phase-a-v1.json"
        )
        self.oracle = qualification.load_json(
            ADAPTER_DIR / "oracles" / "postgres-phase-a-v1.json"
        )

    @staticmethod
    def tier_evidence(tier: str = "normal") -> dict[str, object]:
        evidence: dict[str, object] = {
            "schema": "urn:kungfu-systems:build-images:postgres-tier-evidence:v1",
            "job_id": "synthetic",
            "tier": tier,
            "binding_sha256": "b" * 64,
            "observed_facts_sha256": "f" * 64,
        }
        if tier == "normal":
            evidence.update(operation="current-fact-query", observed_rows=1)
        elif tier == "concurrent":
            evidence.update(operation="two-agent-concurrent-write", observed_rows=3, writers=2)
        elif tier == "crash-recovery":
            evidence.update(operation="sigkill-restart-query", observed_rows=1)
        elif tier == "whole-root-restore":
            evidence.update(
                operation="whole-project-volume-recreate-and-restore",
                observed_rows=1,
                backup_sha256="a" * 64,
            )
        elif tier == "historical-query":
            evidence.update(operation="ordered-history-query", observed_versions="1,2")
        elif tier == "schema-evolution":
            evidence.update(operation="additive-schema-migration", observed_schema_version=2)
        elif tier == "new-agent-takeover":
            evidence.update(operation="fresh-session-handoff-query", observed_state="handoff-ready")
        return evidence

    def verdict(self, job_id: str, facts: dict[str, object], tier: str = "normal") -> dict[str, object]:
        return postgres_semantics.derive_job_verdict(job_id, facts, tier, self.tier_evidence(tier))

    def test_execution_fixture_contains_no_answer_key(self) -> None:
        postgres_semantics.validate_execution_fixture(self.fixture)
        self.assertNotIn("expected", json.dumps(self.fixture))
        poisoned = copy.deepcopy(self.fixture)
        poisoned["jobs"][postgres_semantics.JOB_IDS[0]]["expected"] = {"state": "copied"}
        with self.assertRaisesRegex(postgres_semantics.SemanticError, "forbidden answer field"):
            postgres_semantics.validate_execution_fixture(poisoned)

    def test_base_verdicts_match_the_independent_oracle_across_every_tier(self) -> None:
        for job_id in postgres_semantics.JOB_IDS:
            facts = self.fixture["jobs"][job_id]["facts"]
            for tier in postgres_adapter.TIERS:
                with self.subTest(job_id=job_id, tier=tier):
                    self.assertEqual(self.verdict(job_id, facts, tier), self.oracle["jobs"][job_id])

    def test_j1_healthy_sessions_change_the_verdict(self) -> None:
        job_id = postgres_semantics.JOB_IDS[0]
        verdict = self.verdict(job_id, {"sessions": []})
        self.assertEqual(verdict["state"], "healthy")
        self.assertEqual(verdict["required_findings"], [])
        self.assertFalse(verdict["false_receipt_rejected"])
        self.assertNotEqual(verdict, self.oracle["jobs"][job_id])

    def test_j2_bound_runtime_receipt_completes_delivery(self) -> None:
        job_id = postgres_semantics.JOB_IDS[1]
        facts = copy.deepcopy(self.fixture["jobs"][job_id]["facts"])
        facts["downstream_runtime_smoke_receipt"] = {
            "status": "passed",
            "source_sha": facts["source_sha"],
            "runtime_sha": facts["runtime"]["sha"],
            "package_sha256": facts["package"]["sha256"],
        }
        verdict = self.verdict(job_id, facts)
        self.assertEqual(verdict["state"], "completed-trusted")
        self.assertTrue(verdict["completion"])
        self.assertEqual(verdict["blockers"], [])
        self.assertIsNone(verdict["only_blocker"])

    def test_j3_completed_goal_changes_the_handoff_verdict(self) -> None:
        job_id = postgres_semantics.JOB_IDS[2]
        facts = {
            "goal_status": "completed",
            "latest_marker": {"status": "merged", "ready": False},
            "source_worktree": {"path_state": "removed", "dirty_state": "clean"},
            "source_branch": {"exists": False, "contained_in_main": True},
            "latest_validation": "all checks passed",
            "remaining_risk": "none",
        }
        verdict = self.verdict(job_id, facts)
        self.assertEqual(verdict["state"], "completed")
        self.assertTrue(verdict["final_ready"])
        self.assertEqual(verdict["required_actions"], [])

    def test_semantically_wrong_tier_evidence_fails_closed(self) -> None:
        job_id = postgres_semantics.JOB_IDS[0]
        evidence = self.tier_evidence("crash-recovery")
        evidence["observed_rows"] = 0
        with self.assertRaisesRegex(postgres_semantics.SemanticError, "postcondition"):
            postgres_semantics.derive_job_verdict(
                job_id,
                self.fixture["jobs"][job_id]["facts"],
                "crash-recovery",
                evidence,
            )


class QualificationExecutionTests(unittest.TestCase):
    def test_compose_project_name_is_lowercase_portable_and_frozen(self) -> None:
        scenario = {"id": "j1-normal", "tier": "normal", "job_id": "J1"}
        step = {"id": "execute"}
        project = qualification.compose_project_name(
            "postgres",
            "qual-postgres-20260714T104328Z-90803",
            1,
            scenario,
            step,
        )
        self.assertEqual(project, "kf-qual-postgres-04328z-90803-r001-f1c7607ace")
        self.assertRegex(project, r"^[a-z0-9][a-z0-9_-]*$")
        self.assertLessEqual(len(project), 63)

    def test_all_production_project_names_are_unique_and_portable(self) -> None:
        plan = qualification.load_json(PRODUCTION_PLAN)
        projects = qualification.qualification_project_names(
            plan,
            "postgres",
            "qual-postgres-20260714T104328Z-90803",
        )
        expected_count = plan["repetitions"] * sum(
            len(scenario["steps"]) for scenario in plan["scenarios"]
        )
        self.assertEqual(len(projects), expected_count)
        self.assertEqual(len(projects), len(set(projects)))
        self.assertTrue(all(qualification.COMPOSE_PROJECT_NAME.fullmatch(project) for project in projects))

    def test_compose_preflight_fails_before_service_startup(self) -> None:
        failed = mock.Mock(returncode=1, stdout="", stderr="invalid project name")
        with mock.patch.object(qualification.subprocess, "run", return_value=failed) as run:
            with self.assertRaisesRegex(qualification.QualificationError, "before service startup"):
                qualification.preflight_compose_environment("postgres", "kf-qual-postgres-valid")
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["config", "--quiet"])
        self.assertNotIn("up", command)

    def test_compose_preflight_timeout_fails_closed(self) -> None:
        timeout = subprocess.TimeoutExpired(["docker", "compose"], 60, stderr=b"preflight timeout")
        with mock.patch.object(qualification.subprocess, "run", side_effect=timeout):
            with self.assertRaisesRegex(qualification.QualificationError, "preflight timeout"):
                qualification.preflight_compose_environment("postgres", "kf-qual-postgres-valid")

    def test_controlled_environment_drops_ad_hoc_compose_inputs(self) -> None:
        with mock.patch.dict(
            qualification.os.environ,
            {"PATH": "/usr/bin", "HOME": "/tmp/home", "COMPOSE_FILE": "replacement.yaml", "UNRELATED": "value"},
            clear=True,
        ):
            environment = qualification.controlled_environment()
        self.assertEqual(environment, {"PATH": "/usr/bin", "HOME": "/tmp/home"})

    def test_counted_compose_up_forces_pull_never(self) -> None:
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as temporary:
            project = postgres_adapter.ComposeProject(
                "kf-qual-postgres-pull-never",
                pathlib.Path(temporary),
            )
            with mock.patch.object(postgres_adapter.subprocess, "run", return_value=completed) as run:
                project.up()
            up_command = run.call_args_list[1].args[0]
            self.assertIn(["--pull", "never"], [up_command[index:index + 2] for index in range(len(up_command) - 1)])
            with self.assertRaisesRegex(postgres_adapter.AdapterError, "--pull never"):
                project.command("up", "-d")

    def test_cold_host_pull_retry_finishes_before_all_63_formal_steps(self) -> None:
        plan, resolved = qualification.resolve_plan(PRODUCTION_PLAN)
        self.assertEqual(
            plan["repetitions"] * sum(len(scenario["steps"]) for scenario in plan["scenarios"]),
            63,
        )
        locked_subject = resolved["subject"]["image"]
        runner_image = resolved["runner_image"]
        config = json.dumps(
            {
                "services": {
                    "postgres": {"image": locked_subject, "platform": "linux/amd64"},
                    "runner": {"image": runner_image, "platform": "linux/amd64"},
                }
            }
        )
        local_image = lambda image, fill: json.dumps(  # noqa: E731
            {"Id": f"sha256:{fill * 64}", "RepoDigests": [image]}
        )
        docker_results = iter(
            [
                subprocess.CompletedProcess([], 0, config, ""),
                subprocess.CompletedProcess([], 1, "", "connection reset by peer"),
                subprocess.CompletedProcess([], 0, "pulled\n", ""),
                subprocess.CompletedProcess([], 0, local_image(locked_subject, "a"), ""),
                subprocess.CompletedProcess([], 0, "pulled\n", ""),
                subprocess.CompletedProcess([], 0, local_image(runner_image, "b"), ""),
            ]
        )
        formal_steps: list[tuple[int, str]] = []

        def execute(*args: object, **kwargs: object) -> dict[str, object]:
            self.assertEqual(sleep.call_args_list, [mock.call(2)])
            formal_steps.append((int(args[4]), str(args[5]["id"])))
            return {"passed": True}

        with tempfile.TemporaryDirectory() as temporary:
            isolated_resolved = copy.deepcopy(resolved)
            isolated_resolved["artifact_destination"] = temporary
            with (
                mock.patch.object(qualification, "resolve_plan", return_value=(plan, isolated_resolved)),
                mock.patch.object(qualification, "require_clean_source"),
                mock.patch.object(qualification, "preflight_compose_environment"),
                mock.patch.object(qualification, "runtime_facts", return_value={}),
                mock.patch.object(qualification.subprocess, "run", side_effect=docker_results),
                mock.patch.object(qualification.time, "sleep") as sleep,
                mock.patch.object(qualification, "execute_step", side_effect=execute),
                mock.patch.object(
                    qualification,
                    "retention_results",
                    return_value=[{"pattern": "synthetic", "matches": ["synthetic"], "passed": True}],
                ),
                mock.patch.object(qualification, "verify_bundle"),
            ):
                bundle_dir = qualification.run_plan(PRODUCTION_PLAN)
                preparation = qualification.load_json(
                    bundle_dir / "preparation" / "image-preparation.json"
                )

        self.assertEqual(len(formal_steps), 63)
        self.assertEqual(sleep.call_args_list, [mock.call(2)])
        self.assertEqual(preparation["status"], "passed")
        self.assertTrue(preparation["unscored"])
        self.assertEqual([attempt["exit_code"] for attempt in preparation["images"][0]["attempts"]], [1, 0])

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
                    None,
                    "qualification-timeout-test",
                    1,
                    scenario,
                    step,
                    repetition_dir,
                )
            cleanup_command = run.call_args_list[1].args[0]
            execution_environment = run.call_args_list[0].kwargs["env"]
            project_index = cleanup_command.index("--project-name") + 1
            self.assertEqual(result["exit_code"], 124)
            self.assertEqual(result["cleanup_exit_code"], 0)
            self.assertFalse(result["passed"])
            self.assertIn("--project-name", cleanup_command)
            self.assertEqual(execution_environment["COMPARATOR_PROJECT_NAME"], cleanup_command[project_index])
            self.assertRegex(cleanup_command[project_index], r"^[a-z0-9][a-z0-9_-]*$")
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
        fixture_plan = qualification.load_json(PRODUCTION_PLAN)
        fixture_plan["scenarios"] = fixture_plan["scenarios"][:1]
        fixture_plan["artifact_retention"]["required_patterns"] = [
            "raw/**/adapter-receipt.json",
            "raw/**/observed-facts.json",
            "raw/**/tier-evidence.json",
            "raw/**/j1-decision-receipt.json",
        ]
        self.plan_path = self.bundle_dir / "qualification-plan.json"
        qualification.write_json(self.plan_path, fixture_plan)
        self.plan_sha = qualification.sha256_file(self.plan_path)
        self.contracts = qualification.copy_contracts(self.bundle_dir)
        locked_subject = qualification.load_json(qualification.LOCK_PATH)["profiles"]["postgres"]
        workload_adapter = {
            "registry_sha256": fixture_plan["adapter_registry"]["sha256"],
            "identity": fixture_plan["workload_adapter"],
        }
        self.bundle_inputs = qualification.copy_bundle_inputs(
            self.bundle_dir,
            {
                "runner_image": fixture_plan["environment"]["runner_image"],
                "subject": locked_subject,
                "workload_adapter": workload_adapter,
            },
        )
        self.build_sha = "c" * 40
        self.bundle_id = "synthetic-offline-verification"
        services = [
            {"service": "postgres", "image": locked_subject["image"], "platform": "linux/amd64"},
            {"service": "runner", "image": fixture_plan["environment"]["runner_image"], "platform": "linux/amd64"},
        ]
        discovery_output = json.dumps(
            {
                "services": {
                    service["service"]: {
                        "image": service["image"],
                        "platform": service["platform"],
                    }
                    for service in services
                }
            }
        )
        local_image = lambda image, fill: json.dumps(  # noqa: E731
            {"Id": f"sha256:{fill * 64}", "RepoDigests": [image]}
        )
        docker_results = iter(
            [
                subprocess.CompletedProcess([], 0, discovery_output, ""),
                subprocess.CompletedProcess([], 1, "", "connection reset by peer"),
                subprocess.CompletedProcess([], 0, "pulled\n", ""),
                subprocess.CompletedProcess([], 0, local_image(services[0]["image"], "1"), ""),
                subprocess.CompletedProcess([], 0, "pulled\n", ""),
                subprocess.CompletedProcess([], 0, local_image(services[1]["image"], "2"), ""),
            ]
        )
        preparation_dir = self.bundle_dir / "preparation"
        project = qualification.qualification_project_names(
            fixture_plan,
            "postgres",
            self.bundle_id,
        )[0]
        with (
            mock.patch.object(qualification.subprocess, "run", side_effect=docker_results),
            mock.patch.object(qualification.time, "sleep") as preparation_sleep,
        ):
            preparation_path = qualification.prepare_exact_images(
                "postgres",
                project,
                preparation_dir,
            )
        self.preparation_sleep_calls = preparation_sleep.call_args_list
        self.preparation = {
            "path": preparation_path.relative_to(self.bundle_dir).as_posix(),
            "sha256": qualification.sha256_file(preparation_path),
        }
        self.run_records = []
        scenario = fixture_plan["scenarios"][0]
        step = scenario["steps"][0]
        binding = qualification.workload_binding(
            fixture_plan["profile"],
            fixture_plan["adapter_registry"]["sha256"],
            fixture_plan["workload_adapter"],
            scenario,
            step,
        )
        self.fixture_plan = fixture_plan
        self.scenario = scenario
        self.step = step
        self.binding = binding
        adapter_evidence = {
            "id": fixture_plan["workload_adapter"]["id"],
            "version": fixture_plan["workload_adapter"]["version"],
            "entrypoint": fixture_plan["workload_adapter"]["entrypoint"],
            "artifact": fixture_plan["workload_adapter"]["artifact"],
            "artifact_sha256": fixture_plan["workload_adapter"]["sha256"],
            "semantics": fixture_plan["workload_adapter"]["semantics"],
            "fixture": fixture_plan["workload_adapter"]["fixture"],
            "oracle": fixture_plan["workload_adapter"]["oracle"],
            "registry_sha256": fixture_plan["adapter_registry"]["sha256"],
            "binding_sha256": binding["sha256"],
            "receipt_path": "adapter-receipt.json",
        }
        for repetition in range(1, 4):
            repetition_dir = self.bundle_dir / f"repetition-{repetition:03d}"
            raw = repetition_dir / "raw" / scenario["id"] / step["id"]
            raw.mkdir(parents=True)
            execution_fixture = qualification.load_json(
                ADAPTER_DIR / "fixtures" / "postgres-phase-a-v1.json"
            )
            job_input = execution_fixture["jobs"][scenario["job_id"]]
            observed_facts = {
                "schema": "urn:kungfu-systems:build-images:postgres-observed-facts:v1",
                "job_id": scenario["job_id"],
                "tier": scenario["tier"],
                "binding_sha256": binding["sha256"],
                "query_id": "execution-input-after-tier-event-v1",
                "facts": job_input["facts"],
                "facts_sha256": qualification.sha256_json(job_input["facts"]),
            }
            qualification.write_json(raw / "observed-facts.json", observed_facts)
            tier_evidence = {
                "schema": "urn:kungfu-systems:build-images:postgres-tier-evidence:v1",
                "job_id": scenario["job_id"],
                "tier": scenario["tier"],
                "binding_sha256": binding["sha256"],
                "operation": "current-fact-query",
                "observed_rows": 1,
                "observed_facts_sha256": observed_facts["facts_sha256"],
            }
            qualification.write_json(
                raw / "tier-evidence.json",
                tier_evidence,
            )
            qualification.write_json(
                raw / "j1-decision-receipt.json",
                postgres_adapter.job_receipt(
                    scenario["job_id"],
                    job_input["fixture_id"],
                    job_input["facts"],
                    scenario["tier"],
                    binding["sha256"],
                    tier_evidence,
                    qualification.sha256_file(raw / "observed-facts.json"),
                    qualification.sha256_file(raw / "tier-evidence.json"),
                ),
            )
            qualification.write_json(
                raw / "adapter-receipt.json",
                {
                    "schema": "urn:kungfu-systems:build-images:workload-adapter-receipt:v1",
                    "status": "passed",
                    **binding["payload"],
                    "binding_sha256": binding["sha256"],
                    "adapter_artifact": fixture_plan["workload_adapter"]["artifact"],
                    "semantics_artifact": fixture_plan["workload_adapter"]["semantics"]["path"],
                    "fixture_path": fixture_plan["workload_adapter"]["fixture"]["path"],
                    "oracle_sha256": fixture_plan["workload_adapter"]["oracle"]["sha256"],
                    "job_receipt": "j1-decision-receipt.json",
                    "observed_facts": "observed-facts.json",
                    "tier_evidence": "tier-evidence.json",
                },
            )
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
                "profile": "postgres",
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
                    "image_preparation_sha256": self.preparation["sha256"],
                    "subject": locked_subject,
                    "subject_sha256": qualification.sha256_json(locked_subject),
                    "workload_adapter": workload_adapter,
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
                        "postgres": {"mem_limit": "2g", "cpus": "1.00"},
                    },
                },
                "steps": [
                    {
                        "scenario_id": scenario["id"],
                        "job_id": scenario["job_id"],
                        "tier": scenario["tier"],
                        "step_id": step["id"],
                        "action": step["action"],
                        "adapter": copy.deepcopy(adapter_evidence),
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
                            step["oracles"],
                            {
                                "job_id": scenario["job_id"],
                                "tier": scenario["tier"],
                                "binding_sha256": binding["sha256"],
                                "fixture_id": job_input["fixture_id"],
                                "semantics_path": ADAPTER_DIR / "postgres_phase_a_semantics.py",
                                "semantics_sha256": fixture_plan["workload_adapter"]["semantics"]["sha256"],
                                "oracle_path": ADAPTER_DIR / "oracles" / "postgres-phase-a-v1.json",
                            },
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
            "profile": "postgres",
            "configuration_slot": "realistic-default",
            "build_images_git_sha": self.build_sha,
            "inputs": self.bundle_inputs,
            "plan": {"path": self.plan_path.name, "sha256": self.plan_sha},
            "preparation": self.preparation,
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

    def test_retry_preparation_artifacts_are_unique_and_verifiable(self) -> None:
        preparation = qualification.load_json(
            self.bundle_dir / "preparation" / "image-preparation.json"
        )
        artifact_paths = [
            preparation["discovery"][stream]["path"]
            for stream in ("stdout", "stderr")
        ]
        for image in preparation["images"]:
            for attempt in image["attempts"]:
                artifact_paths.extend(attempt[stream]["path"] for stream in ("stdout", "stderr"))
            artifact_paths.extend(image["local"][stream]["path"] for stream in ("stdout", "stderr"))

        self.assertEqual(self.preparation_sleep_calls, [mock.call(2)])
        self.assertEqual(
            [attempt["exit_code"] for attempt in preparation["images"][0]["attempts"]],
            [1, 0],
        )
        self.assertIn("image-01.pull-01.stderr.log", artifact_paths)
        self.assertIn("image-01.pull-02.stderr.log", artifact_paths)
        self.assertIn("image-01.inspect.stderr.log", artifact_paths)
        self.assertEqual(len(artifact_paths), len(set(artifact_paths)))
        qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_altered_raw_evidence(self) -> None:
        target = self.bundle_dir / "repetition-002" / "raw" / "j1-normal" / "execute" / "adapter-receipt.json"
        target.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(qualification.QualificationError, "receipt binding"):
            qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_missing_repetition(self) -> None:
        bundle_path = self.bundle_dir / "bundle-manifest.json"
        bundle = qualification.load_json(bundle_path)
        bundle["run_manifests"] = bundle["run_manifests"][:2]
        qualification.write_json(bundle_path, bundle)
        with self.assertRaisesRegex(qualification.QualificationError, "exactly one run manifest"):
            qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_unrecorded_artifact(self) -> None:
        target = self.bundle_dir / "repetition-003" / "raw" / "j1-normal" / "execute" / "unrecorded.txt"
        target.write_text("not in the manifest\n", encoding="utf-8")
        with self.assertRaisesRegex(qualification.QualificationError, "artifact inventory is incomplete"):
            qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_altered_frozen_input(self) -> None:
        target = self.bundle_dir / "inputs" / "compose.yaml"
        target.write_text(target.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(qualification.QualificationError, "bundle input digest mismatch"):
            qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_altered_failed_retry_log(self) -> None:
        target = self.bundle_dir / "preparation" / "image-01.pull-01.stderr.log"
        target.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(qualification.QualificationError, "preparation artifact integrity"):
            qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_rebound_subject(self) -> None:
        bundle_path = self.bundle_dir / "bundle-manifest.json"
        bundle = qualification.load_json(bundle_path)
        bundle["inputs"]["subject"]["status"] = "tampered"
        bundle["inputs"]["subject_sha256"] = qualification.sha256_json(bundle["inputs"]["subject"])
        qualification.write_json(bundle_path, bundle)
        with self.assertRaisesRegex(qualification.QualificationError, "subject does not match"):
            qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_relabelled_job_receipt(self) -> None:
        repetition_dir = self.bundle_dir / "repetition-001"
        target = repetition_dir / "raw" / "j1-normal" / "execute" / "j1-decision-receipt.json"
        receipt = qualification.load_json(target)
        receipt["job_id"] = "J2-cross-repo-delivery-trust"
        qualification.write_json(target, receipt)
        self._refresh_run_manifest(1)
        with self.assertRaisesRegex(qualification.QualificationError, "evidence is relabelled"):
            qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_tampered_adapter_artifact(self) -> None:
        record = qualification.load_json(self.bundle_dir / "bundle-manifest.json")["inputs"]["workload_adapter"]
        target = self.bundle_dir / record["artifact"]["path"]
        target.write_text(target.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(qualification.QualificationError, "adapter digest mismatch"):
            qualification.verify_bundle(self.bundle_dir)

    def test_offline_verification_rejects_semantically_contradictory_observations_with_refreshed_hashes(self) -> None:
        raw = self.bundle_dir / "repetition-001" / "raw" / "j1-normal" / "execute"
        observed_path = raw / "observed-facts.json"
        tier_path = raw / "tier-evidence.json"
        receipt_path = raw / "j1-decision-receipt.json"
        observed = qualification.load_json(observed_path)
        observed["facts"] = {"sessions": []}
        observed["facts_sha256"] = qualification.sha256_json(observed["facts"])
        qualification.write_json(observed_path, observed)
        tier = qualification.load_json(tier_path)
        tier["observed_facts_sha256"] = observed["facts_sha256"]
        qualification.write_json(tier_path, tier)
        receipt = qualification.load_json(receipt_path)
        receipt["observed_facts_sha256"] = observed["facts_sha256"]
        receipt["observed_facts_artifact_sha256"] = qualification.sha256_file(observed_path)
        receipt["tier_evidence_sha256"] = qualification.sha256_file(tier_path)
        qualification.write_json(receipt_path, receipt)
        self._refresh_run_manifest(1)
        with self.assertRaisesRegex(qualification.QualificationError, "oracle evidence"):
            qualification.verify_bundle(self.bundle_dir)

    def test_mutating_only_the_oracle_does_not_change_the_execution_receipt(self) -> None:
        raw = self.bundle_dir / "repetition-001" / "raw" / "j1-normal" / "execute"
        receipt_path = raw / "j1-decision-receipt.json"
        receipt_before = receipt_path.read_bytes()
        oracle = qualification.load_json(ADAPTER_DIR / "oracles" / "postgres-phase-a-v1.json")
        oracle["jobs"][self.scenario["job_id"]]["state"] = "healthy"
        oracle_path = self.bundle_dir / "mutated-oracle.json"
        qualification.write_json(oracle_path, oracle)
        execution_fixture = qualification.load_json(
            ADAPTER_DIR / "fixtures" / "postgres-phase-a-v1.json"
        )
        with self.assertRaisesRegex(qualification.QualificationError, "verifier-only oracle"):
            qualification.verify_semantic_evidence(
                raw,
                {
                    "job_id": self.scenario["job_id"],
                    "tier": self.scenario["tier"],
                    "binding_sha256": self.binding["sha256"],
                    "fixture_id": execution_fixture["jobs"][self.scenario["job_id"]]["fixture_id"],
                    "semantics_path": ADAPTER_DIR / "postgres_phase_a_semantics.py",
                    "semantics_sha256": qualification.sha256_file(
                        ADAPTER_DIR / "postgres_phase_a_semantics.py"
                    ),
                    "oracle_path": oracle_path,
                },
            )
        self.assertEqual(receipt_path.read_bytes(), receipt_before)

    def test_semantic_verification_rejects_a_different_implementation(self) -> None:
        raw = self.bundle_dir / "repetition-001" / "raw" / "j1-normal" / "execute"
        execution_fixture = qualification.load_json(
            ADAPTER_DIR / "fixtures" / "postgres-phase-a-v1.json"
        )
        different_semantics = self.bundle_dir / "different-semantics.py"
        different_semantics.write_text("# different verifier\n", encoding="utf-8")
        with self.assertRaisesRegex(qualification.QualificationError, "frozen bundle"):
            qualification.verify_semantic_evidence(
                raw,
                {
                    "job_id": self.scenario["job_id"],
                    "tier": self.scenario["tier"],
                    "binding_sha256": self.binding["sha256"],
                    "fixture_id": execution_fixture["jobs"][self.scenario["job_id"]]["fixture_id"],
                    "semantics_path": different_semantics,
                    "semantics_sha256": qualification.sha256_file(different_semantics),
                    "oracle_path": ADAPTER_DIR / "oracles" / "postgres-phase-a-v1.json",
                },
            )

    def _refresh_run_manifest(self, repetition: int) -> None:
        repetition_dir = self.bundle_dir / f"repetition-{repetition:03d}"
        run_path = repetition_dir / "run-manifest.json"
        run = qualification.load_json(run_path)
        run["artifacts"] = qualification.artifact_records(repetition_dir, exclude={"run-manifest.json"})
        qualification.write_json(run_path, run)
        bundle_path = self.bundle_dir / "bundle-manifest.json"
        bundle = qualification.load_json(bundle_path)
        bundle["run_manifests"][repetition - 1]["sha256"] = qualification.sha256_file(run_path)
        qualification.write_json(bundle_path, bundle)


if __name__ == "__main__":
    unittest.main()
