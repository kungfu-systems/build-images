#!/usr/bin/env python3
"""Run the fixed Aeron containerized Phase A workload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any

from aeron_phase_a_semantics import (
    DECISION_LOGIC_VERSION,
    JOB_RECEIPT_SCHEMA,
    JOB_RECEIPTS,
    OBSERVED_FACTS_SCHEMA,
    SEMANTICS_MODULE_ID,
    SemanticError,
    derive_job_verdict,
    validate_execution_fixture,
)

ADAPTER_ID = "aeron-phase-a-v1"
ADAPTER_VERSION = "1.0.0"
ENTRYPOINT = "aeron-phase-a-v1"
ACTION = "workload-adapter"
SCRIPT_PATH = pathlib.Path(__file__).resolve()
ADAPTER_DIR = SCRIPT_PATH.parent
PILOT_DIR = ADAPTER_DIR.parent
ARTIFACT_ROOT = PILOT_DIR / ".artifacts"
COMPOSE_PATH = PILOT_DIR / "compose.yaml"
REGISTRY_PATH = ADAPTER_DIR / "registry.json"
HARNESS = "/opt/aeron-native-kit/bin/aeron-native-harness"
PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
OBSERVED_QUERY_ID = "execution-input-after-tier-event-v1"
TIERS = (
    "normal",
    "concurrent",
    "crash-recovery",
    "whole-root-restore",
    "historical-query",
    "schema-evolution",
    "new-agent-takeover",
)


class AdapterError(ValueError):
    """A frozen adapter input or lifecycle proof is invalid."""


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
        "profile": "aeron",
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
        raise AdapterError("adapter version or entrypoint does not match the implementation")
    if adapter.get("artifact") != "workload-adapters/aeron_phase_a_v1.py":
        raise AdapterError("adapter artifact path is not allowlisted")
    if adapter.get("sha256") != sha256_file(SCRIPT_PATH):
        raise AdapterError("adapter source digest does not match the registry")
    semantics = adapter.get("semantics")
    semantics_path = ADAPTER_DIR / "aeron_phase_a_semantics.py"
    if (
        not isinstance(semantics, dict)
        or semantics.get("module") != SEMANTICS_MODULE_ID
        or semantics.get("path") != "workload-adapters/aeron_phase_a_semantics.py"
        or semantics.get("sha256") != sha256_file(semantics_path)
    ):
        raise AdapterError("adapter semantics identity does not match the registry")
    fixture = adapter.get("fixture")
    if not isinstance(fixture, dict) or fixture.get("path") != "workload-adapters/fixtures/aeron-phase-a-v1.json":
        raise AdapterError("adapter fixture path is not allowlisted")
    fixture_path = PILOT_DIR / fixture["path"]
    if fixture.get("sha256") != sha256_file(fixture_path):
        raise AdapterError("adapter fixture digest does not match the registry")
    oracle = adapter.get("oracle")
    if not isinstance(oracle, dict) or oracle.get("path") != "workload-adapters/oracles/aeron-phase-a-v1.json":
        raise AdapterError("adapter oracle path is not allowlisted")
    oracle_path = PILOT_DIR / oracle["path"]
    if oracle.get("sha256") != sha256_file(oracle_path):
        raise AdapterError("adapter oracle digest does not match the registry")
    allowed = {
        (mapping.get("job_id"), allowed_tier)
        for mapping in adapter.get("mappings", [])
        if isinstance(mapping, dict)
        for allowed_tier in mapping.get("tiers", [])
    }
    if (job_id, tier) not in allowed:
        raise AdapterError(f"job/tier is not mapped by {ADAPTER_ID}: {job_id}/{tier}")
    execution_fixture = load_json(fixture_path)
    try:
        validate_execution_fixture(execution_fixture)
    except SemanticError as error:
        raise AdapterError(str(error)) from error
    return adapter, fixture_path, execution_fixture, sha256_file(REGISTRY_PATH)


class ComposeProject:
    def __init__(self, project: str, output_dir: pathlib.Path) -> None:
        self.project = project
        self.output_dir = output_dir
        self.command_index = 0

    def _run(
        self,
        command: list[str],
        operation: str,
        *,
        check: bool = True,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        timed_out = False
        try:
            process = subprocess.run(
                command,
                cwd=PILOT_DIR,
                env={**os.environ, "COMPARATOR_PROJECT_NAME": self.project},
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
            stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
            process = subprocess.CompletedProcess(command, 124, stdout, stderr + "\ncommand timed out\n")
        self.command_index += 1
        prefix = self.output_dir / "commands" / f"{self.command_index:03d}"
        prefix.parent.mkdir(parents=True, exist_ok=True)
        prefix.with_suffix(".stdout.log").write_text(process.stdout, encoding="utf-8")
        prefix.with_suffix(".stderr.log").write_text(process.stderr, encoding="utf-8")
        write_json(prefix.with_suffix(".json"), {
            "argv": command,
            "operation": operation,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "timeout_seconds": timeout,
        })
        if check and process.returncode != 0:
            raise AdapterError(f"command failed ({process.returncode}): {operation}")
        return process

    def compose(self, *arguments: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        command = [
            "docker", "compose", "-f", str(COMPOSE_PATH),
            "--project-name", self.project, "--profile", "aeron", *arguments,
        ]
        return self._run(command, "compose " + " ".join(arguments), check=check, timeout=timeout)

    def up(self) -> dict[str, Any]:
        self.compose("config", "--quiet")
        self.compose("up", "-d", "--wait", "--no-build", "--pull", "never", "aeron", timeout=180)
        return self.wait_health()

    def wait_health(self) -> dict[str, Any]:
        deadline = time.monotonic() + 30
        consecutive = 0
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            process = self.compose("exec", "-T", "aeron", HARNESS, "health", "--root", "/var/lib/aeron", check=False, timeout=10)
            try:
                latest = json.loads(process.stdout.splitlines()[-1]) if process.stdout.strip() else {}
            except json.JSONDecodeError:
                latest = {}
            passed = process.returncode == 0 and latest.get("status") == "live"
            consecutive = consecutive + 1 if passed else 0
            if consecutive == 2:
                return latest
            time.sleep(0.5)
        raise AdapterError("Aeron did not sustain two consecutive live-health probes")

    def container_id(self) -> str:
        value = self.compose("ps", "-q", "--all", "aeron").stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", value):
            raise AdapterError("Aeron service container id is invalid")
        return value

    def harness(self, suffix: str, *arguments: str, check: bool = True, timeout: int = 120) -> dict[str, Any]:
        process = self.compose("exec", "-T", "aeron", HARNESS, *arguments, check=check, timeout=timeout)
        if not check:
            return {"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr}
        lines = [line for line in process.stdout.splitlines() if line.strip()]
        if not lines:
            raise AdapterError(f"Aeron harness emitted no JSON: {suffix}")
        try:
            value = json.loads(lines[-1])
        except json.JSONDecodeError as error:
            raise AdapterError(f"Aeron harness emitted invalid JSON: {suffix}") from error
        if not isinstance(value, dict):
            raise AdapterError(f"Aeron harness result is not an object: {suffix}")
        write_json(self.output_dir / f"{suffix}.json", value)
        return value

    def backup_and_recreate(self) -> str:
        backup = self.output_dir / "whole-root-backup"
        backup.mkdir()
        container_id = self.container_id()
        self._run(["docker", "cp", f"{container_id}:/var/lib/aeron/.", str(backup)], "copy whole Aeron root")
        records = [
            {"path": path.relative_to(backup).as_posix(), "sha256": sha256_file(path)}
            for path in sorted(backup.rglob("*")) if path.is_file()
        ]
        if not records:
            raise AdapterError("whole-root backup is empty")
        write_json(self.output_dir / "whole-root-backup.inventory.json", {"files": records})
        self.compose("stop", "aeron")
        self.compose("rm", "-f", "aeron")
        self._run(["docker", "volume", "rm", f"{self.project}_aeron-data"], "remove scoped Aeron volume")
        self.compose("create", "--no-build", "--pull", "never", "aeron")
        restored_container = self.container_id()
        self._run(["docker", "cp", f"{backup}/.", f"{restored_container}:/var/lib/aeron"], "restore whole Aeron root")
        time.sleep(11)
        self.compose("start", "aeron")
        self.wait_health()
        return sha256_json(records)


def record(project: ComposeProject, marker: str, suffix: str, *, payload: int, receipt: str) -> dict[str, Any]:
    value = project.harness(
        f"record-{suffix}", "record", "--root", "/var/lib/aeron", "--count", "5",
        "--payload", str(payload), "--receipt", receipt, "--marker", marker,
    )
    if value.get("observed") != 5 or value.get("marker") != marker:
        raise AdapterError(f"Aeron record oracle failed: {suffix}")
    return value


def replay(project: ComposeProject, marker: str, suffix: str, recorded: dict[str, Any]) -> dict[str, Any]:
    value = project.harness(
        f"replay-{suffix}", "replay", "--root", "/var/lib/aeron", "--count", "5",
        "--recording-id", str(recorded["recording_id"]), "--length", str(recorded["final_position"]),
        "--marker", marker,
    )
    if value.get("observed") != 5 or value.get("marker") != marker:
        raise AdapterError(f"Aeron replay oracle failed: {suffix}")
    return value


def exercise_tier(project: ComposeProject, tier: str, marker: str) -> dict[str, Any]:
    health = project.up()
    records: list[dict[str, Any]] = []
    replays: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "operation": "bounded-record-replay",
        "live_health": health.get("status") == "live",
        "marker_bound": True,
    }
    receipt = {
        "normal": "visible",
        "schema-evolution": "durable_sync",
    }.get(tier, "durable_group")
    first = record(project, marker, "v1", payload=128, receipt=receipt)
    records.append(first)

    if tier == "crash-recovery":
        project.compose("kill", "-s", "SIGKILL", "aeron")
        stale = project.compose(
            "run", "--rm", "--no-deps", "aeron", HARNESS,
            "health", "--root", "/var/lib/aeron", check=False, timeout=30,
        )
        evidence.update({"stale_health_rejected": stale.returncode != 0, "expiry_wait_seconds": 11})
        if stale.returncode == 0:
            raise AdapterError("stale Aeron files passed live-health verification")
        time.sleep(11)
        project.compose("start", "aeron")
        project.wait_health()
    elif tier == "whole-root-restore":
        backup_sha = project.backup_and_recreate()
        evidence.update({"root_restored": True, "backup_sha256": backup_sha})

    replays.append(replay(project, marker, "v1", first))
    if tier == "concurrent":
        second = record(project, marker, "client-2", payload=128, receipt="durable_group")
        records.append(second)
        replays.append(replay(project, marker, "client-2", second))
        evidence.update({"operation": "two-client-isolation", "clients": 2})
    elif tier == "historical-query":
        evidence["operation"] = "ordered-history-replay"
    elif tier == "schema-evolution":
        second = record(project, marker, "v2", payload=256, receipt="durable_sync")
        records.append(second)
        replays.append(replay(project, marker, "v2", second))
        evidence.update({"operation": "additive-frame-envelope", "envelope_versions": [1, 2]})
    elif tier == "new-agent-takeover":
        evidence.update({"operation": "fresh-client-replay", "fresh_client": True})
    elif tier == "crash-recovery":
        evidence["operation"] = "sigkill-live-restart-replay"
    elif tier == "whole-root-restore":
        evidence["operation"] = "whole-root-recreate-restore-replay"
    evidence.update({"records": records, "replays": replays})
    return evidence


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


def observed_facts_document(
    job_id: str,
    tier: str,
    binding_sha256: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": OBSERVED_FACTS_SCHEMA,
        "job_id": job_id,
        "tier": tier,
        "binding_sha256": binding_sha256,
        "query_id": OBSERVED_QUERY_ID,
        "facts": facts,
        "facts_sha256": sha256_json(facts),
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

    adapter, fixture_path, fixture, registry_sha = validate_adapter(args.job_id, args.tier)
    binding = adapter_binding(registry_sha, adapter, args.scenario_id, args.job_id, args.tier, args.step_id)
    if binding["sha256"] != args.expected_binding_sha256:
        raise AdapterError("runner binding does not match the adapter inputs")
    facts = fixture["jobs"][args.job_id]["facts"]
    tier_evidence = exercise_tier(ComposeProject(args.project, output_dir), args.tier, binding["sha256"])
    tier_evidence.update({
        "schema": "urn:kungfu-systems:build-images:aeron-tier-evidence:v1",
        "job_id": args.job_id,
        "tier": args.tier,
        "binding_sha256": binding["sha256"],
    })
    observed_facts = observed_facts_document(
        args.job_id,
        args.tier,
        binding["sha256"],
        facts,
    )
    observed_path = output_dir / "observed-facts.json"
    tier_path = output_dir / "tier-evidence.json"
    write_json(observed_path, observed_facts)
    tier_evidence["observed_facts_sha256"] = observed_facts["facts_sha256"]
    write_json(tier_path, tier_evidence)
    receipt_name = JOB_RECEIPTS[args.job_id]
    write_json(output_dir / receipt_name, job_receipt(
        args.job_id,
        fixture["jobs"][args.job_id]["fixture_id"],
        facts,
        args.tier,
        binding["sha256"],
        tier_evidence,
        sha256_file(observed_path),
        sha256_file(tier_path),
    ))
    write_json(output_dir / "adapter-receipt.json", {
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
    })
    write_json(output_dir / "adapter-inputs.json", {
        "adapter": adapter,
        "registry_sha256": registry_sha,
        "fixture_sha256": sha256_file(fixture_path),
        "binding": binding,
    })
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
            adapter, _, _, registry_sha = validate_adapter(args.job_id, args.tier)
            print(json.dumps({"adapter": adapter, "registry_sha256": registry_sha}, indent=2, sort_keys=True))
        else:
            run(args)
    except (AdapterError, OSError, subprocess.SubprocessError) as error:
        print(f"aeron phase-a adapter error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
