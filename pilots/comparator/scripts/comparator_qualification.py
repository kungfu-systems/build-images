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
ADAPTER_DIR = PILOT_DIR / "workload-adapters"
SEMANTICS_IMPLEMENTATION_PATH = ADAPTER_DIR / "postgres_phase_a_semantics.py"

PLAN_SCHEMA_ID = "urn:kungfu-systems:build-images:comparator-qualification-plan:v1"
RUN_SCHEMA_ID = "urn:kungfu-systems:build-images:comparator-qualification-run:v1"
BUNDLE_SCHEMA_ID = "urn:kungfu-systems:build-images:comparator-qualification-bundle:v1"
EVIDENCE_CLASS = "containerized-user-outcome-qualification"
WORKLOAD_ACTION = "workload-adapter"
JOB_RECEIPTS = {
    "J1-multi-session-progress-triage": "j1-decision-receipt.json",
    "J2-cross-repo-delivery-trust": "j2-delivery-receipt.json",
    "J3-interrupted-go-recovery-handoff": "j3-handoff-receipt.json",
}
PROFILES = ("aeron", "clickhouse", "postgres", "kungfu")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
ID = re.compile(r"^[A-Za-z0-9._-]+$")
COMPOSE_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
EXACT_IMAGE_REF = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
LOCAL_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
TRANSIENT_PULL_ERROR = re.compile(
    r"connection reset|timed? out|timeout|temporary failure|unexpected eof|"
    r"tls handshake timeout|too many requests|\b429\b|\b5\d\d\b",
    re.IGNORECASE,
)
IMAGE_PREPARATION_SCHEMA = "urn:kungfu-systems:build-images:comparator-image-preparation:v1"
PULL_MAX_ATTEMPTS = 3
PULL_RETRY_DELAYS_SECONDS = (2, 5)
PROJECT_RESOURCE_CHECK_TIMEOUT_SECONDS = 15
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
sys.path.insert(0, str(ADAPTER_DIR))
import comparator_pilot  # noqa: E402
from postgres_phase_a_semantics import (  # noqa: E402
    DECISION_LOGIC_VERSION,
    JOB_IDS,
    SemanticError,
    derive_job_verdict,
    validate_execution_fixture,
)


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


def validate_verifier_oracle(oracle: dict[str, Any]) -> None:
    require_keys(oracle, {"schema", "jobs"}, set(), "workload verifier oracle")
    if oracle["schema"] != "urn:kungfu-systems:build-images:postgres-phase-a-verifier-oracle:v1":
        raise QualificationError("workload verifier oracle schema is unsupported")
    jobs = oracle["jobs"]
    if not isinstance(jobs, dict) or set(jobs) != set(JOB_IDS):
        raise QualificationError("workload verifier oracle job set is incomplete or unexpected")
    if any(not isinstance(verdict, dict) or not verdict for verdict in jobs.values()):
        raise QualificationError("workload verifier oracle contains an invalid verdict")


