#!/usr/bin/env python3
"""Execute frozen comparator plans and verify self-contained evidence bundles."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import time
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
PILOT_DIR = SCRIPT_DIR.parent
REPO_ROOT = PILOT_DIR.parents[1]
ARTIFACT_ROOT = PILOT_DIR / ".artifacts"
COMPOSE_PATH = PILOT_DIR / "compose.yaml"
LOCK_PATH = PILOT_DIR / "environment.lock.json"
PILOT_SCRIPT = SCRIPT_DIR / "pilot.sh"
QUALIFICATION_RUNNER = pathlib.Path(__file__).resolve()
PLAN_SCHEMA_PATH = PILOT_DIR / "qualification-plan.schema.json"
RUN_SCHEMA_PATH = PILOT_DIR / "qualification-run-manifest.schema.json"
BUNDLE_SCHEMA_PATH = PILOT_DIR / "qualification-bundle-manifest.schema.json"
ADAPTER_REGISTRY_PATH = PILOT_DIR / "workload-adapters" / "registry.json"

PLAN_SCHEMA_ID = "urn:kungfu-systems:build-images:comparator-qualification-plan:v1"
RUN_SCHEMA_ID = "urn:kungfu-systems:build-images:comparator-qualification-run:v1"
BUNDLE_SCHEMA_ID = "urn:kungfu-systems:build-images:comparator-qualification-bundle:v1"
EVIDENCE_CLASS = "containerized-user-outcome-qualification"
WORKLOAD_ACTION = "workload-adapter"
PROFILES = ("aeron", "clickhouse", "postgres", "kungfu")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
ID = re.compile(r"^[A-Za-z0-9._-]+$")
CLAIM_BOUNDARY = (
    "Containerized user-outcome qualification only; native-host performance, "
    "fresh-install cost, final scoring, and winner declarations remain outside this bundle."
)
RUNTIME_ENV_ALLOWLIST = {
    "PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL",
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR",
}

sys.path.insert(0, str(SCRIPT_DIR))
import comparator_pilot  # noqa: E402


class QualificationError(ValueError):
    """A fail-closed qualification contract violation."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise QualificationError(f"JSON root must be an object: {path}")
    return value


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_keys(value: dict[str, Any], required: set[str], optional: set[str], context: str) -> None:
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required - optional)
    if missing:
        raise QualificationError(f"{context} is missing fields: {', '.join(missing)}")
    if extra:
        raise QualificationError(f"{context} has unsupported fields: {', '.join(extra)}")


def require_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise QualificationError(f"{context} must be an identifier")
    return value


def require_sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise QualificationError(f"{context} must be a lowercase SHA-256")
    return value


def safe_relative(value: Any, context: str, *, allow_glob: bool = False) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value:
        raise QualificationError(f"{context} must be a non-empty relative path")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise QualificationError(f"{context} must stay inside the qualification artifact root")
    if not allow_glob and any(character in value for character in "*?[]"):
        raise QualificationError(f"{context} must not contain glob characters")
    return path


def validate_contract_schemas() -> None:
    expected = (
        (PLAN_SCHEMA_PATH, PLAN_SCHEMA_ID),
        (RUN_SCHEMA_PATH, RUN_SCHEMA_ID),
        (BUNDLE_SCHEMA_PATH, BUNDLE_SCHEMA_ID),
    )
    for path, schema_id in expected:
        schema = load_json(path)
        if schema.get("$id") != schema_id:
            raise QualificationError(f"schema has unexpected $id: {path}")
        if schema.get("additionalProperties") is not False:
            raise QualificationError(f"schema must reject unknown top-level fields: {path}")
    bundle_schema = load_json(BUNDLE_SCHEMA_PATH)
    properties = bundle_schema.get("properties", {})
    for field in ("native_performance_authority", "fresh_install_cost_authority", "final_scoring_authority"):
        if properties.get(field, {}).get("const") is not False:
            raise QualificationError(f"bundle schema must force {field}=false")


def validate_digest_identity(value: Any, context: str, *, versioned: bool) -> dict[str, str]:
    if not isinstance(value, dict):
        raise QualificationError(f"{context} must be an object")
    required = {"id", "sha256"} | ({"version"} if versioned else set())
    require_keys(value, required, set(), context)
    require_id(value["id"], f"{context}.id")
    require_sha256(value["sha256"], f"{context}.sha256")
    if versioned and (not isinstance(value["version"], str) or not value["version"]):
        raise QualificationError(f"{context}.version must be non-empty")
    return value


def validate_adapter_registry(registry_path: pathlib.Path, root: pathlib.Path) -> dict[str, dict[str, Any]]:
    registry = load_json(registry_path)
    require_keys(registry, {"schema", "schema_version", "adapters"}, set(), "workload adapter registry")
    if registry["schema"] != "urn:kungfu-systems:build-images:workload-adapter-registry:v1":
        raise QualificationError("workload adapter registry schema is unsupported")
    if registry["schema_version"] != 1 or not isinstance(registry["adapters"], dict) or not registry["adapters"]:
        raise QualificationError("workload adapter registry is empty or has an unsupported version")
    resolved: dict[str, dict[str, Any]] = {}
    for adapter_id, adapter in registry["adapters"].items():
        require_id(adapter_id, "workload adapter id")
        if not isinstance(adapter, dict):
            raise QualificationError(f"workload adapter record must be an object: {adapter_id}")
        require_keys(
            adapter,
            {"version", "artifact", "sha256", "entrypoint", "profiles", "fixture", "mappings"},
            set(),
            f"workload adapter {adapter_id}",
        )
        if not isinstance(adapter["version"], str) or not adapter["version"]:
            raise QualificationError(f"workload adapter version is invalid: {adapter_id}")
        require_id(adapter["entrypoint"], f"workload adapter {adapter_id}.entrypoint")
        artifact_relative = safe_relative(adapter["artifact"], f"workload adapter {adapter_id}.artifact")
        if not artifact_relative.as_posix().startswith("workload-adapters/"):
            raise QualificationError(f"workload adapter artifact is outside the allowlisted directory: {adapter_id}")
        artifact_path = root / pathlib.Path(*artifact_relative.parts)
        require_sha256(adapter["sha256"], f"workload adapter {adapter_id}.sha256")
        if not artifact_path.is_file() or sha256_file(artifact_path) != adapter["sha256"]:
            raise QualificationError(f"workload adapter artifact digest mismatch: {adapter_id}")
        fixture = adapter["fixture"]
        if not isinstance(fixture, dict):
            raise QualificationError(f"workload adapter fixture is invalid: {adapter_id}")
        require_keys(fixture, {"path", "sha256"}, set(), f"workload adapter {adapter_id}.fixture")
        fixture_relative = safe_relative(fixture["path"], f"workload adapter {adapter_id}.fixture.path")
        if not fixture_relative.as_posix().startswith("workload-adapters/fixtures/"):
            raise QualificationError(f"workload adapter fixture is outside the allowlisted directory: {adapter_id}")
        fixture_path = root / pathlib.Path(*fixture_relative.parts)
        require_sha256(fixture["sha256"], f"workload adapter {adapter_id}.fixture.sha256")
        if not fixture_path.is_file() or sha256_file(fixture_path) != fixture["sha256"]:
            raise QualificationError(f"workload adapter fixture digest mismatch: {adapter_id}")
        profiles = adapter["profiles"]
        if not isinstance(profiles, list) or not profiles or any(profile not in PROFILES for profile in profiles):
            raise QualificationError(f"workload adapter profiles are invalid: {adapter_id}")
        if len(profiles) != len(set(profiles)):
            raise QualificationError(f"workload adapter profiles contain duplicates: {adapter_id}")
        mappings = adapter["mappings"]
        if not isinstance(mappings, list) or not mappings:
            raise QualificationError(f"workload adapter mappings are empty: {adapter_id}")
        seen_mappings: set[tuple[str, str]] = set()
        for index, mapping in enumerate(mappings):
            if not isinstance(mapping, dict):
                raise QualificationError(f"workload adapter mapping must be an object: {adapter_id}[{index}]")
            require_keys(mapping, {"job_id", "tiers"}, set(), f"workload adapter mapping {adapter_id}[{index}]")
            job_id = require_id(mapping["job_id"], f"workload adapter mapping {adapter_id}[{index}].job_id")
            tiers = mapping["tiers"]
            if not isinstance(tiers, list) or not tiers:
                raise QualificationError(f"workload adapter tiers are empty: {adapter_id}/{job_id}")
            for tier in tiers:
                tier_id = require_id(tier, f"workload adapter tier {adapter_id}/{job_id}")
                pair = (job_id, tier_id)
                if pair in seen_mappings:
                    raise QualificationError(f"duplicate workload adapter mapping: {adapter_id}/{job_id}/{tier_id}")
                seen_mappings.add(pair)
        resolved[adapter_id] = copy.deepcopy(adapter)
    return resolved


