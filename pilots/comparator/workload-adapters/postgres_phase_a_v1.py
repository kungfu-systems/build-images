#!/usr/bin/env python3
"""Run the fixed PostgreSQL Phase A workload inside one comparator Compose project."""

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
import time
from typing import Any

from postgres_phase_a_semantics import (
    DECISION_LOGIC_VERSION,
    JOB_RECEIPT_SCHEMA,
    OBSERVED_FACTS_SCHEMA,
    SEMANTICS_MODULE_ID,
    SemanticError,
    derive_job_verdict,
    validate_execution_fixture,
)

ADAPTER_ID = "postgres-phase-a-v1"
ADAPTER_VERSION = "1.2.0"
ENTRYPOINT = "postgres-phase-a-v1"
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
COMPOSE_COMMAND_TIMEOUT_SECONDS = 120


class AdapterError(ValueError):
    """A fixed adapter contract or execution failure."""


def wait_for_cgroup_sampler() -> None:
    value = os.environ.get("FORMAL_PERFORMANCE_CGROUP_SAMPLER_READY_FILE", "")
    if not value:
        return
    ready_path = pathlib.Path(value)
    if not ready_path.is_absolute():
        raise AdapterError("cgroup sampler ready marker must be an absolute path")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if ready_path.is_file():
            ready_path.unlink()
            return
        time.sleep(0.01)
    raise AdapterError("cgroup sampler did not retain two baseline samples")


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


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


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
        "profile": "postgres",
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
    if adapter.get("artifact") != "workload-adapters/postgres_phase_a_v1.py":
        raise AdapterError("adapter artifact path is not allowlisted")
    if adapter.get("sha256") != sha256_file(SCRIPT_PATH):
        raise AdapterError("adapter source digest does not match the registry")
    semantics_record = adapter.get("semantics")
    semantics_path = ADAPTER_DIR / "postgres_phase_a_semantics.py"
    if (
        not isinstance(semantics_record, dict)
        or semantics_record.get("module") != SEMANTICS_MODULE_ID
        or semantics_record.get("path") != "workload-adapters/postgres_phase_a_semantics.py"
    ):
        raise AdapterError("adapter semantics path is not allowlisted")
    if semantics_record.get("sha256") != sha256_file(semantics_path):
        raise AdapterError("adapter semantics digest does not match the registry")
    fixture_record = adapter.get("fixture")
    if not isinstance(fixture_record, dict) or fixture_record.get("path") != "workload-adapters/fixtures/postgres-phase-a-v1.json":
        raise AdapterError("adapter fixture path is not allowlisted")
    fixture_path = PILOT_DIR / fixture_record["path"]
    if fixture_record.get("sha256") != sha256_file(fixture_path):
        raise AdapterError("adapter fixture digest does not match the registry")
    oracle_record = adapter.get("oracle")
    if not isinstance(oracle_record, dict) or oracle_record.get("path") != "workload-adapters/oracles/postgres-phase-a-v1.json":
        raise AdapterError("adapter oracle identity is not allowlisted")
    if not SHA256.fullmatch(str(oracle_record.get("sha256", ""))):
        raise AdapterError("adapter oracle digest is invalid")
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
        command_kind: str,
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
                timeout_output(error.stderr)
                + f"\nfixed {command_kind} command timed out after {timeout_seconds}s: {operation}\n",
            )
        if record:
            self.command_index += 1
            prefix = self.output_dir / "commands" / f"{self.command_index:03d}"
            prefix.parent.mkdir(parents=True, exist_ok=True)
            (prefix.with_suffix(".stdout.log")).write_text(process.stdout, encoding="utf-8")
            (prefix.with_suffix(".stderr.log")).write_text(process.stderr, encoding="utf-8")
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
            raise AdapterError(
                f"fixed {command_kind} command timed out after {timeout_seconds}s: {operation}"
            )
        if check and process.returncode != 0:
            raise AdapterError(f"fixed {command_kind} command failed ({process.returncode}): {operation}")
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
        command = [
            "docker", "compose", "-f", str(COMPOSE_PATH),
            "--project-name", self.project, "--profile", "postgres", *arguments,
        ]
        return self._run_fixed_command(
            command,
            command[:3] + ["<fixed-compose>"] + command[4:],
            " ".join(arguments),
            command_kind="Compose",
            input_text=input_text,
            check=check,
            record=record,
            timeout_seconds=timeout_seconds,
        )

    def container_id(self, service: str) -> str:
        container_id = self.command("ps", "-q", service).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise AdapterError(f"fixed Compose service has no valid container id: {service}")
        return container_id

    def wait_container(self, container_id: str, service: str) -> int:
        process = self._run_fixed_command(
            ["docker", "wait", container_id],
            ["docker", "wait", f"<{service}-container>"],
            f"wait {service}",
            command_kind="Docker",
        )
        try:
            exit_code = int(process.stdout.strip())
        except ValueError as error:
            raise AdapterError(f"docker wait returned an invalid exit code for {service}") from error
        return exit_code

    def up(self, *services: str) -> None:
        self.command("config", "--quiet")
        self.command("up", "-d", "--wait", "--pull", "never", *(services or ("runner", "postgres")))

    def psql(self, sql: str, *, application_name: str = "phase-a-adapter") -> str:
        process = self.command(
            "exec", "-T", "-e", f"PGAPPNAME={application_name}",
            "postgres", "psql", "-X", "-U", "pilot", "-d", "pilot",
            "-v", "ON_ERROR_STOP=1", "-At", "-c", sql,
        )
        return process.stdout.strip()