def validate_adapter_registry(registry_path: pathlib.Path, root: pathlib.Path) -> dict[str, dict[str, Any]]:
    registry = load_json(registry_path)
    require_keys(registry, {"schema", "schema_version", "adapters"}, set(), "workload adapter registry")
    if registry["schema"] != "urn:kungfu-systems:build-images:workload-adapter-registry:v2":
        raise QualificationError("workload adapter registry schema is unsupported")
    if registry["schema_version"] != 2 or not isinstance(registry["adapters"], dict) or not registry["adapters"]:
        raise QualificationError("workload adapter registry is empty or has an unsupported version")
    resolved: dict[str, dict[str, Any]] = {}
    for adapter_id, adapter in registry["adapters"].items():
        require_id(adapter_id, "workload adapter id")
        if not isinstance(adapter, dict):
            raise QualificationError(f"workload adapter record must be an object: {adapter_id}")
        require_keys(
            adapter,
            {
                "version", "artifact", "sha256", "entrypoint", "profiles",
                "semantics", "fixture", "oracle", "mappings",
            },
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
        semantics = adapter["semantics"]
        if not isinstance(semantics, dict):
            raise QualificationError(f"workload adapter semantics are invalid: {adapter_id}")
        require_keys(semantics, {"path", "sha256"}, set(), f"workload adapter {adapter_id}.semantics")
        semantics_relative = safe_relative(semantics["path"], f"workload adapter {adapter_id}.semantics.path")
        if not semantics_relative.as_posix().startswith("workload-adapters/"):
            raise QualificationError(f"workload adapter semantics are outside the allowlisted directory: {adapter_id}")
        semantics_path = root / pathlib.Path(*semantics_relative.parts)
        require_sha256(semantics["sha256"], f"workload adapter {adapter_id}.semantics.sha256")
        if not semantics_path.is_file() or sha256_file(semantics_path) != semantics["sha256"]:
            raise QualificationError(f"workload adapter semantics digest mismatch: {adapter_id}")
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
        try:
            validate_execution_fixture(load_json(fixture_path))
        except SemanticError as error:
            raise QualificationError(str(error)) from error
        oracle = adapter["oracle"]
        if not isinstance(oracle, dict):
            raise QualificationError(f"workload adapter oracle is invalid: {adapter_id}")
        require_keys(oracle, {"path", "sha256"}, set(), f"workload adapter {adapter_id}.oracle")
        oracle_relative = safe_relative(oracle["path"], f"workload adapter {adapter_id}.oracle.path")
        if not oracle_relative.as_posix().startswith("workload-adapters/oracles/"):
            raise QualificationError(f"workload adapter oracle is outside the verifier-only directory: {adapter_id}")
        oracle_path = root / pathlib.Path(*oracle_relative.parts)
        require_sha256(oracle["sha256"], f"workload adapter {adapter_id}.oracle.sha256")
        if not oracle_path.is_file() or sha256_file(oracle_path) != oracle["sha256"]:
            raise QualificationError(f"workload adapter oracle digest mismatch: {adapter_id}")
        validate_verifier_oracle(load_json(oracle_path))
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
        "schema": "urn:kungfu-systems:build-images:workload-binding:v2",
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
        "semantics_sha256": adapter["semantics"]["sha256"],
        "fixture_sha256": adapter["fixture"]["sha256"],
        "oracle_sha256": adapter["oracle"]["sha256"],
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
            {
                "id", "version", "artifact", "sha256", "entrypoint", "profiles",
                "semantics", "fixture", "oracle", "mappings",
            },
            set(),
            "workload_adapter",
        )
        adapter_id = require_id(adapter["id"], "workload_adapter.id")
        if not isinstance(adapter["version"], str) or not adapter["version"]:
            raise QualificationError("workload_adapter.version must be non-empty")
        require_id(adapter["entrypoint"], "workload_adapter.entrypoint")
        safe_relative(adapter["artifact"], "workload_adapter.artifact")
        require_sha256(adapter["sha256"], "workload_adapter.sha256")
        semantics = adapter["semantics"]
        if not isinstance(semantics, dict):
            raise QualificationError("workload_adapter.semantics must be an object")
        require_keys(semantics, {"path", "sha256"}, set(), "workload_adapter.semantics")
        safe_relative(semantics["path"], "workload_adapter.semantics.path")
        require_sha256(semantics["sha256"], "workload_adapter.semantics.sha256")
        fixture = adapter["fixture"]
        if not isinstance(fixture, dict):
            raise QualificationError("workload_adapter.fixture must be an object")
        require_keys(fixture, {"path", "sha256"}, set(), "workload_adapter.fixture")
        safe_relative(fixture["path"], "workload_adapter.fixture.path")
        require_sha256(fixture["sha256"], "workload_adapter.fixture.sha256")
        oracle = adapter["oracle"]
        if not isinstance(oracle, dict):
            raise QualificationError("workload_adapter.oracle must be an object")
        require_keys(oracle, {"path", "sha256"}, set(), "workload_adapter.oracle")
        safe_relative(oracle["path"], "workload_adapter.oracle.path")
        require_sha256(oracle["sha256"], "workload_adapter.oracle.sha256")
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
            has_semantic = False
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
                elif oracle_type == "semantic-verdict":
                    if oracle["expected"] is not True or "path" in oracle:
                        raise QualificationError(f"{oracle_context} must require a true semantic verdict without a path")
                    if has_semantic:
                        raise QualificationError(f"{step_context} must not duplicate semantic verdict oracles")
                    has_semantic = True
                else:
                    raise QualificationError(f"{oracle_context}.type is unsupported")
            if not has_exit or not has_artifact:
                raise QualificationError(f"{step_context} must include exit-code and artifact oracles")
            if not plan["test_only"] and not has_semantic:
                raise QualificationError(f"{step_context} must include a verifier-only semantic verdict oracle")
            if plan["test_only"] and has_semantic:
                raise QualificationError(f"{step_context} cannot use a production semantic verdict oracle")

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