def workload_binding(
    profile: str,
    registry_sha256: str,
    adapter: dict[str, Any],
    scenario: dict[str, Any],
    step: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema": "urn:kungfu-systems:build-images:workload-binding:v1",
        "profile": profile,
        "scenario_id": scenario["id"],
        "job_id": scenario["job_id"],
        "tier": scenario["tier"],
        "step_id": step["id"],
        "action": step["action"],
        "adapter_id": adapter["id"],
        "adapter_version": adapter["version"],
        "adapter_entrypoint": adapter["entrypoint"],
        "adapter_sha256": adapter["sha256"],
        "fixture_sha256": adapter["fixture"]["sha256"],
        "registry_sha256": registry_sha256,
    }
    return {"payload": payload, "sha256": sha256_json(payload)}


def parse_compose_limits() -> dict[str, dict[str, str]]:
    limits: dict[str, dict[str, str]] = {}
    service = ""
    in_services = False
    for line in COMPOSE_PATH.read_text(encoding="utf-8").splitlines():
        if line == "services:":
            in_services = True
            continue
        if in_services and line and not line.startswith(" "):
            break
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if in_services and match:
            service = match.group(1)
            limits.setdefault(service, {})
            continue
        setting = re.match(r"^    (mem_limit|cpus):\s*(.+?)\s*$", line)
        if in_services and service and setting:
            limits[service][setting.group(1)] = setting.group(2).strip('"')
    return limits


def git_head() -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not GIT_SHA.fullmatch(result):
        raise QualificationError("build-images HEAD is not an exact Git SHA")
    return result


def require_clean_source() -> None:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=no"],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise QualificationError("production qualification requires a clean tracked build-images checkout")