def initialize_schema(project: ComposeProject, job_id: str, tier: str, facts: dict[str, Any]) -> None:
    payload = sql_literal(json.dumps(facts, sort_keys=True, separators=(",", ":")))
    project.psql(
        "CREATE TABLE qualification_facts ("
        "seq bigserial PRIMARY KEY, job_id text NOT NULL, tier text NOT NULL, "
        "event_version integer NOT NULL, payload jsonb NOT NULL, evidence_ref text NOT NULL, "
        "created_at timestamptz NOT NULL DEFAULT clock_timestamp());"
        "CREATE TABLE qualification_receipts ("
        "receipt_id bigserial PRIMARY KEY, job_id text NOT NULL, tier text NOT NULL, "
        "state text NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp());"
        f"INSERT INTO qualification_facts(job_id,tier,event_version,payload,evidence_ref) VALUES ("
        f"{sql_literal(job_id)},{sql_literal(tier)},1,{payload}::jsonb,'execution-input-v2');"
    )


def exercise_tier(project: ComposeProject, job_id: str, tier: str, facts: dict[str, Any]) -> dict[str, Any]:
    initialize_schema(project, job_id, tier, facts)
    if tier == "normal":
        observed = project.psql("SELECT count(*) FROM qualification_facts;")
        return {"operation": "current-fact-query", "observed_rows": int(observed)}
    if tier == "concurrent":
        sql_template = (
            "INSERT INTO qualification_facts(job_id,tier,event_version,payload,evidence_ref) VALUES ("
            f"{sql_literal(job_id)},{sql_literal(tier)},2,'{{\"writer\":\"%s\"}}'::jsonb,%s);"
        )
        def write_concurrently(writer: str) -> subprocess.CompletedProcess[str]:
            return project.command(
                "exec", "-T", "postgres", "psql", "-X", "-U", "pilot", "-d", "pilot",
                "-v", "ON_ERROR_STOP=1", "-c",
                sql_template % (writer, sql_literal(f"concurrent-{writer}")),
                record=False,
            )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write_concurrently, ("agent-a", "agent-b")))
        for index, result in enumerate(results, start=1):
            if result.returncode != 0:
                raise AdapterError(f"concurrent writer {index} failed")
            (project.output_dir / f"concurrent-writer-{index}.stdout.log").write_text(result.stdout, encoding="utf-8")
            (project.output_dir / f"concurrent-writer-{index}.stderr.log").write_text(result.stderr, encoding="utf-8")
        observed = project.psql("SELECT count(*) FROM qualification_facts;")
        return {"operation": "two-agent-concurrent-write", "observed_rows": int(observed), "writers": 2}
    if tier == "crash-recovery":
        container_id = project.container_id("postgres")
        project.command("kill", "-s", "SIGKILL", "postgres")
        exit_code = project.wait_container(container_id, "postgres")
        if exit_code != 137:
            raise AdapterError(f"crash-recovery postgres exit code is not SIGKILL: {exit_code}")
        project.up("postgres")
        observed = project.psql("SELECT count(*) FROM qualification_facts;")
        return {"operation": "sigkill-restart-query", "observed_rows": int(observed)}
    if tier == "whole-root-restore":
        dump = project.command(
            "exec", "-T", "postgres", "pg_dump", "-U", "pilot", "-d", "pilot",
            "--no-owner", "--no-privileges",
        ).stdout
        backup_path = project.output_dir / "whole-root-backup.sql"
        backup_path.write_text(dump, encoding="utf-8")
        project.command("down", "--volumes", "--remove-orphans")
        project.up()
        project.command(
            "exec", "-T", "postgres", "psql", "-X", "-U", "pilot", "-d", "pilot",
            "-v", "ON_ERROR_STOP=1", input_text=dump,
        )
        observed = project.psql("SELECT count(*) FROM qualification_facts;")
        return {
            "operation": "whole-project-volume-recreate-and-restore",
            "observed_rows": int(observed),
            "backup_sha256": sha256_file(backup_path),
        }
    if tier == "historical-query":
        project.psql(
            "INSERT INTO qualification_facts(job_id,tier,event_version,payload,evidence_ref) "
            f"VALUES ({sql_literal(job_id)},{sql_literal(tier)},2,'{{\"revision\":2}}'::jsonb,'history-v2');"
        )
        versions = project.psql("SELECT string_agg(event_version::text, ',' ORDER BY event_version) FROM qualification_facts;")
        return {"operation": "ordered-history-query", "observed_versions": versions}
    if tier == "schema-evolution":
        project.psql(
            "ALTER TABLE qualification_facts ADD COLUMN schema_version integer NOT NULL DEFAULT 1;"
            "UPDATE qualification_facts SET schema_version=2;"
        )
        observed = project.psql(
            "SELECT schema_version FROM qualification_facts ORDER BY seq LIMIT 1;"
        )
        return {"operation": "additive-schema-migration", "observed_schema_version": int(observed)}
    if tier == "new-agent-takeover":
        project.psql(
            "INSERT INTO qualification_receipts(job_id,tier,state,payload) VALUES ("
            f"{sql_literal(job_id)},{sql_literal(tier)},'handoff-ready','{{\"owner\":\"agent-a\"}}'::jsonb);"
        )
        observed = project.psql(
            "SELECT state FROM qualification_receipts ORDER BY receipt_id DESC LIMIT 1;",
            application_name="new-agent-takeover",
        )
        return {"operation": "fresh-session-handoff-query", "observed_state": observed}
    raise AdapterError(f"unsupported tier: {tier}")