def inspect_project_resources(
    project: str,
    environment: dict[str, str],
    *,
    timeout_seconds: int = PROJECT_RESOURCE_CHECK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    label = f"label=com.docker.compose.project={project}"
    commands = {
        "containers": ["docker", "ps", "-aq", "--filter", label],
        "volumes": ["docker", "volume", "ls", "-q", "--filter", label],
        "networks": ["docker", "network", "ls", "-q", "--filter", label],
    }
    checks: dict[str, Any] = {}
    for resource, command in commands.items():
        timed_out = False
        try:
            process = subprocess.run(
                command,
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            stdout = process.stdout
            stderr = process.stderr
            exit_code = process.returncode
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout = normalize_timeout_output(error.stdout)
            stderr = normalize_timeout_output(error.stderr) + "\nproject resource check timed out\n"
            exit_code = 124
        checks[resource] = {
            "argv": command,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
            "stdout": stdout,
            "stderr": stderr,
            "resources": [line for line in stdout.splitlines() if line],
        }
    return {
        "schema": "urn:kungfu-systems:build-images:comparator-project-cleanup:v1",
        "project": project,
        "checks": checks,
        "passed": all(
            check["exit_code"] == 0 and not check["resources"]
            for check in checks.values()
        ),
    }


def controlled_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if name in RUNTIME_ENV_ALLOWLIST}


def compose_project_name(
    profile: str,
    bundle_id: str,
    repetition: int,
    scenario: dict[str, Any],
    step: dict[str, Any],
) -> str:
    step_identity = hashlib.sha256(
        f"{scenario['id']}:{scenario.get('tier', '')}:{step['id']}".encode("utf-8")
    ).hexdigest()[:10]
    raw = f"kf-qual-{profile}-{bundle_id[-12:]}-r{repetition:03d}-{step_identity}"
    project = re.sub(r"[^a-z0-9_-]", "-", raw.lower())[:63]
    if COMPOSE_PROJECT_NAME.fullmatch(project) is None:
        raise QualificationError(f"generated Compose project name is invalid: {project}")
    return project


def qualification_project_names(plan: dict[str, Any], profile: str, bundle_id: str) -> list[str]:
    projects = [
        compose_project_name(profile, bundle_id, repetition, scenario, step)
        for repetition in range(1, plan["repetitions"] + 1)
        for scenario in plan["scenarios"]
        for step in scenario["steps"]
    ]
    if len(projects) != len(set(projects)):
        raise QualificationError("generated Compose project names contain a collision")
    return projects


def preflight_compose_environment(profile: str, project: str) -> None:
    try:
        process = subprocess.run(
            [
                "docker", "compose", "-f", str(COMPOSE_PATH),
                "--project-name", project,
                "--profile", profile,
                "config", "--quiet",
            ],
            cwd=REPO_ROOT,
            env={**controlled_environment(), "COMPARATOR_PROJECT_NAME": project},
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        detail = normalize_timeout_output(error.stderr).strip() or "Compose validation timed out"
        raise QualificationError(
            f"locked Compose environment preflight failed before service startup: {detail}"
        ) from error
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "unknown Compose validation error"
        raise QualificationError(f"locked Compose environment preflight failed before service startup: {detail}")


def preparation_file_record(root: pathlib.Path, path: pathlib.Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def compose_profile_images(
    profile: str,
    project: str,
    preparation_dir: pathlib.Path,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    command = [
        "docker", "compose", "-f", str(COMPOSE_PATH),
        "--project-name", project,
        "--profile", profile,
        "config", "--format", "json",
    ]
    try:
        process = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env={**controlled_environment(), "COMPARATOR_PROJECT_NAME": project},
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        process = subprocess.CompletedProcess(
            command,
            124,
            normalize_timeout_output(error.stdout),
            normalize_timeout_output(error.stderr) + "\nCompose image discovery timed out\n",
        )
    stdout_path = preparation_dir / "discovery.stdout.log"
    stderr_path = preparation_dir / "discovery.stderr.log"
    stdout_path.write_text(process.stdout, encoding="utf-8")
    stderr_path.write_text(process.stderr, encoding="utf-8")
    discovery = evidence if evidence is not None else {}
    discovery.update({
        "command": command,
        "exit_code": process.returncode,
        "stdout": preparation_file_record(preparation_dir, stdout_path),
        "stderr": preparation_file_record(preparation_dir, stderr_path),
        "services": [],
    })
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "unknown Compose discovery error"
        raise QualificationError(f"locked Compose image discovery failed before formal execution: {detail}")
    try:
        config = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise QualificationError(f"locked Compose image discovery is not JSON: {error}") from error
    services = config.get("services") if isinstance(config, dict) else None
    if not isinstance(services, dict) or not services:
        raise QualificationError("locked Compose profile has no services to prepare")
    for service_name, service in sorted(services.items()):
        if not isinstance(service, dict):
            raise QualificationError(f"locked Compose service is invalid: {service_name}")
        image = service.get("image")
        platform_name = service.get("platform")
        if not isinstance(image, str) or EXACT_IMAGE_REF.fullmatch(image) is None:
            raise QualificationError(
                f"locked Compose service is not backed by an exact digest: {service_name}"
            )
        if not isinstance(platform_name, str) or not platform_name:
            raise QualificationError(f"locked Compose service platform is missing: {service_name}")
        discovery["services"].append(
            {"service": service_name, "image": image, "platform": platform_name}
        )
    if len({item["image"] for item in discovery["services"]}) != len(discovery["services"]):
        raise QualificationError("locked Compose profile reuses an image across multiple services")
    return discovery


def inspect_prepared_image(
    image: str,
    expected_digest: str,
    preparation_dir: pathlib.Path,
    image_index: int,
) -> dict[str, Any]:
    command = ["docker", "image", "inspect", image, "--format", "{{json .}}"]
    try:
        process = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=controlled_environment(),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        process = subprocess.CompletedProcess(
            command,
            124,
            normalize_timeout_output(error.stdout),
            normalize_timeout_output(error.stderr) + "\nlocal image inspection timed out\n",
        )
    stdout_path = preparation_dir / f"image-{image_index:02d}.inspect.stdout.log"
    stderr_path = preparation_dir / f"image-{image_index:02d}.inspect.stderr.log"
    stdout_path.write_text(process.stdout, encoding="utf-8")
    stderr_path.write_text(process.stderr, encoding="utf-8")
    record = {
        "command": command,
        "exit_code": process.returncode,
        "stdout": preparation_file_record(preparation_dir, stdout_path),
        "stderr": preparation_file_record(preparation_dir, stderr_path),
    }
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "unknown local image error"
        raise QualificationError(f"prepared image cannot be inspected: {detail}")
    try:
        inspected = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise QualificationError(f"prepared image inspection is not JSON: {error}") from error
    local_id = inspected.get("Id") if isinstance(inspected, dict) else None
    repo_digests = inspected.get("RepoDigests") if isinstance(inspected, dict) else None
    if not isinstance(local_id, str) or LOCAL_IMAGE_ID.fullmatch(local_id) is None:
        raise QualificationError("prepared image has no valid local image ID")
    if (
        not isinstance(repo_digests, list)
        or not repo_digests
        or not all(isinstance(value, str) for value in repo_digests)
        or not any(value.endswith(f"@{expected_digest}") for value in repo_digests)
    ):
        raise QualificationError("prepared image does not retain the expected repository digest")
    return {**record, "id": local_id, "repo_digests": sorted(repo_digests)}


def prepare_exact_images(profile: str, project: str, preparation_dir: pathlib.Path) -> pathlib.Path:
    preparation_dir.mkdir(parents=True, exist_ok=False)
    started_at = utc_now()
    started = time.monotonic()
    manifest: dict[str, Any] = {
        "schema": IMAGE_PREPARATION_SCHEMA,
        "status": "running",
        "unscored": True,
        "profile": profile,
        "project": project,
        "started_at": started_at,
        "finished_at": "",
        "duration_seconds": 0.0,
        "max_attempts": PULL_MAX_ATTEMPTS,
        "retry_delays_seconds": list(PULL_RETRY_DELAYS_SECONDS),
        "discovery": {},
        "images": [],
    }
    manifest_path = preparation_dir / "image-preparation.json"
    try:
        discovery = compose_profile_images(
            profile,
            project,
            preparation_dir,
            manifest["discovery"],
        )
        for image_index, service in enumerate(discovery["services"], start=1):
            image = service["image"]
            expected_digest = image.rsplit("@", 1)[1]
            image_record: dict[str, Any] = {
                **service,
                "expected_digest": expected_digest,
                "attempts": [],
                "final_status": "failed",
            }
            manifest["images"].append(image_record)
            for attempt in range(1, PULL_MAX_ATTEMPTS + 1):
                command = ["docker", "pull", "--platform", service["platform"], image]
                attempt_started_at = utc_now()
                attempt_started = time.monotonic()
                timed_out = False
                try:
                    process = subprocess.run(
                        command,
                        cwd=REPO_ROOT,
                        env=controlled_environment(),
                        capture_output=True,
                        text=True,
                        timeout=900,
                    )
                except subprocess.TimeoutExpired as error:
                    timed_out = True
                    process = subprocess.CompletedProcess(
                        command,
                        124,
                        normalize_timeout_output(error.stdout),
                        normalize_timeout_output(error.stderr) + "\nexact image pull timed out\n",
                    )
                stdout_path = preparation_dir / (
                    f"image-{image_index:02d}.pull-{attempt:02d}.stdout.log"
                )
                stderr_path = preparation_dir / (
                    f"image-{image_index:02d}.pull-{attempt:02d}.stderr.log"
                )
                stdout_path.write_text(process.stdout, encoding="utf-8")
                stderr_path.write_text(process.stderr, encoding="utf-8")
                retryable = timed_out or TRANSIENT_PULL_ERROR.search(
                    f"{process.stdout}\n{process.stderr}"
                ) is not None
                image_record["attempts"].append(
                    {
                        "attempt": attempt,
                        "command": command,
                        "started_at": attempt_started_at,
                        "finished_at": utc_now(),
                        "duration_seconds": round(time.monotonic() - attempt_started, 6),
                        "timed_out": timed_out,
                        "exit_code": process.returncode,
                        "retryable": retryable,
                        "stdout": preparation_file_record(preparation_dir, stdout_path),
                        "stderr": preparation_file_record(preparation_dir, stderr_path),
                    }
                )
                if process.returncode == 0:
                    image_record["final_status"] = "passed"
                    break
                if not retryable or attempt == PULL_MAX_ATTEMPTS:
                    detail = process.stderr.strip() or process.stdout.strip() or "unknown pull error"
                    raise QualificationError(
                        f"exact image preparation failed after {attempt} attempt(s): {image}: {detail}"
                    )
                time.sleep(PULL_RETRY_DELAYS_SECONDS[attempt - 1])
            image_record["local"] = inspect_prepared_image(
                image,
                expected_digest,
                preparation_dir,
                image_index,
            )
        manifest["status"] = "passed"
    except QualificationError:
        manifest["status"] = "failed"
        raise
    finally:
        manifest["finished_at"] = utc_now()
        manifest["duration_seconds"] = round(time.monotonic() - started, 6)
        write_json(manifest_path, manifest)
    return manifest_path


def verify_semantic_evidence(step_dir: pathlib.Path, context: dict[str, Any]) -> dict[str, Any]:
    job_id = context["job_id"]
    tier = context["tier"]
    binding_sha256 = context["binding_sha256"]
    semantics_path = pathlib.Path(context["semantics_path"])
    semantics_sha256 = context["semantics_sha256"]
    if (
        not semantics_path.is_file()
        or sha256_file(semantics_path) != semantics_sha256
        or sha256_file(SEMANTICS_IMPLEMENTATION_PATH) != semantics_sha256
    ):
        raise QualificationError("verifier semantics implementation does not match the frozen bundle")
    observed_path = step_dir / "observed-facts.json"
    tier_path = step_dir / "tier-evidence.json"
    receipt_path = step_dir / JOB_RECEIPTS[job_id]
    if not observed_path.is_file() or not tier_path.is_file() or not receipt_path.is_file():
        raise QualificationError("semantic evidence is incomplete")
    observed = load_json(observed_path)
    tier_evidence = load_json(tier_path)
    receipt = load_json(receipt_path)
    observed_identity = {
        "schema": "urn:kungfu-systems:build-images:postgres-observed-facts:v1",
        "job_id": job_id,
        "tier": tier,
        "binding_sha256": binding_sha256,
        "query_id": "execution-input-after-tier-event-v1",
    }
    for field, expected in observed_identity.items():
        if observed.get(field) != expected:
            raise QualificationError(f"observed facts identity mismatch for {field}")
    facts = observed.get("facts")
    if not isinstance(facts, dict) or observed.get("facts_sha256") != sha256_json(facts):
        raise QualificationError("observed facts digest does not match the retained query result")
    if (
        tier_evidence.get("job_id") != job_id
        or tier_evidence.get("tier") != tier
        or tier_evidence.get("binding_sha256") != binding_sha256
        or tier_evidence.get("observed_facts_sha256") != observed["facts_sha256"]
    ):
        raise QualificationError("tier evidence is not bound to the observed job facts")
    try:
        verdict = derive_job_verdict(job_id, facts, tier, tier_evidence)
    except SemanticError as error:
        raise QualificationError(str(error)) from error
    expected_receipt = {
        "schema": "urn:kungfu-systems:build-images:postgres-job-receipt:v2",
        "job_id": job_id,
        "tier": tier,
        "fixture_id": context["fixture_id"],
        "binding_sha256": binding_sha256,
        "decision_logic_version": DECISION_LOGIC_VERSION,
        "observed_facts_sha256": observed["facts_sha256"],
        "observed_facts_artifact_sha256": sha256_file(observed_path),
        "tier_evidence_sha256": sha256_file(tier_path),
        **verdict,
    }
    if receipt != expected_receipt:
        raise QualificationError("derived job receipt does not match the retained observations")
    oracle_path = pathlib.Path(context["oracle_path"])
    if not oracle_path.is_file():
        raise QualificationError("verifier-only oracle is missing")
    oracle = load_json(oracle_path)
    validate_verifier_oracle(oracle)
    if oracle["jobs"].get(job_id) != verdict:
        raise QualificationError("derived job verdict does not match the verifier-only oracle")
    return {
        "matched": True,
        "verdict_sha256": sha256_json(verdict),
        "observed_facts_sha256": observed["facts_sha256"],
        "oracle_sha256": sha256_file(oracle_path),
    }


def evaluate_oracles(
    step_dir: pathlib.Path,
    exit_code: int,
    oracles: list[dict[str, Any]],
    semantic_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for oracle in oracles:
        oracle_type = oracle["type"]
        actual: Any
        passed: bool
        if oracle_type == "exit-code":
            actual = exit_code
            passed = actual == oracle["expected"]
        elif oracle_type == "semantic-verdict":
            try:
                if semantic_context is None:
                    raise QualificationError("semantic verdict context is missing")
                actual = verify_semantic_evidence(step_dir, semantic_context)
                passed = actual.get("matched") is True
            except (QualificationError, OSError, KeyError) as error:
                actual = {"matched": False, "error": str(error)}
                passed = False
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
    project = compose_project_name(profile, bundle_id, repetition, scenario, step)
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
    semantic_context: dict[str, Any] | None = None
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
            "semantics": adapter["semantics"],
            "fixture": adapter["fixture"],
            "oracle": adapter["oracle"],
            "registry_sha256": workload_adapter["registry_sha256"],
            "binding_sha256": binding["sha256"],
            "receipt_path": "adapter-receipt.json",
        }
        fixture_path = PILOT_DIR / pathlib.Path(*safe_relative(adapter["fixture"]["path"], "adapter fixture").parts)
        execution_fixture = load_json(fixture_path)
        semantic_context = {
            "job_id": scenario["job_id"],
            "tier": scenario["tier"],
            "binding_sha256": binding["sha256"],
            "fixture_id": execution_fixture["jobs"][scenario["job_id"]]["fixture_id"],
            "semantics_path": PILOT_DIR / pathlib.Path(*safe_relative(adapter["semantics"]["path"], "adapter semantics").parts),
            "semantics_sha256": adapter["semantics"]["sha256"],
            "oracle_path": PILOT_DIR / pathlib.Path(*safe_relative(adapter["oracle"]["path"], "adapter oracle").parts),
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
    cleanup_resources = inspect_project_resources(project, environment)
    cleanup_resources_path = step_dir / "cleanup.resources.json"
    write_json(cleanup_resources_path, cleanup_resources)
    if not cleanup_resources["passed"] and cleanup_exit_code == 0:
        cleanup_exit_code = 125
    duration = round(time.monotonic() - started, 6)
    (step_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    (step_dir / "stderr.log").write_text(stderr, encoding="utf-8")
    (step_dir / "cleanup.stdout.log").write_text(cleanup_stdout, encoding="utf-8")
    (step_dir / "cleanup.stderr.log").write_text(cleanup_stderr, encoding="utf-8")
    pilot_artifacts = ARTIFACT_ROOT / project
    if step["action"] == "profile-smoke" and pilot_artifacts.is_dir():
        shutil.copytree(pilot_artifacts, step_dir / "pilot")
    oracle_results = evaluate_oracles(step_dir, exit_code, step["oracles"], semantic_context)
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
        "cleanup_resources": {
            "path": cleanup_resources_path.name,
            "sha256": sha256_file(cleanup_resources_path),
            "passed": cleanup_resources["passed"],
        },
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
        ("semantics", safe_relative(adapter["semantics"]["path"], "workload adapter semantics")),
        ("fixture", safe_relative(adapter["fixture"]["path"], "workload adapter fixture")),
        ("oracle", safe_relative(adapter["oracle"]["path"], "workload adapter oracle")),
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
    bundle_id = f"qual-{resolved['profile']}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
    projects = qualification_project_names(plan, resolved["profile"], bundle_id)
    preflight_compose_environment(resolved["profile"], projects[0])
    runtime = runtime_facts(resolved["resource_limits"])
    destination = pathlib.Path(resolved["artifact_destination"])
    bundle_dir = destination / bundle_id
    if bundle_dir.exists():
        raise QualificationError(f"qualification bundle already exists: {bundle_dir}")
    bundle_dir.mkdir(parents=True)
    plan_copy = bundle_dir / "qualification-plan.json"
    shutil.copy2(plan_path, plan_copy)
    contracts = copy_contracts(bundle_dir)
    bundle_inputs = copy_bundle_inputs(bundle_dir, resolved)
    preparation_path = bundle_dir / "preparation" / "image-preparation.json"
    try:
        prepare_exact_images(
            resolved["profile"],
            projects[0],
            preparation_path.parent,
        )
    except QualificationError as error:
        raise QualificationError(
            f"{error}; retained unscored preparation evidence: {preparation_path}"
        ) from error
    preparation = {
        "path": preparation_path.relative_to(bundle_dir).as_posix(),
        "sha256": sha256_file(preparation_path),
    }
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
                "image_preparation_sha256": preparation["sha256"],
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
        "preparation": preparation,
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


def verify_preparation(
    bundle_dir: pathlib.Path,
    bundle: dict[str, Any],
    plan: dict[str, Any],
    bundle_inputs: dict[str, Any],
) -> str:
    record = bundle.get("preparation")
    if not isinstance(record, dict):
        raise QualificationError("bundle image preparation record is missing")
    require_keys(record, {"path", "sha256"}, set(), "bundle image preparation record")
    relative = safe_relative(record.get("path"), "bundle preparation.path")
    if relative.as_posix() != "preparation/image-preparation.json":
        raise QualificationError("bundle image preparation path is unexpected")
    path = bundle_dir / pathlib.Path(*relative.parts)
    if not path.is_file() or sha256_file(path) != record.get("sha256"):
        raise QualificationError("bundle image preparation digest mismatch")
    preparation = load_json(path)
    require_keys(
        preparation,
        {
            "schema", "status", "unscored", "profile", "project", "started_at",
            "finished_at", "duration_seconds", "max_attempts", "retry_delays_seconds",
            "discovery", "images",
        },
        set(),
        "image preparation manifest",
    )
    if (
        preparation.get("schema") != IMAGE_PREPARATION_SCHEMA
        or preparation.get("status") != "passed"
        or preparation.get("unscored") is not True
    ):
        raise QualificationError("image preparation is not a passed unscored phase")
    expected_project = qualification_project_names(
        plan,
        bundle["profile"],
        bundle["bundle_id"],
    )[0]
    if preparation.get("profile") != bundle["profile"] or preparation.get("project") != expected_project:
        raise QualificationError("image preparation profile/project binding is invalid")
    if (
        preparation.get("max_attempts") != PULL_MAX_ATTEMPTS
        or preparation.get("retry_delays_seconds") != list(PULL_RETRY_DELAYS_SECONDS)
        or not isinstance(preparation.get("duration_seconds"), (int, float))
        or preparation["duration_seconds"] < 0
    ):
        raise QualificationError("image preparation retry or timing policy is invalid")

    referenced_artifacts: set[str] = set()

    def verify_log(record_value: Any, context: str) -> pathlib.Path:
        if not isinstance(record_value, dict):
            raise QualificationError(f"{context} log record is invalid")
        require_keys(record_value, {"path", "size", "sha256"}, set(), f"{context} log record")
        log_relative = safe_relative(record_value.get("path"), f"{context}.path")
        log_name = log_relative.as_posix()
        if log_name in referenced_artifacts:
            raise QualificationError(f"duplicate preparation artifact record: {log_name}")
        log_path = path.parent / pathlib.Path(*log_relative.parts)
        if (
            not log_path.is_file()
            or log_path.stat().st_size != record_value.get("size")
            or sha256_file(log_path) != record_value.get("sha256")
        ):
            raise QualificationError(f"image preparation artifact integrity mismatch: {log_name}")
        referenced_artifacts.add(log_name)
        return log_path

    discovery = preparation.get("discovery")
    if not isinstance(discovery, dict):
        raise QualificationError("image preparation discovery evidence is missing")
    require_keys(
        discovery,
        {"command", "exit_code", "stdout", "stderr", "services"},
        set(),
        "image preparation discovery",
    )
    command = discovery.get("command")
    if (
        not isinstance(command, list)
        or len(command) != 11
        or command[:3] != ["docker", "compose", "-f"]
        or command[4:] != [
            "--project-name", expected_project,
            "--profile", bundle["profile"],
            "config", "--format", "json",
        ]
        or discovery.get("exit_code") != 0
    ):
        raise QualificationError("image preparation discovery command is invalid")
    discovery_stdout = verify_log(discovery.get("stdout"), "image preparation discovery stdout")
    verify_log(discovery.get("stderr"), "image preparation discovery stderr")

    services = discovery.get("services")
    images = preparation.get("images")
    if not isinstance(services, list) or not isinstance(images, list) or not services or len(images) != len(services):
        raise QualificationError("image preparation service/image set is invalid")
    try:
        discovered_config = json.loads(discovery_stdout.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise QualificationError(f"image preparation discovery log is not JSON: {error}") from error
    discovered_services = discovered_config.get("services") if isinstance(discovered_config, dict) else None
    if not isinstance(discovered_services, dict):
        raise QualificationError("image preparation discovery log has no services")
    services_from_log = [
        {
            "service": service_name,
            "image": service.get("image") if isinstance(service, dict) else None,
            "platform": service.get("platform") if isinstance(service, dict) else None,
        }
        for service_name, service in sorted(discovered_services.items())
    ]
    if services != services_from_log:
        raise QualificationError("image preparation services do not match the discovery log")
    expected_images = {bundle_inputs["runner_image"]}
    subject_image = bundle_inputs.get("subject", {}).get("image")
    if isinstance(subject_image, str):
        expected_images.add(subject_image)
    observed_images = {item.get("image") for item in images if isinstance(item, dict)}
    if observed_images != expected_images:
        raise QualificationError("image preparation does not cover the locked exact image set")

    for image_index, (service, image_record) in enumerate(zip(services, images), start=1):
        if not isinstance(service, dict) or not isinstance(image_record, dict):
            raise QualificationError("image preparation image record is invalid")
        require_keys(service, {"service", "image", "platform"}, set(), "prepared Compose service")
        require_keys(
            image_record,
            {
                "service", "image", "platform", "expected_digest", "attempts",
                "final_status", "local",
            },
            set(),
            "prepared image",
        )
        if {key: image_record.get(key) for key in service} != service:
            raise QualificationError("prepared image is not bound to Compose discovery")
        image = service.get("image")
        platform_name = service.get("platform")
        if not isinstance(image, str) or EXACT_IMAGE_REF.fullmatch(image) is None:
            raise QualificationError("prepared image is not pinned by exact digest")
        expected_digest = image.rsplit("@", 1)[1]
        if image_record.get("expected_digest") != expected_digest or image_record.get("final_status") != "passed":
            raise QualificationError("prepared image final digest/status is invalid")
        attempts = image_record.get("attempts")
        if not isinstance(attempts, list) or not 1 <= len(attempts) <= PULL_MAX_ATTEMPTS:
            raise QualificationError("prepared image attempt count is invalid")
        for attempt_index, attempt in enumerate(attempts, start=1):
            if not isinstance(attempt, dict):
                raise QualificationError("prepared image attempt record is invalid")
            require_keys(
                attempt,
                {
                    "attempt", "command", "started_at", "finished_at", "duration_seconds",
                    "timed_out", "exit_code", "retryable", "stdout", "stderr",
                },
                set(),
                "prepared image attempt",
            )
            if (
                attempt.get("attempt") != attempt_index
                or attempt.get("command") != ["docker", "pull", "--platform", platform_name, image]
                or not isinstance(attempt.get("started_at"), str)
                or not isinstance(attempt.get("finished_at"), str)
                or not isinstance(attempt.get("duration_seconds"), (int, float))
                or attempt["duration_seconds"] < 0
                or not isinstance(attempt.get("timed_out"), bool)
                or not isinstance(attempt.get("retryable"), bool)
            ):
                raise QualificationError("prepared image attempt command/order/timing is invalid")
            if attempt_index < len(attempts):
                if attempt.get("exit_code") == 0 or attempt.get("retryable") is not True:
                    raise QualificationError("prepared image retry is not justified by a transient failure")
            elif attempt.get("exit_code") != 0:
                raise QualificationError("prepared image final pull attempt did not pass")
            pull_stdout = verify_log(
                attempt.get("stdout"),
                f"prepared image {image_index} attempt {attempt_index} stdout",
            )
            pull_stderr = verify_log(
                attempt.get("stderr"),
                f"prepared image {image_index} attempt {attempt_index} stderr",
            )
            pull_output = f"{pull_stdout.read_text(encoding='utf-8')}\n{pull_stderr.read_text(encoding='utf-8')}"
            expected_retryable = attempt["timed_out"] or TRANSIENT_PULL_ERROR.search(pull_output) is not None
            if attempt.get("retryable") is not expected_retryable:
                raise QualificationError("prepared image retry classification does not match retained logs")
            if attempt["timed_out"] and attempt.get("exit_code") != 124:
                raise QualificationError("prepared image timeout exit code is invalid")
        local = image_record.get("local")
        if not isinstance(local, dict):
            raise QualificationError("prepared image local identity is missing")
        require_keys(
            local,
            {"command", "exit_code", "stdout", "stderr", "id", "repo_digests"},
            set(),
            "prepared image local identity",
        )
        if (
            local.get("command") != ["docker", "image", "inspect", image, "--format", "{{json .}}"]
            or local.get("exit_code") != 0
            or not isinstance(local.get("id"), str)
            or LOCAL_IMAGE_ID.fullmatch(local["id"]) is None
            or not isinstance(local.get("repo_digests"), list)
            or not all(isinstance(value, str) for value in local["repo_digests"])
            or not any(value.endswith(f"@{expected_digest}") for value in local["repo_digests"])
        ):
            raise QualificationError("prepared image local digest proof is invalid")
        inspect_stdout = verify_log(local.get("stdout"), f"prepared image {image_index} inspect stdout")
        verify_log(local.get("stderr"), f"prepared image {image_index} inspect stderr")
        try:
            inspected = json.loads(inspect_stdout.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise QualificationError(f"prepared image inspect log is not JSON: {error}") from error
        if (
            not isinstance(inspected, dict)
            or inspected.get("Id") != local["id"]
            or sorted(inspected.get("RepoDigests", [])) != local["repo_digests"]
        ):
            raise QualificationError("prepared image local identity does not match retained inspect output")

    actual_artifacts = {
        artifact.relative_to(path.parent).as_posix()
        for artifact in path.parent.rglob("*")
        if artifact.is_file() and artifact != path
    }
    if referenced_artifacts != actual_artifacts:
        raise QualificationError("image preparation artifact inventory is incomplete or contains extras")
    return record["sha256"]


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
            "inputs", "plan", "preparation", "expected_repetitions", "completed_repetitions",
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
    preparation_sha256 = verify_preparation(bundle_dir, bundle, plan, bundle_inputs)

    copied_adapter = bundle_inputs.get("workload_adapter")
    if not isinstance(copied_adapter, dict):
        raise QualificationError("bundle workload adapter inputs are missing")
    require_keys(
        copied_adapter,
        {"registry", "artifact", "semantics", "fixture", "oracle", "identity", "registry_sha256"},
        set(),
        "bundle workload adapter",
    )
    expected_adapter_paths = {
        "registry": "inputs/workload-adapters/registry.json",
        "artifact": f"inputs/{plan['workload_adapter']['artifact']}",
        "semantics": f"inputs/{plan['workload_adapter']['semantics']['path']}",
        "fixture": f"inputs/{plan['workload_adapter']['fixture']['path']}",
        "oracle": f"inputs/{plan['workload_adapter']['oracle']['path']}",
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
    copied_execution_fixture = load_json(copied_adapter_paths["fixture"])
    try:
        validate_execution_fixture(copied_execution_fixture)
    except SemanticError as error:
        raise QualificationError(str(error)) from error
    validate_verifier_oracle(load_json(copied_adapter_paths["oracle"]))

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
                "semantics": plan["workload_adapter"]["semantics"],
                "fixture": plan["workload_adapter"]["fixture"],
                "oracle": plan["workload_adapter"]["oracle"],
                "registry_sha256": plan["adapter_registry"]["sha256"],
                "binding_sha256": binding["sha256"],
                "receipt_path": "adapter-receipt.json",
            }
            if observed_adapter != expected_adapter_evidence:
                raise QualificationError(f"run manifest workload binding does not match the plan: {relative}")
            if observed_step.get("exit_code") != 0 or not all(
                oracle.get("passed") is True for oracle in observed_step.get("oracles", [])
            ):
                raise QualificationError(
                    "run manifest contains a failed step/oracle: "
                    f"{relative} ({planned_scenario['id']}/{planned_step['id']})"
                )
            if observed_step.get("cleanup_exit_code") != 0:
                raise QualificationError(f"run manifest project cleanup failed: {relative}")
            cleanup_path = step_dir / "cleanup.resources.json"
            cleanup_binding = observed_step.get("cleanup_resources")
            expected_cleanup_binding = {
                "path": "cleanup.resources.json",
                "sha256": sha256_file(cleanup_path) if cleanup_path.is_file() else "",
                "passed": True,
            }
            if cleanup_binding != expected_cleanup_binding:
                raise QualificationError(f"run manifest cleanup resource evidence mismatch: {relative}")
            cleanup_evidence = load_json(cleanup_path)
            cleanup_checks = cleanup_evidence.get("checks")
            if (
                cleanup_evidence.get("project") != observed_step.get("project")
                or cleanup_evidence.get("passed") is not True
                or not isinstance(cleanup_checks, dict)
                or set(cleanup_checks) != {"containers", "volumes", "networks"}
                or any(
                    not isinstance(check, dict)
                    or check.get("exit_code") != 0
                    or check.get("timed_out") is not False
                    or check.get("resources") != []
                    for check in cleanup_checks.values()
                )
            ):
                raise QualificationError(f"run manifest project resources remain after cleanup: {relative}")
            receipt_path = step_dir / "adapter-receipt.json"
            tier_evidence_path = step_dir / "tier-evidence.json"
            observed_facts_path = step_dir / "observed-facts.json"
            if not receipt_path.is_file() or not tier_evidence_path.is_file() or not observed_facts_path.is_file():
                raise QualificationError(f"workload adapter receipt is missing: {relative}")
            adapter_receipt = load_json(receipt_path)
            expected_receipt_fields = {
                "status": "passed",
                **binding["payload"],
                "binding_sha256": binding["sha256"],
                "adapter_artifact": plan["workload_adapter"]["artifact"],
                "semantics_artifact": plan["workload_adapter"]["semantics"]["path"],
                "fixture_path": plan["workload_adapter"]["fixture"]["path"],
                "oracle_sha256": plan["workload_adapter"]["oracle"]["sha256"],
                "observed_facts": "observed-facts.json",
                "tier_evidence": "tier-evidence.json",
            }
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
            semantic_context = {
                "job_id": planned_scenario["job_id"],
                "tier": planned_scenario["tier"],
                "binding_sha256": binding["sha256"],
                "fixture_id": copied_execution_fixture["jobs"][planned_scenario["job_id"]]["fixture_id"],
                "semantics_path": copied_adapter_paths["semantics"],
                "semantics_sha256": plan["workload_adapter"]["semantics"]["sha256"],
                "oracle_path": copied_adapter_paths["oracle"],
            }
            recomputed_oracles = evaluate_oracles(
                step_dir,
                observed_step.get("exit_code"),
                planned_step["oracles"],
                semantic_context,
            )
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
                "subject_sha256", "image_preparation_sha256", "workload_adapter",
            },
            set(),
            f"run manifest inputs {relative}",
        )
        if inputs.get("plan_sha256") != plan_record.get("sha256"):
            raise QualificationError(f"run manifest plan digest mismatch: {relative}")
        if inputs.get("image_preparation_sha256") != preparation_sha256:
            raise QualificationError(f"run manifest image preparation digest mismatch: {relative}")
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