def validate_plan_document(plan: dict[str, Any]) -> None:
    required = {
        "plan_schema",
        "schema_version",
        "test_only",
        "evidence_class",
        "charter",
        "fixture_set",
        "profile",
        "configuration_slot",
        "environment",
        "repetitions",
        "scenarios",
        "artifact_retention",
        "claim_boundary",
    }
    require_keys(plan, required, {"subject", "adapter_registry", "workload_adapter"}, "qualification plan")
    if plan["plan_schema"] != PLAN_SCHEMA_ID or plan["schema_version"] != 1:
        raise QualificationError("qualification plan schema/version is unsupported")
    if not isinstance(plan["test_only"], bool):
        raise QualificationError("qualification plan test_only must be boolean")
    if plan["evidence_class"] != EVIDENCE_CLASS:
        raise QualificationError(f"qualification plan evidence_class must be {EVIDENCE_CLASS}")
    validate_digest_identity(plan["charter"], "charter", versioned=True)
    validate_digest_identity(plan["fixture_set"], "fixture_set", versioned=False)

    profile = plan["profile"]
    if profile not in PROFILES:
        raise QualificationError(f"unknown profile: {profile}")
    if plan["configuration_slot"] not in ("realistic-default", "expert-tuned"):
        raise QualificationError("unknown configuration_slot")

    adapter_mappings: set[tuple[str, str]] = set()
    adapter_id = ""
    if plan["test_only"]:
        if "adapter_registry" in plan or "workload_adapter" in plan:
            raise QualificationError("test-only profile-smoke plans must not declare production workload adapters")
    else:
        registry = plan.get("adapter_registry")
        adapter = plan.get("workload_adapter")
        if not isinstance(registry, dict) or not isinstance(adapter, dict):
            raise QualificationError("production qualification requires an adapter registry and workload adapter")
        require_keys(registry, {"path", "sha256"}, set(), "adapter_registry")
        if registry["path"] != "workload-adapters/registry.json":
            raise QualificationError("replacement workload adapter registries are forbidden")
        require_sha256(registry["sha256"], "adapter_registry.sha256")
        require_keys(
            adapter,
            {"id", "version", "artifact", "sha256", "entrypoint", "profiles", "fixture", "mappings"},
            set(),
            "workload_adapter",
        )
        adapter_id = require_id(adapter["id"], "workload_adapter.id")
        if not isinstance(adapter["version"], str) or not adapter["version"]:
            raise QualificationError("workload_adapter.version must be non-empty")
        require_id(adapter["entrypoint"], "workload_adapter.entrypoint")
        safe_relative(adapter["artifact"], "workload_adapter.artifact")
        require_sha256(adapter["sha256"], "workload_adapter.sha256")
        fixture = adapter["fixture"]
        if not isinstance(fixture, dict):
            raise QualificationError("workload_adapter.fixture must be an object")
        require_keys(fixture, {"path", "sha256"}, set(), "workload_adapter.fixture")
        safe_relative(fixture["path"], "workload_adapter.fixture.path")
        require_sha256(fixture["sha256"], "workload_adapter.fixture.sha256")
        if adapter["profiles"] != [profile]:
            raise QualificationError("workload adapter must bind exactly the selected profile")
        mappings = adapter["mappings"]
        if not isinstance(mappings, list) or not mappings:
            raise QualificationError("workload adapter mappings must be non-empty")
        for mapping_index, mapping in enumerate(mappings):
            if not isinstance(mapping, dict):
                raise QualificationError(f"workload adapter mapping {mapping_index} must be an object")
            require_keys(mapping, {"job_id", "tiers"}, set(), f"workload adapter mapping {mapping_index}")
            mapped_job = require_id(mapping["job_id"], f"workload adapter mapping {mapping_index}.job_id")
            if not isinstance(mapping["tiers"], list) or not mapping["tiers"]:
                raise QualificationError(f"workload adapter mapping {mapping_index}.tiers must be non-empty")
            for tier in mapping["tiers"]:
                pair = (mapped_job, require_id(tier, f"workload adapter mapping {mapping_index}.tier"))
                if pair in adapter_mappings:
                    raise QualificationError(f"duplicate workload adapter mapping: {pair[0]}/{pair[1]}")
                adapter_mappings.add(pair)

    environment = plan["environment"]
    if not isinstance(environment, dict):
        raise QualificationError("environment must be an object")
    require_keys(
        environment,
        {"compose_path", "compose_sha256", "environment_lock_path", "environment_lock_sha256", "runner_image"},
        set(),
        "environment",
    )
    if environment["compose_path"] != "compose.yaml":
        raise QualificationError("replacement Compose files are forbidden")
    if environment["environment_lock_path"] != "environment.lock.json":
        raise QualificationError("replacement environment locks are forbidden")
    require_sha256(environment["compose_sha256"], "environment.compose_sha256")
    require_sha256(environment["environment_lock_sha256"], "environment.environment_lock_sha256")
    if not isinstance(environment["runner_image"], str) or not re.search(r"@sha256:[0-9a-f]{64}$", environment["runner_image"]):
        raise QualificationError("runner_image must use an immutable digest")
    if ":latest" in environment["runner_image"]:
        raise QualificationError("runner_image must not use :latest")

    repetitions = plan["repetitions"]
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or not 3 <= repetitions <= 20:
        raise QualificationError("repetitions must be an integer between 3 and 20")

    scenarios = plan["scenarios"]
    if not isinstance(scenarios, list) or not scenarios:
        raise QualificationError("scenarios must be a non-empty array")
    scenario_ids: set[str] = set()
    for scenario_index, scenario in enumerate(scenarios):
        context = f"scenarios[{scenario_index}]"
        if not isinstance(scenario, dict):
            raise QualificationError(f"{context} must be an object")
        scenario_required = {"id", "job_id", "steps"} | ({"tier"} if not plan["test_only"] else set())
        require_keys(scenario, scenario_required, set(), context)
        scenario_id = require_id(scenario["id"], f"{context}.id")
        job_id = require_id(scenario["job_id"], f"{context}.job_id")
        tier = ""
        if not plan["test_only"]:
            tier = require_id(scenario["tier"], f"{context}.tier")
            if (job_id, tier) not in adapter_mappings:
                raise QualificationError(f"scenario job/tier is not mapped by the workload adapter: {job_id}/{tier}")
        if scenario_id in scenario_ids:
            raise QualificationError(f"duplicate scenario id: {scenario_id}")
        scenario_ids.add(scenario_id)
        steps = scenario["steps"]
        if not isinstance(steps, list) or not steps:
            raise QualificationError(f"{context}.steps must be non-empty")
        step_ids: set[str] = set()
        for step_index, step in enumerate(steps):
            step_context = f"{context}.steps[{step_index}]"
            if not isinstance(step, dict):
                raise QualificationError(f"{step_context} must be an object")
            step_required = {"id", "action", "timeout_seconds", "oracles"}
            step_optional = {"adapter_id"}
            require_keys(step, step_required, step_optional, step_context)
            step_id = require_id(step["id"], f"{step_context}.id")
            if step_id in step_ids:
                raise QualificationError(f"duplicate step id in {scenario_id}: {step_id}")
            step_ids.add(step_id)
            if plan["test_only"]:
                if step["action"] != "profile-smoke" or "adapter_id" in step:
                    raise QualificationError(f"{step_context} must remain a test-only profile-smoke step")
            elif step["action"] != WORKLOAD_ACTION or step.get("adapter_id") != adapter_id:
                raise QualificationError(f"{step_context} must use the declared workload adapter")
            timeout = step["timeout_seconds"]
            if not isinstance(timeout, int) or isinstance(timeout, bool) or not 30 <= timeout <= 3600:
                raise QualificationError(f"{step_context}.timeout_seconds must be 30..3600")
            oracles = step["oracles"]
            if not isinstance(oracles, list) or len(oracles) < 2:
                raise QualificationError(f"{step_context}.oracles must include exit and artifact checks")
            has_exit = False
            has_artifact = False
            for oracle_index, oracle in enumerate(oracles):
                oracle_context = f"{step_context}.oracles[{oracle_index}]"
                if not isinstance(oracle, dict):
                    raise QualificationError(f"{oracle_context} must be an object")
                require_keys(oracle, {"type", "expected"}, {"path"}, oracle_context)
                oracle_type = oracle["type"]
                if oracle_type == "exit-code":
                    if oracle["expected"] != 0 or "path" in oracle:
                        raise QualificationError(f"{oracle_context} must require exit code 0 without a path")
                    has_exit = True
                elif oracle_type in ("artifact-exists", "artifact-text-contains"):
                    safe_relative(oracle.get("path"), f"{oracle_context}.path")
                    if oracle_type == "artifact-exists" and oracle["expected"] is not True:
                        raise QualificationError(f"{oracle_context} must expect true")
                    if oracle_type == "artifact-text-contains" and (
                        not isinstance(oracle["expected"], str) or not oracle["expected"]
                    ):
                        raise QualificationError(f"{oracle_context} must expect non-empty text")
                    has_artifact = True
                else:
                    raise QualificationError(f"{oracle_context}.type is unsupported")
            if not has_exit or not has_artifact:
                raise QualificationError(f"{step_context} must include exit-code and artifact oracles")

    retention = plan["artifact_retention"]
    if not isinstance(retention, dict):
        raise QualificationError("artifact_retention must be an object")
    require_keys(retention, {"destination", "required_patterns", "retention_days"}, set(), "artifact_retention")
    safe_relative(retention["destination"], "artifact_retention.destination")
    patterns = retention["required_patterns"]
    if not isinstance(patterns, list) or not patterns:
        raise QualificationError("artifact_retention.required_patterns must be non-empty")
    for index, pattern in enumerate(patterns):
        safe_relative(pattern, f"artifact_retention.required_patterns[{index}]", allow_glob=True)
    days = retention["retention_days"]
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 90:
        raise QualificationError("artifact_retention.retention_days must be 1..90")

    boundary = plan["claim_boundary"]
    expected_boundary = {
        "native_performance_authority": False,
        "user_outcome_qualification_authority_requested": True,
        "fresh_install_cost_authority": False,
        "final_scoring_authority": False,
    }
    if boundary != expected_boundary:
        raise QualificationError("claim_boundary must preserve the split authority contract")

    if profile == "kungfu":
        subject = plan.get("subject")
        if not isinstance(subject, dict):
            raise QualificationError("kungfu qualification requires a package subject")
        require_keys(subject, {"artifact_name", "version", "package_sha256", "source_sha", "evidence_url"}, set(), "subject")
        if subject["artifact_name"] != "kungfu-episodes-cli-linux-x64.tar.gz":
            raise QualificationError("subject.artifact_name must match the fixed Kungfu package filename")
        if not isinstance(subject["version"], str) or not subject["version"]:
            raise QualificationError("subject.version must be non-empty")
        require_sha256(subject["package_sha256"], "subject.package_sha256")
        if not isinstance(subject["source_sha"], str) or not GIT_SHA.fullmatch(subject["source_sha"]):
            raise QualificationError("subject.source_sha must be an exact Git SHA")
        if not isinstance(subject["evidence_url"], str) or not subject["evidence_url"].startswith("https://"):
            raise QualificationError("subject.evidence_url must be https")
    elif "subject" in plan:
        raise QualificationError("non-Kungfu profiles resolve their subject only from environment.lock.json")