def observe_job_facts(project: ComposeProject, job_id: str, tier: str, binding_sha256: str) -> dict[str, Any]:
    encoded = project.psql(
        "SELECT payload::text FROM qualification_facts "
        f"WHERE job_id={sql_literal(job_id)} AND tier={sql_literal(tier)} "
        "AND event_version=1 AND evidence_ref='execution-input-v2' "
        "ORDER BY seq LIMIT 1;"
    )
    if not encoded:
        raise AdapterError("observed job facts query returned no execution input")
    try:
        facts = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise AdapterError(f"observed job facts are not JSON: {error}") from error
    if not isinstance(facts, dict):
        raise AdapterError("observed job facts must be an object")
    return {
        "schema": OBSERVED_FACTS_SCHEMA,
        "job_id": job_id,
        "tier": tier,
        "binding_sha256": binding_sha256,
        "query_id": "execution-input-after-tier-event-v1",
        "facts": facts,
        "facts_sha256": sha256_json(facts),
    }


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
    if args.tier not in TIERS:
        raise AdapterError(f"unsupported tier: {args.tier}")
    if args.job_id not in JOB_RECEIPTS:
        raise AdapterError(f"unsupported job: {args.job_id}")
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

    project = ComposeProject(args.project, output_dir)
    project.up()
    wait_for_cgroup_sampler()
    tier_evidence = exercise_tier(
        project, args.job_id, args.tier, fixture["jobs"][args.job_id]["facts"]
    )
    tier_evidence.update(
        {
            "schema": "urn:kungfu-systems:build-images:postgres-tier-evidence:v1",
            "job_id": args.job_id,
            "tier": args.tier,
            "binding_sha256": binding["sha256"],
        }
    )
    observed_facts = observe_job_facts(project, args.job_id, args.tier, binding["sha256"])
    observed_facts_path = output_dir / "observed-facts.json"
    write_json(observed_facts_path, observed_facts)
    tier_evidence["observed_facts_sha256"] = observed_facts["facts_sha256"]
    write_json(output_dir / "tier-evidence.json", tier_evidence)
    receipt_name = JOB_RECEIPTS[args.job_id]
    write_json(
        output_dir / receipt_name,
        job_receipt(
            args.job_id,
            fixture["jobs"][args.job_id]["fixture_id"],
            observed_facts["facts"],
            args.tier,
            binding["sha256"],
            tier_evidence,
            sha256_file(observed_facts_path),
            sha256_file(output_dir / "tier-evidence.json"),
        ),
    )
    adapter_receipt = {
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
    }
    write_json(output_dir / "adapter-receipt.json", adapter_receipt)
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
        print(f"postgres phase-a adapter error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
