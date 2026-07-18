#!/usr/bin/env python3
"""Run the fixed packaged-Kungfu Phase B workload in one Compose project."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any

from kungfu_phase_b_semantics import (
    DECISION_LOGIC_VERSION,
    JOB_RECEIPT_SCHEMA,
    OBSERVED_FACTS_SCHEMA,
    SEMANTICS_MODULE_ID,
    SemanticError,
    derive_job_verdict,
    validate_execution_fixture,
)

ADAPTER_ID = "kungfu-phase-b-v1"
ADAPTER_VERSION = "1.0.0"
ENTRYPOINT = "kungfu-phase-b-v1"
ACTION = "workload-adapter"
SCRIPT_PATH = pathlib.Path(__file__).resolve()
ADAPTER_DIR = SCRIPT_PATH.parent
PILOT_DIR = ADAPTER_DIR.parent
ARTIFACT_ROOT = PILOT_DIR / ".artifacts"
COMPOSE_PATH = PILOT_DIR / "compose.yaml"
REGISTRY_PATH = ADAPTER_DIR / "registry.json"
PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TIERS = (
    "normal",
    "concurrent",
    "crash-recovery",
    "whole-root-restore",
    "historical-query",
    "schema-evolution",
    "new-agent-takeover",
)
JOB_RECEIPTS = {
    "J1-multi-session-progress-triage": "j1-decision-receipt.json",
    "J2-cross-repo-delivery-trust": "j2-delivery-receipt.json",
    "J3-interrupted-go-recovery-handoff": "j3-handoff-receipt.json",
}
COMPOSE_COMMAND_TIMEOUT_SECONDS = 180
FACT_TYPE = "qualification-job-facts"
FACT_HOME = "/var/lib/kungfu/primary"
FACT_BUNDLE = "/var/lib/kungfu/qualification-library.json"


class AdapterError(ValueError):
    """A fixed adapter contract or execution failure."""


def timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AdapterError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise AdapterError(f"JSON root must be an object: {path}")
    return value


def parse_json_output(value: str, context: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except json.JSONDecodeError as error:
        raise AdapterError(f"{context} did not return JSON: {error}") from error
    if not isinstance(result, dict):
        raise AdapterError(f"{context} JSON root must be an object")
    return result


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def adapter_binding(
    registry_sha256: str,
    adapter: dict[str, Any],
    scenario_id: str,
    job_id: str,
    tier: str,
    step_id: str,
) -> dict[str, Any]:
    payload = {
        "schema": "urn:kungfu-systems:build-images:workload-binding:v2",
        "profile": "kungfu",
        "scenario_id": scenario_id,
        "job_id": job_id,
        "tier": tier,
        "step_id": step_id,
        "action": ACTION,
        "adapter_id": ADAPTER_ID,
        "adapter_version": adapter["version"],
        "adapter_entrypoint": adapter["entrypoint"],
        "adapter_sha256": adapter["sha256"],
        "semantics_sha256": adapter["semantics"]["sha256"],
        "fixture_sha256": adapter["fixture"]["sha256"],
        "oracle_sha256": adapter["oracle"]["sha256"],
        "registry_sha256": registry_sha256,
    }
    return {"payload": payload, "sha256": sha256_json(payload)}


def validate_adapter(job_id: str, tier: str) -> tuple[dict[str, Any], pathlib.Path, dict[str, Any], str]:
    registry = load_json(REGISTRY_PATH)
    if registry.get("schema") != "urn:kungfu-systems:build-images:workload-adapter-registry:v2":
        raise AdapterError("workload adapter registry schema is unsupported")
    adapter = registry.get("adapters", {}).get(ADAPTER_ID)
    if not isinstance(adapter, dict):
        raise AdapterError(f"adapter is not registered: {ADAPTER_ID}")
    if adapter.get("version") != ADAPTER_VERSION or adapter.get("entrypoint") != ENTRYPOINT:
        raise AdapterError("adapter version or entrypoint does not match the fixed implementation")
    if adapter.get("artifact") != "workload-adapters/kungfu_phase_b_v1.py":
        raise AdapterError("adapter artifact path is not allowlisted")
    if adapter.get("sha256") != sha256_file(SCRIPT_PATH):
        raise AdapterError("adapter source digest does not match the registry")
    semantics_record = adapter.get("semantics")
    semantics_path = ADAPTER_DIR / "kungfu_phase_b_semantics.py"
    if (
        not isinstance(semantics_record, dict)
        or semantics_record.get("module") != SEMANTICS_MODULE_ID
        or semantics_record.get("path") != "workload-adapters/kungfu_phase_b_semantics.py"
    ):
        raise AdapterError("adapter semantics path is not allowlisted")
    if semantics_record.get("sha256") != sha256_file(semantics_path):
        raise AdapterError("adapter semantics digest does not match the registry")
    fixture_record = adapter.get("fixture")
    expected_fixture = "workload-adapters/fixtures/kungfu-phase-b-v1.json"
    if not isinstance(fixture_record, dict) or fixture_record.get("path") != expected_fixture:
        raise AdapterError("adapter fixture path is not allowlisted")
    fixture_path = PILOT_DIR / expected_fixture
    if fixture_record.get("sha256") != sha256_file(fixture_path):
        raise AdapterError("adapter fixture digest does not match the registry")
    oracle_record = adapter.get("oracle")
    if (
        not isinstance(oracle_record, dict)
        or oracle_record.get("path") != "workload-adapters/oracles/kungfu-phase-b-v1.json"
        or not SHA256.fullmatch(str(oracle_record.get("sha256", "")))
    ):
        raise AdapterError("adapter oracle identity is not allowlisted")
    allowed = {
        (mapping.get("job_id"), allowed_tier)
        for mapping in adapter.get("mappings", [])
        if isinstance(mapping, dict)
        for allowed_tier in mapping.get("tiers", [])
    }
    if (job_id, tier) not in allowed:
        raise AdapterError(f"job/tier is not mapped by {ADAPTER_ID}: {job_id}/{tier}")
    fixture = load_json(fixture_path)
    try:
        validate_execution_fixture(fixture)
    except SemanticError as error:
        raise AdapterError(str(error)) from error
    if job_id not in fixture.get("jobs", {}):
        raise AdapterError(f"fixture does not define job: {job_id}")
    return adapter, fixture_path, fixture, sha256_file(REGISTRY_PATH)


class ComposeProject:
    def __init__(self, project: str, output_dir: pathlib.Path) -> None:
        self.project = project
        self.output_dir = output_dir
        self.command_index = 0

    def _run_fixed_command(
        self,
        command: list[str],
        recorded_argv: list[str],
        operation: str,
        *,
        input_text: str | None = None,
        check: bool = True,
        record: bool = True,
        timeout_seconds: int = COMPOSE_COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        timed_out = False
        try:
            process = subprocess.run(
                command,
                cwd=PILOT_DIR,
                env={**os.environ, "COMPARATOR_PROJECT_NAME": self.project},
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            timed_out = True
            process = subprocess.CompletedProcess(
                command,
                124,
                timeout_output(error.stdout),
                timeout_output(error.stderr) + f"\nfixed command timed out: {operation}\n",
            )
        if record:
            self.command_index += 1
            prefix = self.output_dir / "commands" / f"{self.command_index:03d}"
            prefix.parent.mkdir(parents=True, exist_ok=True)
            prefix.with_suffix(".stdout.log").write_text(process.stdout, encoding="utf-8")
            prefix.with_suffix(".stderr.log").write_text(process.stderr, encoding="utf-8")
            write_json(
                prefix.with_suffix(".json"),
                {
                    "argv": recorded_argv,
                    "exit_code": process.returncode,
                    "stdin_supplied": input_text is not None,
                    "timed_out": timed_out,
                    "timeout_seconds": timeout_seconds,
                },
            )
        if timed_out:
            raise AdapterError(f"fixed command timed out: {operation}")
        if check and process.returncode != 0:
            raise AdapterError(f"fixed command failed ({process.returncode}): {operation}")
        return process

    def command(
        self,
        *arguments: str,
        input_text: str | None = None,
        check: bool = True,
        record: bool = True,
        timeout_seconds: int = COMPOSE_COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        if arguments and arguments[0] == "up":
            pull_index = arguments.index("--pull") if "--pull" in arguments else -1
            if pull_index < 0 or pull_index + 1 >= len(arguments) or arguments[pull_index + 1] != "never":
                raise AdapterError("counted Compose up commands must use --pull never")
            if "--no-build" not in arguments:
                raise AdapterError("counted Kungfu Compose up commands must use --no-build")
        command = [
            "docker", "compose", "-f", str(COMPOSE_PATH),
            "--project-name", self.project, "--profile", "kungfu", *arguments,
        ]
        return self._run_fixed_command(
            command,
            command[:3] + ["<fixed-compose>"] + command[4:],
            " ".join(arguments),
            input_text=input_text,
            check=check,
            record=record,
            timeout_seconds=timeout_seconds,
        )

    def kungfu(
        self,
        *arguments: str,
        input_text: str | None = None,
        record: bool = True,
    ) -> dict[str, Any]:
        process = self.command(
            "exec", "-T", "kungfu", "kungfu", "-H", FACT_HOME, *arguments,
            input_text=input_text,
            record=record,
        )
        return parse_json_output(process.stdout, "packaged Kungfu CLI")

    def container_id(self) -> str:
        container_id = self.command("ps", "-q", "kungfu").stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise AdapterError("fixed Compose Kungfu service has no valid container id")
        return container_id

    def wait_container(self, container_id: str) -> int:
        process = self._run_fixed_command(
            ["docker", "wait", container_id],
            ["docker", "wait", "<kungfu-container>"],
            "wait kungfu",
        )
        try:
            return int(process.stdout.strip())
        except ValueError as error:
            raise AdapterError("docker wait returned an invalid Kungfu exit code") from error

    def up(self) -> None:
        self.command("config", "--quiet")
        self.command("up", "-d", "--wait", "--pull", "never", "--no-build", "runner", "kungfu")
        expected_image_id = os.environ.get("KUNGFU_CLI_IMAGE_ID", "")
        if not SHA256.fullmatch(expected_image_id.removeprefix("sha256:")):
            raise AdapterError("prepared Kungfu image ID is missing from the fixed execution environment")
        container_id = self.container_id()
        inspected = self._run_fixed_command(
            ["docker", "inspect", "--format", "{{.Image}}", container_id],
            ["docker", "inspect", "--format", "{{.Image}}", "<kungfu-container>"],
            "inspect Kungfu container image",
        ).stdout.strip()
        if inspected != expected_image_id:
            raise AdapterError("running Kungfu container image does not match preparation evidence")


def fact_schema(revision: int) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["schema_revision", "facts"],
        "properties": {
            "schema_revision": {"type": "integer"},
            "facts": {"type": "object"},
        },
        "additionalProperties": False,
    }


def create_type(project: ComposeProject, revision: int, *, evolution: bool) -> dict[str, Any]:
    arguments = [
        "facts", "type", "create",
        "--id", FACT_TYPE,
        "--version", f"v{revision}",
        "--source", "build-images",
        "--source", "agent-a",
        "--source", "agent-b",
        "--schema-file", "-",
    ]
    if evolution and revision == 1:
        arguments += ["--effective-from", "0", "--effective-until", "2", "--system-time", "1"]
    elif evolution and revision == 2:
        arguments += ["--effective-from", "2", "--system-time", "2"]
    result = project.kungfu(*arguments, input_text=json.dumps(fact_schema(revision), sort_keys=True))
    if result.get("ok") is not True or result.get("status") != "created":
        raise AdapterError(f"Kungfu fact type v{revision} was not created")
    return result


def put_material(
    project: ComposeProject,
    *,
    revision: int,
    source: str,
    subject: str,
    observation_id: str,
    facts: dict[str, Any],
    action: str = "assert",
    target: str = "",
    system_time: int | None = None,
    record: bool = True,
) -> dict[str, Any]:
    arguments = [
        "facts", "material", "put",
        "--type", FACT_TYPE,
        "--type-version", f"v{revision}",
        "--source", source,
        "--subject", subject,
        "--payload-file", "-",
        "--observation-id", observation_id,
        "--action", action,
    ]
    if target:
        arguments += ["--target", target]
    if system_time is not None:
        arguments += ["--system-time", str(system_time)]
    payload = {"schema_revision": revision, "facts": facts}
    result = project.kungfu(
        *arguments,
        input_text=json.dumps(payload, sort_keys=True),
        record=record,
    )
    if result.get("ok") is not True or result.get("receipt", {}).get("admission", {}).get("outcome") != "admitted":
        raise AdapterError(f"Kungfu fact material was not admitted: {observation_id}")
    return result


def material_catalog(project: ComposeProject) -> dict[str, Any]:
    catalog = project.kungfu("facts", "material", "list", "--type", FACT_TYPE)
    if catalog.get("schema") != "kungfu.facts.material-catalog/v1":
        raise AdapterError("Kungfu material catalog schema is invalid")
    return catalog


def observed_payload(catalog: dict[str, Any], subject: str) -> dict[str, Any]:
    state = catalog.get("state")
    payloads = catalog.get("payloads")
    if not isinstance(state, dict) or not isinstance(payloads, dict):
        raise AdapterError("Kungfu material catalog is incomplete")
    matches = [
        fact for fact in state.get("canonical_facts", [])
        if isinstance(fact, dict) and fact.get("subject_key") == subject
    ]
    if len(matches) != 1:
        raise AdapterError(f"Kungfu material catalog has no unique canonical subject: {subject}")
    payload = payloads.get(matches[0].get("payload_hash"))
    if not isinstance(payload, dict) or not isinstance(payload.get("facts"), dict):
        raise AdapterError("Kungfu canonical material payload is unavailable")
    return payload["facts"]


def exercise_tier(
    project: ComposeProject,
    job_id: str,
    tier: str,
    facts: dict[str, Any],
    binding_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evolution = tier == "schema-evolution"
    create_type(project, 1, evolution=evolution)
    initial_observation = f"obs-{binding_sha256[:24]}-v1"
    put_material(
        project,
        revision=1,
        source="build-images",
        subject=job_id,
        observation_id=initial_observation,
        facts=facts,
        system_time=1 if evolution else None,
    )

    if tier == "normal":
        catalog = material_catalog(project)
        canonical_count = len(catalog["state"]["canonical_facts"])
        evidence = {"operation": "fact-library-current-query", "canonical_count": canonical_count}
    elif tier == "concurrent":
        def write_agent(source: str) -> tuple[str, subprocess.CompletedProcess[str]]:
            observation = f"obs-{binding_sha256[:16]}-{source}"
            arguments = [
                "exec", "-T", "kungfu", "kungfu", "-H", FACT_HOME,
                "facts", "material", "put",
                "--type", FACT_TYPE,
                "--type-version", "v1",
                "--source", source,
                "--subject", f"{job_id}-{source}",
                "--payload-file", "-",
                "--observation-id", observation,
                "--action", "assert",
            ]
            payload = json.dumps({"schema_revision": 1, "facts": facts}, sort_keys=True)
            return source, project.command(
                *arguments,
                input_text=payload,
                check=False,
                record=False,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write_agent, ("agent-a", "agent-b")))
        for source, process in results:
            (project.output_dir / f"concurrent-{source}.stdout.log").write_text(process.stdout, encoding="utf-8")
            (project.output_dir / f"concurrent-{source}.stderr.log").write_text(process.stderr, encoding="utf-8")
            if process.returncode != 0:
                raise AdapterError(
                    f"concurrent writer failed ({process.returncode}): {source}: "
                    f"{process.stderr.strip()}"
                )
            result = parse_json_output(process.stdout, f"concurrent writer {source}")
            if result.get("ok") is not True:
                raise AdapterError(f"concurrent writer was not admitted: {source}")
        catalog = material_catalog(project)
        evidence = {
            "operation": "two-agent-concurrent-material-write",
            "canonical_count": len(catalog["state"]["canonical_facts"]),
            "writers": 2,
        }
    elif tier == "crash-recovery":
        container_id = project.container_id()
        project.command("kill", "-s", "SIGKILL", "kungfu")
        exit_code = project.wait_container(container_id)
        if exit_code != 137:
            raise AdapterError(f"crash-recovery Kungfu exit code is not SIGKILL: {exit_code}")
        project.up()
        catalog = material_catalog(project)
        evidence = {"operation": "sigkill-restart-fact-query", "crash_exit_code": exit_code}
    elif tier == "whole-root-restore":
        export_result = project.kungfu("facts", "export", "--out", FACT_BUNDLE)
        if export_result.get("ok") is not True or export_result.get("mode") != "full":
            raise AdapterError("Kungfu Fact Library export did not complete")
        backup_path = project.output_dir / "whole-root-fact-library.json"
        project.command("cp", f"kungfu:{FACT_BUNDLE}", str(backup_path))
        project.command("down", "--volumes", "--remove-orphans")
        project.up()
        project.command("cp", str(backup_path), f"kungfu:{FACT_BUNDLE}")
        imported = project.kungfu("facts", "import", "--file", FACT_BUNDLE, "--execute")
        if imported.get("ok") is not True:
            raise AdapterError("Kungfu Fact Library import did not complete")
        catalog = material_catalog(project)
        evidence = {
            "operation": "whole-volume-recreate-and-library-import",
            "imported": True,
            "backup_sha256": sha256_file(backup_path),
        }
    elif tier == "historical-query":
        put_material(
            project,
            revision=1,
            source="build-images",
            subject=job_id,
            observation_id=f"obs-{binding_sha256[:24]}-v2",
            facts=facts,
            action="correct",
            target=initial_observation,
        )
        catalog = material_catalog(project)
        evidence = {
            "operation": "fact-history-and-head-query",
            "history_count": len(catalog["state"]["observation_history"]),
            "canonical_count": len(catalog["state"]["canonical_facts"]),
        }
    elif tier == "schema-evolution":
        create_type(project, 2, evolution=True)
        put_material(
            project,
            revision=2,
            source="build-images",
            subject=job_id,
            observation_id=f"obs-{binding_sha256[:24]}-v2",
            facts=facts,
            system_time=2,
        )
        catalog = material_catalog(project)
        evidence = {
            "operation": "non-overlapping-v1-v2-schema-evolution",
            "versions": sorted(item["version"] for item in catalog["state"]["catalog"]["fact_surfaces"]),
            "admitted_count": catalog["state"]["admission_outcomes"].get("admitted", 0),
        }
    elif tier == "new-agent-takeover":
        catalog = material_catalog(project)
        evidence = {"operation": "fresh-agent-process-query", "reader": "agent-b"}
    else:
        raise AdapterError(f"unsupported tier: {tier}")

    observed = observed_payload(catalog, job_id)
    evidence["facts_observed"] = observed == facts
    return observed, evidence


def job_receipt(
    job_id: str,
    fixture_id: str,
    facts: dict[str, Any],
    tier: str,
    binding_sha256: str,
    tier_evidence: dict[str, Any],
    observed_facts_artifact_sha256: str,
    tier_evidence_sha256: str,
) -> dict[str, Any]:
    verdict = derive_job_verdict(job_id, facts, tier, tier_evidence)
    return {
        "schema": JOB_RECEIPT_SCHEMA,
        "job_id": job_id,
        "tier": tier,
        "fixture_id": fixture_id,
        "binding_sha256": binding_sha256,
        "decision_logic_version": DECISION_LOGIC_VERSION,
        "observed_facts_sha256": sha256_json(facts),
        "observed_facts_artifact_sha256": observed_facts_artifact_sha256,
        "tier_evidence_sha256": tier_evidence_sha256,
        **verdict,
    }


def run(args: argparse.Namespace) -> pathlib.Path:
    for field in ("scenario_id", "job_id", "tier", "step_id"):
        if not IDENTIFIER.fullmatch(getattr(args, field)):
            raise AdapterError(f"{field} must be an identifier")
    if not PROJECT_ID.fullmatch(args.project):
        raise AdapterError("project must be a Compose-safe identifier")
    if args.tier not in TIERS or args.job_id not in JOB_RECEIPTS:
        raise AdapterError("job/tier is unsupported")
    if not SHA256.fullmatch(args.expected_binding_sha256):
        raise AdapterError("expected binding must be a lowercase SHA-256")
    output_dir = args.output_dir.resolve()
    if ARTIFACT_ROOT.resolve() not in output_dir.parents:
        raise AdapterError("output directory must stay inside the comparator artifact root")
    output_dir.mkdir(parents=True, exist_ok=True)

    adapter, fixture_path, fixture, registry_sha256 = validate_adapter(args.job_id, args.tier)
    binding = adapter_binding(
        registry_sha256, adapter, args.scenario_id, args.job_id, args.tier, args.step_id
    )
    if binding["sha256"] != args.expected_binding_sha256:
        raise AdapterError("runner binding does not match the adapter inputs")
    facts = fixture["jobs"][args.job_id]["facts"]
    project = ComposeProject(args.project, output_dir)
    project.up()
    observed, tier_evidence = exercise_tier(
        project, args.job_id, args.tier, facts, binding["sha256"]
    )
    tier_evidence.update(
        {
            "schema": "urn:kungfu-systems:build-images:kungfu-tier-evidence:v1",
            "job_id": args.job_id,
            "tier": args.tier,
            "binding_sha256": binding["sha256"],
        }
    )
    observed_facts = {
        "schema": OBSERVED_FACTS_SCHEMA,
        "job_id": args.job_id,
        "tier": args.tier,
        "binding_sha256": binding["sha256"],
        "query_id": "execution-input-after-tier-event-v1",
        "facts": observed,
        "facts_sha256": sha256_json(observed),
    }
    observed_facts_path = output_dir / "observed-facts.json"
    write_json(observed_facts_path, observed_facts)
    tier_evidence["observed_facts_sha256"] = observed_facts["facts_sha256"]
    tier_evidence_path = output_dir / "tier-evidence.json"
    write_json(tier_evidence_path, tier_evidence)
    receipt_name = JOB_RECEIPTS[args.job_id]
    write_json(
        output_dir / receipt_name,
        job_receipt(
            args.job_id,
            fixture["jobs"][args.job_id]["fixture_id"],
            observed,
            args.tier,
            binding["sha256"],
            tier_evidence,
            sha256_file(observed_facts_path),
            sha256_file(tier_evidence_path),
        ),
    )
    write_json(
        output_dir / "adapter-receipt.json",
        {
            "schema": "urn:kungfu-systems:build-images:workload-adapter-receipt:v1",
            "status": "passed",
            **binding["payload"],
            "binding_sha256": binding["sha256"],
            "adapter_artifact": adapter["artifact"],
            "semantics_artifact": adapter["semantics"]["path"],
            "fixture_path": adapter["fixture"]["path"],
            "oracle_sha256": adapter["oracle"]["sha256"],
            "job_receipt": receipt_name,
            "observed_facts": "observed-facts.json",
            "tier_evidence": "tier-evidence.json",
        },
    )
    write_json(
        output_dir / "adapter-inputs.json",
        {
            "adapter": adapter,
            "registry_sha256": registry_sha256,
            "fixture_sha256": sha256_file(fixture_path),
            "binding": binding,
        },
    )
    print(json.dumps({"status": "passed", "binding_sha256": binding["sha256"]}, sort_keys=True))
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--job-id", required=True)
    plan.add_argument("--tier", required=True)
    execute = subparsers.add_parser("run")
    execute.add_argument("--project", required=True)
    execute.add_argument("--scenario-id", required=True)
    execute.add_argument("--job-id", required=True)
    execute.add_argument("--tier", required=True)
    execute.add_argument("--step-id", required=True)
    execute.add_argument("--expected-binding-sha256", required=True)
    execute.add_argument("--output-dir", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            adapter, _, _, registry_sha256 = validate_adapter(args.job_id, args.tier)
            print(json.dumps({"adapter": adapter, "registry_sha256": registry_sha256}, indent=2, sort_keys=True))
        else:
            run(args)
    except (AdapterError, OSError, subprocess.SubprocessError) as error:
        print(f"kungfu phase-b adapter error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