def resolve_plan(plan_path: pathlib.Path) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_contract_schemas()
    lock = comparator_pilot.validate()
    plan = load_json(plan_path)
    validate_plan_document(plan)
    environment = plan["environment"]
    actual_compose_sha = sha256_file(COMPOSE_PATH)
    actual_lock_sha = sha256_file(LOCK_PATH)
    if environment["compose_sha256"] != actual_compose_sha:
        raise QualificationError("qualification plan Compose digest does not match the fixed compose.yaml")
    if environment["environment_lock_sha256"] != actual_lock_sha:
        raise QualificationError("qualification plan environment lock digest does not match environment.lock.json")
    if environment["runner_image"] != lock.get("runner", {}).get("image"):
        raise QualificationError("qualification plan runner image does not match the environment lock")
    if ":latest" in COMPOSE_PATH.read_text(encoding="utf-8"):
        raise QualificationError("fixed Compose environment contains :latest")

    workload_adapter: dict[str, Any] | None = None
    adapter_registry_sha256 = ""
    if not plan["test_only"]:
        adapter_registry_sha256 = sha256_file(ADAPTER_REGISTRY_PATH)
        if plan["adapter_registry"]["sha256"] != adapter_registry_sha256:
            raise QualificationError("qualification plan adapter registry digest does not match the fixed registry")
        registered = validate_adapter_registry(ADAPTER_REGISTRY_PATH, PILOT_DIR)
        adapter_id = plan["workload_adapter"]["id"]
        registered_adapter = registered.get(adapter_id)
        if registered_adapter is None:
            raise QualificationError(f"qualification plan adapter is not registered: {adapter_id}")
        expected_adapter = {"id": adapter_id, **registered_adapter}
        if plan["workload_adapter"] != expected_adapter:
            raise QualificationError("qualification plan adapter identity does not match the fixed registry")
        workload_adapter = {
            "registry_sha256": adapter_registry_sha256,
            "identity": copy.deepcopy(expected_adapter),
        }

    slot = lock.get("configuration_slots", {}).get(plan["configuration_slot"], {})
    if plan["configuration_slot"] == "expert-tuned" or slot.get("status") != "active":
        raise QualificationError("expert-tuned is unavailable without a separately reviewed active tuning record")

    profile = plan["profile"]
    if profile == "kungfu":
        subject = copy.deepcopy(lock["profiles"][profile])
        subject.update(plan["subject"])
    else:
        subject = copy.deepcopy(lock["profiles"][profile])
        if subject.get("status") != "ready":
            raise QualificationError(f"profile is not ready in the environment lock: {profile}")

    limits = parse_compose_limits()
    if profile not in limits or "runner" not in limits:
        raise QualificationError("Compose resource limits are missing for runner or subject service")
    destination = ARTIFACT_ROOT / pathlib.Path(*safe_relative(
        plan["artifact_retention"]["destination"], "artifact_retention.destination"
    ).parts)
    if ARTIFACT_ROOT.resolve() not in destination.resolve().parents:
        raise QualificationError("artifact destination escapes the comparator artifact root")

    resolved = {
        "plan_schema": PLAN_SCHEMA_ID,
        "test_only": plan["test_only"],
        "evidence_class": EVIDENCE_CLASS,
        "profile": profile,
        "configuration_slot": plan["configuration_slot"],
        "build_images_git_sha": git_head(),
        "plan_sha256": sha256_file(plan_path),
        "charter": plan["charter"],
        "fixture_set": plan["fixture_set"],
        "compose": {"path": str(COMPOSE_PATH), "sha256": actual_compose_sha},
        "environment_lock": {"path": str(LOCK_PATH), "sha256": actual_lock_sha},
        "runner_image": environment["runner_image"],
        "subject": subject,
        "workload_adapter": workload_adapter,
        "resource_limits": {"runner": limits["runner"], profile: limits[profile]},
        "repetitions": plan["repetitions"],
        "artifact_destination": str(destination),
        "retention_days": plan["artifact_retention"]["retention_days"],
        "cleanup_scope": "unique Compose project and project-scoped volumes only",
        "claim_boundary": CLAIM_BOUNDARY,
    }
    return plan, resolved


def command_text(command: list[str]) -> str:
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise QualificationError(f"runtime command failed: {' '.join(command)}: {error}") from error
    return result.stdout.strip()


def runtime_facts(resource_limits: dict[str, Any]) -> dict[str, Any]:
    memory_bytes = 0
    try:
        memory_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        pass
    cgroup: dict[str, Any] = {"version": "unknown", "files": {}}
    controllers = pathlib.Path("/sys/fs/cgroup/cgroup.controllers")
    if controllers.is_file():
        cgroup["version"] = "v2"
        for name in ("cgroup.controllers", "cpu.max", "memory.max", "memory.current"):
            path = controllers.parent / name
            if path.is_file():
                cgroup["files"][name] = path.read_text(encoding="utf-8", errors="replace").strip()
    docker_version_text = command_text(["docker", "version", "--format", "{{json .}}"])
    docker_info_text = command_text(["docker", "info", "--format", "{{json .}}"])
    try:
        docker_version = json.loads(docker_version_text)
        docker_info = json.loads(docker_info_text)
    except json.JSONDecodeError as error:
        raise QualificationError(f"Docker runtime facts are not JSON: {error}") from error
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "cpu_count": os.cpu_count() or 0,
        "memory_bytes": memory_bytes,
        "docker_version": docker_version,
        "docker_compose_version": command_text(["docker", "compose", "version", "--short"]),
        "docker_info": docker_info,
        "cgroup": cgroup,
        "resource_limits": resource_limits,
    }


def normalize_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def controlled_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if name in RUNTIME_ENV_ALLOWLIST}


def evaluate_oracles(step_dir: pathlib.Path, exit_code: int, oracles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for oracle in oracles:
        oracle_type = oracle["type"]
        actual: Any
        passed: bool
        if oracle_type == "exit-code":
            actual = exit_code
            passed = actual == oracle["expected"]
        else:
            relative = safe_relative(oracle["path"], "oracle.path")
            target = step_dir / pathlib.Path(*relative.parts)
            if oracle_type == "artifact-exists":
                actual = target.is_file()
                passed = actual is True
            else:
                actual = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
                passed = target.is_file() and oracle["expected"] in actual
                actual = {"file_exists": target.is_file(), "contains": oracle["expected"] if passed else ""}
        results.append({"type": oracle_type, "path": oracle.get("path", ""), "expected": oracle["expected"], "actual": actual, "passed": passed})
    return results


def execute_step(
    profile: str,
    subject: dict[str, Any],
    workload_adapter: dict[str, Any] | None,
    bundle_id: str,
    repetition: int,
    scenario: dict[str, Any],
    step: dict[str, Any],
    repetition_dir: pathlib.Path,
) -> dict[str, Any]:
    step_identity = hashlib.sha256(
        f"{scenario['id']}:{scenario.get('tier', '')}:{step['id']}".encode("utf-8")
    ).hexdigest()[:10]
    project = f"kf-qual-{profile}-{bundle_id[-12:]}-r{repetition:03d}-{step_identity}"
    project = re.sub(r"[^A-Za-z0-9_-]", "-", project)[:63]
    step_dir = repetition_dir / "raw" / scenario["id"] / step["id"]
    step_dir.mkdir(parents=True, exist_ok=True)
    environment = controlled_environment()
    environment["COMPARATOR_PROJECT_NAME"] = project
    if profile == "kungfu":
        environment.update(
            {
                "KUNGFU_CLI_ARTIFACT_NAME": subject["artifact_name"],
                "KUNGFU_CLI_PACKAGE_SHA256": subject["package_sha256"],
                "KUNGFU_CLI_VERSION": subject["version"],
                "KUNGFU_CLI_SOURCE_SHA": subject["source_sha"],
                "KUNGFU_CLI_EVIDENCE_URL": subject["evidence_url"],
            }
        )
    adapter_evidence: dict[str, Any] | None = None
    if step["action"] == WORKLOAD_ACTION:
        if not isinstance(workload_adapter, dict):
            raise QualificationError("workload adapter step is missing its resolved adapter")
        adapter = workload_adapter["identity"]
        binding = workload_binding(
            profile,
            workload_adapter["registry_sha256"],
            adapter,
            scenario,
            step,
        )
        adapter_path = PILOT_DIR / pathlib.Path(*safe_relative(adapter["artifact"], "adapter artifact").parts)
        command = [
            sys.executable,
            str(adapter_path),
            "run",
            "--project",
            project,
            "--scenario-id",
            scenario["id"],
            "--job-id",
            scenario["job_id"],
            "--tier",
            scenario["tier"],
            "--step-id",
            step["id"],
            "--expected-binding-sha256",
            binding["sha256"],
            "--output-dir",
            str(step_dir),
        ]
        adapter_evidence = {
            "id": adapter["id"],
            "version": adapter["version"],
            "entrypoint": adapter["entrypoint"],
            "artifact": adapter["artifact"],
            "artifact_sha256": adapter["sha256"],
            "fixture": adapter["fixture"],
            "registry_sha256": workload_adapter["registry_sha256"],
            "binding_sha256": binding["sha256"],
            "receipt_path": "adapter-receipt.json",
        }
    else:
        command = ["bash", str(PILOT_SCRIPT), "smoke", profile, "--execute"]
    started_at = utc_now()
    started = time.monotonic()
    timed_out = False
    try:
        process = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=step["timeout_seconds"],
        )
        exit_code = process.returncode
        stdout = process.stdout
        stderr = process.stderr
    except subprocess.TimeoutExpired as error:
        timed_out = True
        exit_code = 124
        stdout = normalize_timeout_output(error.stdout)
        stderr = normalize_timeout_output(error.stderr) + "\nqualification step timed out\n"
    try:
        cleanup = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_PATH),
                "--project-name",
                project,
                "--profile",
                profile,
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        cleanup_exit_code = cleanup.returncode
        cleanup_stdout = cleanup.stdout
        cleanup_stderr = cleanup.stderr
    except subprocess.TimeoutExpired as error:
        cleanup_exit_code = 124
        cleanup_stdout = normalize_timeout_output(error.stdout)
        cleanup_stderr = normalize_timeout_output(error.stderr) + "\nproject cleanup timed out\n"
    duration = round(time.monotonic() - started, 6)
    (step_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    (step_dir / "stderr.log").write_text(stderr, encoding="utf-8")
    (step_dir / "cleanup.stdout.log").write_text(cleanup_stdout, encoding="utf-8")
    (step_dir / "cleanup.stderr.log").write_text(cleanup_stderr, encoding="utf-8")
    pilot_artifacts = ARTIFACT_ROOT / project
    if step["action"] == "profile-smoke" and pilot_artifacts.is_dir():
        shutil.copytree(pilot_artifacts, step_dir / "pilot")
    oracle_results = evaluate_oracles(step_dir, exit_code, step["oracles"])
    return {
        "scenario_id": scenario["id"],
        "job_id": scenario["job_id"],
        "tier": scenario.get("tier", ""),
        "step_id": step["id"],
        "action": step["action"],
        "adapter": adapter_evidence,
        "project": project,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": duration,
        "timeout_seconds": step["timeout_seconds"],
        "timed_out": timed_out,
        "exit_code": exit_code,
        "cleanup_exit_code": cleanup_exit_code,
        "oracles": oracle_results,
        "passed": cleanup_exit_code == 0 and exit_code == 0 and all(result["passed"] for result in oracle_results),
    }


def artifact_records(root: pathlib.Path, *, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    excluded = exclude or set()
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        records.append({"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)})
    return records


def retention_results(repetition_dir: pathlib.Path, patterns: list[str]) -> list[dict[str, Any]]:
    results = []
    for pattern in patterns:
        matches = sorted(path.relative_to(repetition_dir).as_posix() for path in repetition_dir.glob(pattern) if path.is_file())
        results.append({"pattern": pattern, "matches": matches, "passed": bool(matches)})
    return results


def copy_contracts(bundle_dir: pathlib.Path) -> list[dict[str, Any]]:
    contract_dir = bundle_dir / "contracts"
    contract_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for source in (PLAN_SCHEMA_PATH, RUN_SCHEMA_PATH, BUNDLE_SCHEMA_PATH):
        target = contract_dir / source.name
        shutil.copy2(source, target)
        records.append({"path": target.relative_to(bundle_dir).as_posix(), "sha256": sha256_file(target)})
    return records


def copy_bundle_inputs(bundle_dir: pathlib.Path, resolved: dict[str, Any]) -> dict[str, Any]:
    input_dir = bundle_dir / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, str]] = {}
    for role, source in (
        ("compose", COMPOSE_PATH),
        ("environment_lock", LOCK_PATH),
        ("pilot_script", PILOT_SCRIPT),
        ("qualification_runner", QUALIFICATION_RUNNER),
    ):
        target = input_dir / source.name
        shutil.copy2(source, target)
        files[role] = {
            "path": target.relative_to(bundle_dir).as_posix(),
            "sha256": sha256_file(target),
        }
    workload_adapter = resolved.get("workload_adapter")
    if not isinstance(workload_adapter, dict):
        raise QualificationError("production qualification bundle is missing its workload adapter")
    adapter = workload_adapter["identity"]
    adapter_files: dict[str, dict[str, str]] = {}
    for role, source_relative in (
        ("registry", pathlib.PurePosixPath("workload-adapters/registry.json")),
        ("artifact", safe_relative(adapter["artifact"], "workload adapter artifact")),
        ("fixture", safe_relative(adapter["fixture"]["path"], "workload adapter fixture")),
    ):
        source = PILOT_DIR / pathlib.Path(*source_relative.parts)
        target = input_dir / pathlib.Path(*source_relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        adapter_files[role] = {
            "path": target.relative_to(bundle_dir).as_posix(),
            "sha256": sha256_file(target),
        }
    return {
        **files,
        "runner_image": resolved["runner_image"],
        "subject": resolved["subject"],
        "subject_sha256": sha256_json(resolved["subject"]),
        "workload_adapter": {
            **adapter_files,
            "identity": adapter,
            "registry_sha256": workload_adapter["registry_sha256"],
        },
    }


def run_plan(plan_path: pathlib.Path) -> pathlib.Path:
    plan, resolved = resolve_plan(plan_path)
    if plan["test_only"]:
        raise QualificationError("test-only plans cannot issue qualification bundles")
    require_clean_source()
    runtime = runtime_facts(resolved["resource_limits"])
    bundle_id = f"qual-{resolved['profile']}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
    destination = pathlib.Path(resolved["artifact_destination"])
    bundle_dir = destination / bundle_id
    if bundle_dir.exists():
        raise QualificationError(f"qualification bundle already exists: {bundle_dir}")
    bundle_dir.mkdir(parents=True)
    plan_copy = bundle_dir / "qualification-plan.json"
    shutil.copy2(plan_path, plan_copy)
    contracts = copy_contracts(bundle_dir)
    bundle_inputs = copy_bundle_inputs(bundle_dir, resolved)
    run_records: list[dict[str, Any]] = []
    completed = 0

    for repetition in range(1, plan["repetitions"] + 1):
        repetition_started_at = utc_now()
        repetition_started = time.monotonic()
        repetition_dir = bundle_dir / f"repetition-{repetition:03d}"
        step_results: list[dict[str, Any]] = []
        for scenario in plan["scenarios"]:
            for step in scenario["steps"]:
                step_results.append(
                    execute_step(
                        resolved["profile"],
                        resolved["subject"],
                        resolved["workload_adapter"],
                        bundle_id,
                        repetition,
                        scenario,
                        step,
                        repetition_dir,
                    )
                )
        retained = retention_results(repetition_dir, plan["artifact_retention"]["required_patterns"])
        candidate = all(step["passed"] for step in step_results) and all(item["passed"] for item in retained)
        if candidate:
            completed += 1
        manifest = {
            "run_schema": RUN_SCHEMA_ID,
            "schema_version": 1,
            "status": "passed" if candidate else "failed",
            "evidence_class": EVIDENCE_CLASS,
            "qualification_candidate": candidate,
            "native_performance_authority": False,
            "user_outcome_qualification_authority": False,
            "fresh_install_cost_authority": False,
            "final_scoring_authority": False,
            "bundle_id": bundle_id,
            "run_id": f"{bundle_id}-r{repetition:03d}",
            "repetition": repetition,
            "profile": resolved["profile"],
            "configuration_slot": resolved["configuration_slot"],
            "started_at": repetition_started_at,
            "finished_at": utc_now(),
            "duration_seconds": round(time.monotonic() - repetition_started, 6),
            "inputs": {
                "build_images_git_sha": resolved["build_images_git_sha"],
                "plan_sha256": resolved["plan_sha256"],
                "charter": resolved["charter"],
                "fixture_set": resolved["fixture_set"],
                "compose_sha256": resolved["compose"]["sha256"],
                "environment_lock_sha256": resolved["environment_lock"]["sha256"],
                "runner_image": resolved["runner_image"],
                "pilot_script_sha256": sha256_file(PILOT_SCRIPT),
                "qualification_runner_sha256": sha256_file(QUALIFICATION_RUNNER),
                "subject": resolved["subject"],
                "subject_sha256": sha256_json(resolved["subject"]),
                "workload_adapter": resolved["workload_adapter"],
            },
            "runtime": runtime,
            "steps": step_results,
            "retention": retained,
            "artifacts": artifact_records(repetition_dir, exclude={"run-manifest.json"}),
            "claim_boundary": CLAIM_BOUNDARY,
        }
        manifest_path = repetition_dir / "run-manifest.json"
        write_json(manifest_path, manifest)
        run_records.append(
            {
                "repetition": repetition,
                "path": manifest_path.relative_to(bundle_dir).as_posix(),
                "sha256": sha256_file(manifest_path),
                "status": manifest["status"],
            }
        )

    authoritative = completed == plan["repetitions"] and len(run_records) == plan["repetitions"]
    bundle_manifest = {
        "bundle_schema": BUNDLE_SCHEMA_ID,
        "schema_version": 1,
        "status": "complete" if authoritative else "incomplete",
        "evidence_class": EVIDENCE_CLASS,
        "native_performance_authority": False,
        "user_outcome_qualification_authority": authoritative,
        "fresh_install_cost_authority": False,
        "final_scoring_authority": False,
        "bundle_id": bundle_id,
        "generated_at": utc_now(),
        "profile": resolved["profile"],
        "configuration_slot": resolved["configuration_slot"],
        "build_images_git_sha": resolved["build_images_git_sha"],
        "inputs": bundle_inputs,
        "plan": {"path": plan_copy.name, "sha256": sha256_file(plan_copy)},
        "expected_repetitions": plan["repetitions"],
        "completed_repetitions": completed,
        "contracts": contracts,
        "run_manifests": run_records,
        "claim_boundary": CLAIM_BOUNDARY,
    }
    write_json(bundle_dir / "bundle-manifest.json", bundle_manifest)
    verify_bundle(bundle_dir, require_authority=authoritative)
    if not authoritative:
        raise QualificationError(f"qualification bundle is incomplete: {bundle_dir}")
    return bundle_dir


def verify_bundle(bundle_dir: pathlib.Path, *, require_authority: bool = True) -> dict[str, Any]:
    manifest_path = bundle_dir / "bundle-manifest.json"
    bundle = load_json(manifest_path)
    require_keys(
        bundle,
        {
            "bundle_schema", "schema_version", "status", "evidence_class",
            "native_performance_authority", "user_outcome_qualification_authority",
            "fresh_install_cost_authority", "final_scoring_authority", "bundle_id",
            "generated_at", "profile", "configuration_slot", "build_images_git_sha",
            "inputs", "plan", "expected_repetitions", "completed_repetitions",
            "contracts", "run_manifests", "claim_boundary",
        },
        set(),
        "bundle manifest",
    )
    if bundle.get("bundle_schema") != BUNDLE_SCHEMA_ID or bundle.get("schema_version") != 1:
        raise QualificationError("bundle manifest schema/version is unsupported")
    if bundle.get("evidence_class") != EVIDENCE_CLASS:
        raise QualificationError("bundle evidence class is invalid")
    for field in ("native_performance_authority", "fresh_install_cost_authority", "final_scoring_authority"):
        if bundle.get(field) is not False:
            raise QualificationError(f"bundle must force {field}=false")
    if not isinstance(bundle.get("build_images_git_sha"), str) or not GIT_SHA.fullmatch(bundle["build_images_git_sha"]):
        raise QualificationError("bundle build_images_git_sha is invalid")

    plan_record = bundle.get("plan")
    if not isinstance(plan_record, dict):
        raise QualificationError("bundle plan record is missing")
    plan_relative = safe_relative(plan_record.get("path"), "bundle.plan.path")
    plan_path = bundle_dir / pathlib.Path(*plan_relative.parts)
    if not plan_path.is_file() or sha256_file(plan_path) != plan_record.get("sha256"):
        raise QualificationError("bundle plan digest mismatch")
    plan = load_json(plan_path)
    validate_plan_document(plan)
    if plan.get("test_only"):
        raise QualificationError("test-only plans cannot produce authoritative bundles")
    if bundle.get("profile") != plan.get("profile") or bundle.get("configuration_slot") != plan.get("configuration_slot"):
        raise QualificationError("bundle profile/configuration does not match the plan")
    if bundle.get("claim_boundary") != CLAIM_BOUNDARY:
        raise QualificationError("bundle claim boundary is invalid")

    bundle_inputs = bundle.get("inputs")
    if not isinstance(bundle_inputs, dict):
        raise QualificationError("bundle inputs are missing")
    require_keys(
        bundle_inputs,
        {
            "compose", "environment_lock", "pilot_script", "qualification_runner",
            "runner_image", "subject", "subject_sha256", "workload_adapter",
        },
        set(),
        "bundle inputs",
    )
    expected_input_paths = {
        "compose": "inputs/compose.yaml",
        "environment_lock": "inputs/environment.lock.json",
        "pilot_script": "inputs/pilot.sh",
        "qualification_runner": "inputs/comparator_qualification.py",
    }
    input_paths: dict[str, pathlib.Path] = {}
    for role, expected_path in expected_input_paths.items():
        record = bundle_inputs.get(role)
        if not isinstance(record, dict):
            raise QualificationError(f"bundle input record is invalid: {role}")
        require_keys(record, {"path", "sha256"}, set(), f"bundle input {role}")
        if record.get("path") != expected_path:
            raise QualificationError(f"bundle input path is unexpected: {role}")
        relative = safe_relative(record["path"], f"bundle input {role}.path")
        path = bundle_dir / pathlib.Path(*relative.parts)
        if not path.is_file() or sha256_file(path) != record.get("sha256"):
            raise QualificationError(f"bundle input digest mismatch: {role}")
        input_paths[role] = path
    if bundle_inputs["compose"]["sha256"] != plan["environment"]["compose_sha256"]:
        raise QualificationError("bundle Compose input does not match the plan")
    if bundle_inputs["environment_lock"]["sha256"] != plan["environment"]["environment_lock_sha256"]:
        raise QualificationError("bundle environment lock input does not match the plan")
    if bundle_inputs.get("runner_image") != plan["environment"]["runner_image"]:
        raise QualificationError("bundle runner image does not match the plan")
    copied_lock = load_json(input_paths["environment_lock"])
    locked_subject = copy.deepcopy(copied_lock.get("profiles", {}).get(bundle["profile"]))
    if not isinstance(locked_subject, dict):
        raise QualificationError("bundle environment lock does not contain the selected profile")
    if bundle["profile"] == "kungfu":
        locked_subject.update(plan["subject"])
    expected_subject_sha = sha256_json(locked_subject)
    if bundle_inputs.get("subject") != locked_subject or bundle_inputs.get("subject_sha256") != expected_subject_sha:
        raise QualificationError("bundle subject does not match the frozen environment inputs")

    copied_adapter = bundle_inputs.get("workload_adapter")
    if not isinstance(copied_adapter, dict):
        raise QualificationError("bundle workload adapter inputs are missing")
    require_keys(
        copied_adapter,
        {"registry", "artifact", "fixture", "identity", "registry_sha256"},
        set(),
        "bundle workload adapter",
    )
    expected_adapter_paths = {
        "registry": "inputs/workload-adapters/registry.json",
        "artifact": f"inputs/{plan['workload_adapter']['artifact']}",
        "fixture": f"inputs/{plan['workload_adapter']['fixture']['path']}",
    }
    copied_adapter_paths: dict[str, pathlib.Path] = {}
    for role, expected_path in expected_adapter_paths.items():
        record = copied_adapter.get(role)
        if not isinstance(record, dict):
            raise QualificationError(f"bundle workload adapter record is invalid: {role}")
        require_keys(record, {"path", "sha256"}, set(), f"bundle workload adapter {role}")
        if record["path"] != expected_path:
            raise QualificationError(f"bundle workload adapter path is unexpected: {role}")
        relative = safe_relative(record["path"], f"bundle workload adapter {role}.path")
        path = bundle_dir / pathlib.Path(*relative.parts)
        if not path.is_file() or sha256_file(path) != record["sha256"]:
            raise QualificationError(f"bundle workload adapter digest mismatch: {role}")
        copied_adapter_paths[role] = path
    if copied_adapter["registry_sha256"] != plan["adapter_registry"]["sha256"]:
        raise QualificationError("bundle workload adapter registry does not match the plan")
    registered_adapters = validate_adapter_registry(
        copied_adapter_paths["registry"],
        bundle_dir / "inputs",
    )
    adapter_id = plan["workload_adapter"]["id"]
    expected_adapter = {"id": adapter_id, **registered_adapters.get(adapter_id, {})}
    if copied_adapter["identity"] != expected_adapter or plan["workload_adapter"] != expected_adapter:
        raise QualificationError("bundle workload adapter identity does not match the copied registry")

    contracts = bundle.get("contracts")
    if not isinstance(contracts, list) or len(contracts) != 3:
        raise QualificationError("bundle must retain all three qualification schemas")
    expected_contract_ids = {PLAN_SCHEMA_ID, RUN_SCHEMA_ID, BUNDLE_SCHEMA_ID}
    observed_contract_ids: set[str] = set()
    for contract in contracts:
        if not isinstance(contract, dict):
            raise QualificationError("bundle contract record is invalid")
        relative = safe_relative(contract.get("path"), "contract.path")
        path = bundle_dir / pathlib.Path(*relative.parts)
        if not path.is_file() or sha256_file(path) != contract.get("sha256"):
            raise QualificationError(f"bundle contract digest mismatch: {relative}")
        observed_contract_ids.add(load_json(path).get("$id", ""))
    if observed_contract_ids != expected_contract_ids:
        raise QualificationError("bundle contract set is incomplete or unexpected")

    expected = bundle.get("expected_repetitions")
    completed = bundle.get("completed_repetitions")
    run_records = bundle.get("run_manifests")
    if expected != plan.get("repetitions") or not isinstance(expected, int) or expected < 3:
        raise QualificationError("bundle repetition contract does not match the plan")
    if not isinstance(run_records, list) or len(run_records) != expected:
        raise QualificationError("bundle does not contain exactly one run manifest per repetition")
    observed_repetitions: list[int] = []
    candidate_count = 0
    for record in run_records:
        if not isinstance(record, dict):
            raise QualificationError("run manifest index record is invalid")
        require_keys(record, {"repetition", "path", "sha256", "status"}, set(), "run manifest index record")
        relative = safe_relative(record.get("path"), "run manifest path")
        run_path = bundle_dir / pathlib.Path(*relative.parts)
        if not run_path.is_file() or sha256_file(run_path) != record.get("sha256"):
            raise QualificationError(f"run manifest digest mismatch: {relative}")
        run = load_json(run_path)
        require_keys(
            run,
            {
                "run_schema", "schema_version", "status", "evidence_class",
                "qualification_candidate", "native_performance_authority",
                "user_outcome_qualification_authority", "fresh_install_cost_authority",
                "final_scoring_authority", "bundle_id", "run_id", "repetition",
                "profile", "configuration_slot", "started_at", "finished_at",
                "duration_seconds", "inputs", "runtime", "steps", "retention",
                "artifacts", "claim_boundary",
            },
            set(),
            f"run manifest {relative}",
        )
        repetition = run.get("repetition")
        observed_repetitions.append(repetition)
        if run.get("run_schema") != RUN_SCHEMA_ID or run.get("schema_version") != 1:
            raise QualificationError(f"run manifest schema/version is invalid: {relative}")
        if run.get("bundle_id") != bundle.get("bundle_id"):
            raise QualificationError(f"run manifest bundle_id mismatch: {relative}")
        if run.get("profile") != bundle.get("profile") or run.get("configuration_slot") != bundle.get("configuration_slot"):
            raise QualificationError(f"run manifest profile/configuration mismatch: {relative}")
        if run.get("claim_boundary") != CLAIM_BOUNDARY:
            raise QualificationError(f"run manifest claim boundary is invalid: {relative}")
        if run.get("native_performance_authority") is not False or run.get("user_outcome_qualification_authority") is not False:
            raise QualificationError(f"individual repetitions cannot carry authority: {relative}")
        if run.get("fresh_install_cost_authority") is not False or run.get("final_scoring_authority") is not False:
            raise QualificationError(f"run manifest claim boundary is invalid: {relative}")
        steps = run.get("steps")
        if not isinstance(steps, list) or not steps:
            raise QualificationError(f"run manifest has no step evidence: {relative}")
        plan_steps = [
            (scenario, step)
            for scenario in plan["scenarios"]
            for step in scenario["steps"]
        ]
        expected_steps = [
            (scenario["id"], scenario["job_id"], scenario["tier"], step["id"], step["action"], step["adapter_id"])
            for scenario, step in plan_steps
        ]
        observed_steps = [
            (
                step.get("scenario_id"), step.get("job_id"), step.get("tier"),
                step.get("step_id"), step.get("action"),
                step.get("adapter", {}).get("id") if isinstance(step.get("adapter"), dict) else None,
            )
            for step in steps
        ]
        if observed_steps != expected_steps:
            raise QualificationError(f"run manifest step order does not match the plan: {relative}")
        for observed_step, (planned_scenario, planned_step) in zip(steps, plan_steps):
            if observed_step.get("cleanup_exit_code") != 0:
                raise QualificationError(f"run manifest project cleanup failed: {relative}")
            step_dir = run_path.parent / "raw" / planned_scenario["id"] / planned_step["id"]
            binding = workload_binding(
                bundle["profile"],
                plan["adapter_registry"]["sha256"],
                plan["workload_adapter"],
                planned_scenario,
                planned_step,
            )
            observed_adapter = observed_step.get("adapter")
            expected_adapter_evidence = {
                "id": plan["workload_adapter"]["id"],
                "version": plan["workload_adapter"]["version"],
                "entrypoint": plan["workload_adapter"]["entrypoint"],
                "artifact": plan["workload_adapter"]["artifact"],
                "artifact_sha256": plan["workload_adapter"]["sha256"],
                "fixture": plan["workload_adapter"]["fixture"],
                "registry_sha256": plan["adapter_registry"]["sha256"],
                "binding_sha256": binding["sha256"],
                "receipt_path": "adapter-receipt.json",
            }
            if observed_adapter != expected_adapter_evidence:
                raise QualificationError(f"run manifest workload binding does not match the plan: {relative}")
            receipt_path = step_dir / "adapter-receipt.json"
            tier_evidence_path = step_dir / "tier-evidence.json"
            if not receipt_path.is_file() or not tier_evidence_path.is_file():
                raise QualificationError(f"workload adapter receipt is missing: {relative}")
            adapter_receipt = load_json(receipt_path)
            expected_receipt_fields = {"status": "passed", **binding["payload"], "binding_sha256": binding["sha256"]}
            for field, expected_value in expected_receipt_fields.items():
                if adapter_receipt.get(field) != expected_value:
                    raise QualificationError(f"workload adapter receipt binding mismatch for {field}: {relative}")
            job_receipt_relative = safe_relative(adapter_receipt.get("job_receipt"), "adapter receipt job_receipt")
            job_receipt_path = step_dir / pathlib.Path(*job_receipt_relative.parts)
            if not job_receipt_path.is_file():
                raise QualificationError(f"workload adapter job receipt is missing: {relative}")
            job_receipt = load_json(job_receipt_path)
            tier_evidence = load_json(tier_evidence_path)
            for evidence_name, evidence in (("job", job_receipt), ("tier", tier_evidence)):
                if (
                    evidence.get("job_id") != planned_scenario["job_id"]
                    or evidence.get("tier") != planned_scenario["tier"]
                    or evidence.get("binding_sha256") != binding["sha256"]
                ):
                    raise QualificationError(f"workload adapter {evidence_name} evidence is relabelled: {relative}")
            recomputed_oracles = evaluate_oracles(step_dir, observed_step.get("exit_code"), planned_step["oracles"])
            if observed_step.get("oracles") != recomputed_oracles:
                raise QualificationError(f"run manifest oracle evidence does not match raw artifacts: {relative}")
            expected_passed = (
                observed_step.get("exit_code") == 0
                and observed_step.get("cleanup_exit_code") == 0
                and all(oracle["passed"] for oracle in recomputed_oracles)
            )
            if observed_step.get("passed") is not expected_passed or not expected_passed:
                raise QualificationError(f"run manifest contains a failed step/oracle: {relative}")
        runtime = run.get("runtime")
        runtime_fields = {
            "platform", "machine", "kernel", "cpu_count", "memory_bytes", "docker_version",
            "docker_compose_version", "docker_info", "cgroup", "resource_limits",
        }
        if not isinstance(runtime, dict) or not runtime_fields.issubset(runtime):
            raise QualificationError(f"run manifest runtime facts are incomplete: {relative}")
        if not runtime.get("docker_version") or not runtime.get("docker_info") or not runtime.get("docker_compose_version"):
            raise QualificationError(f"run manifest Docker facts are empty: {relative}")
        if not isinstance(runtime.get("cpu_count"), int) or runtime["cpu_count"] < 1:
            raise QualificationError(f"run manifest CPU facts are invalid: {relative}")
        if not isinstance(runtime.get("memory_bytes"), int) or runtime["memory_bytes"] < 1:
            raise QualificationError(f"run manifest memory facts are invalid: {relative}")
        limits = runtime.get("resource_limits")
        for service in ("runner", bundle["profile"]):
            service_limits = limits.get(service, {}) if isinstance(limits, dict) else {}
            if not service_limits.get("mem_limit") or not service_limits.get("cpus"):
                raise QualificationError(f"run manifest resource limits are incomplete for {service}: {relative}")
        inputs = run.get("inputs")
        if not isinstance(inputs, dict) or inputs.get("build_images_git_sha") != bundle.get("build_images_git_sha"):
            raise QualificationError(f"run manifest source binding is invalid: {relative}")
        require_keys(
            inputs,
            {
                "build_images_git_sha", "plan_sha256", "charter", "fixture_set",
                "compose_sha256", "environment_lock_sha256", "runner_image",
                "pilot_script_sha256", "qualification_runner_sha256", "subject",
                "subject_sha256", "workload_adapter",
            },
            set(),
            f"run manifest inputs {relative}",
        )
        if inputs.get("plan_sha256") != plan_record.get("sha256"):
            raise QualificationError(f"run manifest plan digest mismatch: {relative}")
        expected_inputs = {
            "charter": plan["charter"],
            "fixture_set": plan["fixture_set"],
            "compose_sha256": plan["environment"]["compose_sha256"],
            "environment_lock_sha256": plan["environment"]["environment_lock_sha256"],
            "runner_image": plan["environment"]["runner_image"],
        }
        for field, expected_value in expected_inputs.items():
            if inputs.get(field) != expected_value:
                raise QualificationError(f"run manifest input mismatch for {field}: {relative}")
        expected_file_digests = {
            "pilot_script_sha256": bundle_inputs["pilot_script"]["sha256"],
            "qualification_runner_sha256": bundle_inputs["qualification_runner"]["sha256"],
        }
        for field, expected_value in expected_file_digests.items():
            if inputs.get(field) != expected_value:
                raise QualificationError(f"run manifest input mismatch for {field}: {relative}")
        if inputs.get("subject") != locked_subject or inputs.get("subject_sha256") != expected_subject_sha:
            raise QualificationError(f"run manifest subject binding is invalid: {relative}")
        expected_workload_input = {
            "registry_sha256": plan["adapter_registry"]["sha256"],
            "identity": plan["workload_adapter"],
        }
        if inputs.get("workload_adapter") != expected_workload_input:
            raise QualificationError(f"run manifest workload adapter input is invalid: {relative}")

        repetition_dir = run_path.parent
        artifacts = run.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise QualificationError(f"run manifest has no raw artifacts: {relative}")
        seen_artifacts: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise QualificationError(f"invalid artifact record: {relative}")
            artifact_relative = safe_relative(artifact.get("path"), "artifact.path")
            artifact_name = artifact_relative.as_posix()
            if artifact_name in seen_artifacts:
                raise QualificationError(f"duplicate artifact record: {artifact_name}")
            seen_artifacts.add(artifact_name)
            artifact_path = repetition_dir / pathlib.Path(*artifact_relative.parts)
            if not artifact_path.is_file():
                raise QualificationError(f"required artifact is missing: {artifact_name}")
            if artifact_path.stat().st_size != artifact.get("size") or sha256_file(artifact_path) != artifact.get("sha256"):
                raise QualificationError(f"artifact integrity mismatch: {artifact_name}")
        actual_artifacts = {
            path.relative_to(repetition_dir).as_posix()
            for path in repetition_dir.rglob("*")
            if path.is_file() and path != run_path
        }
        if seen_artifacts != actual_artifacts:
            raise QualificationError(f"run manifest artifact inventory is incomplete or contains extras: {relative}")
        retention = run.get("retention")
        expected_patterns = plan["artifact_retention"]["required_patterns"]
        if not isinstance(retention, list) or [item.get("pattern") for item in retention] != expected_patterns:
            raise QualificationError(f"run manifest retention contract does not match the plan: {relative}")
        for retained in retention:
            recomputed = sorted(
                path.relative_to(repetition_dir).as_posix()
                for path in repetition_dir.glob(retained["pattern"])
                if path.is_file()
            )
            if retained.get("passed") is not True or retained.get("matches") != recomputed or not recomputed:
                raise QualificationError(f"retention requirement failed: {retained.get('pattern', '')}")
        if run.get("status") != "passed" or run.get("qualification_candidate") is not True:
            raise QualificationError(f"run is not a qualification candidate: {relative}")
        candidate_count += 1

    if observed_repetitions != list(range(1, expected + 1)):
        raise QualificationError("bundle repetitions are missing, duplicated, or out of order")
    if completed != candidate_count or completed != expected:
        raise QualificationError("bundle completed_repetitions is inconsistent")
    authoritative = bundle.get("user_outcome_qualification_authority") is True
    if bundle.get("status") != "complete" or not authoritative:
        if require_authority:
            raise QualificationError("bundle is not an authoritative qualification receipt")
    elif candidate_count != expected:
        raise QualificationError("authoritative bundle is incomplete")
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate-plan", "plan"):
        child = subparsers.add_parser(command)
        child.add_argument("--plan", required=True, type=pathlib.Path)
    run = subparsers.add_parser("run")
    run.add_argument("--plan", required=True, type=pathlib.Path)
    run.add_argument("--execute", action="store_true")
    verify = subparsers.add_parser("verify-bundle")
    verify.add_argument("--bundle", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        if args.command == "validate-plan":
            _, resolved = resolve_plan(args.plan)
            print(json.dumps({"status": "valid", **resolved}, indent=2, sort_keys=True))
        elif args.command == "plan":
            _, resolved = resolve_plan(args.plan)
            print(json.dumps(resolved, indent=2, sort_keys=True))
        elif args.command == "run":
            if not args.execute:
                raise QualificationError("run requires explicit --execute")
            bundle = run_plan(args.plan)
            print(json.dumps({"status": "complete", "bundle": str(bundle), "user_outcome_qualification_authority": True}, indent=2))
        else:
            bundle = verify_bundle(args.bundle)
            print(json.dumps({"status": "verified", "bundle_id": bundle["bundle_id"], "user_outcome_qualification_authority": True}, indent=2))
    except (QualificationError, OSError, subprocess.SubprocessError) as error:
        print(f"comparator qualification error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
