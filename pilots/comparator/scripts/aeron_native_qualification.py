#!/usr/bin/env python3
"""Run and offline-verify the separately authoritative Aeron native qualification."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import platform
import re
import resource
import shutil
import signal
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any


SCRIPT_PATH = pathlib.Path(__file__).resolve()
PILOT_DIR = SCRIPT_PATH.parent.parent
REPO_ROOT = PILOT_DIR.parents[1]
RUNNER_REPO_PATH = SCRIPT_PATH.relative_to(REPO_ROOT).as_posix()
ARTIFACT_ROOT = PILOT_DIR / ".artifacts" / "aeron-native-qualification"
PLAN_SCHEMA_PATH = PILOT_DIR / "aeron-native-plan.schema.json"
RUN_SCHEMA_PATH = PILOT_DIR / "aeron-native-run-manifest.schema.json"
BUNDLE_SCHEMA_PATH = PILOT_DIR / "aeron-native-bundle-manifest.schema.json"
PLAN_SCHEMA_ID = "urn:kungfu-systems:build-images:aeron-native-qualification-plan:v1"
RUN_SCHEMA_ID = "urn:kungfu-systems:build-images:aeron-native-qualification-run:v1"
BUNDLE_SCHEMA_ID = "urn:kungfu-systems:build-images:aeron-native-qualification-bundle:v1"
EVIDENCE_CLASS = "native-host-performance-qualification"
CLAIM_BOUNDARY = (
    "Native-host Aeron IPC and Archive qualification only; containerized user outcomes, "
    "fresh-install cost, final scoring, Aeron Cluster, Premium transports, replication, "
    "and high availability remain outside this bundle."
)
EXACT_IMAGE = re.compile(r"^ghcr\.io/kungfu-systems/build-images/aeron-native-kit@sha256:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
EXACT_RELEASE_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
ID = re.compile(r"^[A-Za-z0-9._-]+$")
EXTRACTION_LABEL = "io.kungfu.qualification.extraction"
CONTAINER_MARKERS = ("/docker/", "/kubepods/", "/containerd/", "libpod")
RELEASE_PASSPORT_CONTRACT = "kungfu-buildchain-release-passport"
RELEASE_CHECK_REPORT_CONTRACT = "kungfu-buildchain-release-check-report"
PUBLIC_RELEASE_RECEIPT_CONTRACT = "kungfu-build-images-aeron-native-public-release-receipt"
PUBLIC_ORIGIN_URLS = {
    "git@github.com:kungfu-systems/build-images.git",
    "https://github.com/kungfu-systems/build-images.git",
}
GITHUB_RELEASE_API = "https://api.github.com/repos/kungfu-systems/build-images/releases/tags"


class NativeQualificationError(ValueError):
    """A fail-closed native qualification contract violation."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise NativeQualificationError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise NativeQualificationError(f"JSON root must be an object: {path}")
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


def require_keys(value: dict[str, Any], required: set[str], context: str) -> None:
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        raise NativeQualificationError(f"{context} is missing fields: {', '.join(missing)}")
    if extra:
        raise NativeQualificationError(f"{context} has unsupported fields: {', '.join(extra)}")


def require_positive(value: Any, context: str, *, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise NativeQualificationError(f"{context} must be an integer >= {minimum}")
    return value


def require_sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise NativeQualificationError(f"{context} must be a lowercase SHA-256")
    return value


def safe_relative(value: Any, context: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value:
        raise NativeQualificationError(f"{context} must be a non-empty relative path")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise NativeQualificationError(f"{context} must stay inside the bundle")
    return path


def validate_contract_schemas() -> None:
    for path, expected_id in (
        (PLAN_SCHEMA_PATH, PLAN_SCHEMA_ID),
        (RUN_SCHEMA_PATH, RUN_SCHEMA_ID),
        (BUNDLE_SCHEMA_PATH, BUNDLE_SCHEMA_ID),
    ):
        schema = load_json(path)
        if schema.get("$id") != expected_id or schema.get("additionalProperties") is not False:
            raise NativeQualificationError(f"native qualification schema contract is invalid: {path}")


def validate_repetition_group(group: Any, name: str, *, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(group, dict):
        raise NativeQualificationError(f"{name} must be an object")
    require_keys(group, allowed, name)
    require_positive(group.get("repetitions"), f"{name}.repetitions", minimum=3 if name != "soak" else 1)
    return group


def validate_plan_document(plan: dict[str, Any]) -> None:
    require_keys(
        plan,
        {
            "plan_schema", "schema_version", "test_only", "evidence_class",
            "native_performance_authority_requested", "kit", "configuration",
            "host_policy", "calibration", "ipc", "receipts", "recovery", "soak",
            "advocate_review", "claim_boundary",
        },
        "native qualification plan",
    )
    if plan["plan_schema"] != PLAN_SCHEMA_ID or plan["schema_version"] != 1:
        raise NativeQualificationError("native qualification plan schema/version is unsupported")
    if plan["test_only"] is not False:
        raise NativeQualificationError("production native qualification plan must set test_only=false")
    if plan["evidence_class"] != EVIDENCE_CLASS or plan["native_performance_authority_requested"] is not True:
        raise NativeQualificationError("native qualification authority request is invalid")

    kit = plan["kit"]
    if not isinstance(kit, dict):
        raise NativeQualificationError("kit must be an object")
    require_keys(kit, {"image", "platform", "files", "source_files"}, "kit")
    if not isinstance(kit["image"], str) or not EXACT_IMAGE.fullmatch(kit["image"]):
        raise NativeQualificationError("kit.image must be the exact build-images Aeron kit digest")
    if kit["image"].endswith("0" * 64):
        raise NativeQualificationError("kit.image still contains the first-publish placeholder digest")
    if kit["platform"] != "linux/amd64":
        raise NativeQualificationError("kit.platform must be linux/amd64")
    for field in ("files", "source_files"):
        records = kit[field]
        if not isinstance(records, dict) or not records:
            raise NativeQualificationError(f"kit.{field} must be a non-empty digest map")
        for relative, digest in records.items():
            safe_relative(relative, f"kit.{field} path")
            require_sha256(digest, f"kit.{field}[{relative}]")

    configuration = plan["configuration"]
    if not isinstance(configuration, dict):
        raise NativeQualificationError("configuration must be an object")
    require_keys(
        configuration,
        {
            "aeron_version", "java_vendor", "java_version", "ipc_channel",
            "archive_control_channel", "archive_control_response_channel",
            "archive_replication_channel", "driver_threading", "archive_threading",
            "idle_strategy", "term_buffer_length", "segment_file_length", "sparse",
            "file_sync_level", "catalog_sync_level", "spies_simulate_connection",
            "receipt_timeout_seconds", "coordinated_omission", "ipc_poll_batch",
        },
        "configuration",
    )
    expected_configuration = {
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
    }
    if configuration != expected_configuration:
        raise NativeQualificationError("configuration does not match the fixed harness contract")

    host_policy = plan["host_policy"]
    if not isinstance(host_policy, dict):
        raise NativeQualificationError("host_policy must be an object")
    require_keys(
        host_policy,
        {"sudo_allowed", "host_tuning_allowed", "perf_counters", "required_facts", "interference_preflight"},
        "host_policy",
    )
    if host_policy["sudo_allowed"] is not False or host_policy["host_tuning_allowed"] is not False:
        raise NativeQualificationError("native runner must not require sudo or host tuning")
    if host_policy["perf_counters"] != "optional-record-availability":
        raise NativeQualificationError("perf counters must remain optional")
    required_facts = host_policy["required_facts"]
    expected_facts = {"cpu_topology", "governor", "energy_policy", "affinity", "load", "memory", "filesystem", "cgroup", "perf_event_paranoid"}
    if not isinstance(required_facts, list) or set(required_facts) != expected_facts:
        raise NativeQualificationError("host_policy.required_facts is incomplete")
    if host_policy["interference_preflight"] != "record-load-no-mutation":
        raise NativeQualificationError("host interference policy is unsupported")

    calibration = validate_repetition_group(
        plan["calibration"], "calibration",
        allowed={"repetitions", "warmup", "messages", "payload", "rate", "max_p99_drift_ratio"},
    )
    for field in ("warmup", "messages", "payload", "rate"):
        require_positive(calibration.get(field), f"calibration.{field}")
    drift = calibration.get("max_p99_drift_ratio")
    if not isinstance(drift, (int, float)) or isinstance(drift, bool) or drift <= 0:
        raise NativeQualificationError("calibration.max_p99_drift_ratio must be positive")

    ipc = validate_repetition_group(
        plan["ipc"], "ipc", allowed={"repetitions", "warmup", "messages", "payloads", "rates"}
    )
    require_positive(ipc.get("warmup"), "ipc.warmup")
    require_positive(ipc.get("messages"), "ipc.messages")
    for field in ("payloads", "rates"):
        values = ipc.get(field)
        if not isinstance(values, list) or not values or len(values) != len(set(values)):
            raise NativeQualificationError(f"ipc.{field} must be a unique non-empty list")
        for value in values:
            require_positive(value, f"ipc.{field} value")

    receipts = validate_repetition_group(
        plan["receipts"], "receipts", allowed={"repetitions", "messages", "payload", "modes"}
    )
    require_positive(receipts.get("messages"), "receipts.messages")
    require_positive(receipts.get("payload"), "receipts.payload", minimum=80)
    if receipts.get("modes") != ["visible", "durable_group", "durable_sync"]:
        raise NativeQualificationError("receipt modes and order must preserve the reviewed mapping")

    recovery = validate_repetition_group(
        plan["recovery"], "recovery",
        allowed={"repetitions", "messages", "payload", "receipt", "expiry_wait_seconds", "modes"},
    )
    require_positive(recovery.get("messages"), "recovery.messages")
    require_positive(recovery.get("payload"), "recovery.payload", minimum=80)
    if recovery.get("receipt") != "durable_group" or recovery.get("expiry_wait_seconds") != 11:
        raise NativeQualificationError("recovery must use durable_group and the reviewed 11-second expiry wait")
    if recovery.get("modes") != ["crash-replay", "whole-root-restore"]:
        raise NativeQualificationError("recovery mode set is incomplete")

    soak = validate_repetition_group(
        plan["soak"], "soak", allowed={"repetitions", "warmup", "messages", "payload", "rate"}
    )
    for field in ("warmup", "messages", "payload", "rate"):
        require_positive(soak.get(field), f"soak.{field}")

    review = plan["advocate_review"]
    if not isinstance(review, dict):
        raise NativeQualificationError("advocate_review must be an object")
    require_keys(review, {"reviewer", "status", "scope"}, "advocate_review")
    if review["reviewer"] != "kungfu-origin" or review["status"] != "approved":
        raise NativeQualificationError("native plan requires named kungfu-origin advocate approval")
    required_scope = {
        "version", "channels", "threading", "idle", "sync", "payload",
        "poll_batch", "topology", "receipt_mapping", "exclusions",
    }
    if not isinstance(review["scope"], list) or set(review["scope"]) != required_scope:
        raise NativeQualificationError("advocate review scope is incomplete")

    boundary = plan["claim_boundary"]
    expected_boundary = {
        "native_performance_authority_requested": True,
        "containerized_user_outcome_authority": False,
        "fresh_install_cost_authority": False,
        "final_scoring_authority": False,
    }
    if boundary != expected_boundary:
        raise NativeQualificationError("native plan claim boundary is invalid")


def validate_plan(plan_path: pathlib.Path) -> dict[str, Any]:
    validate_contract_schemas()
    plan = load_json(plan_path)
    validate_plan_document(plan)
    return plan


def run_command(
    command: list[str], *, timeout: int = 30, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as error:
        if check:
            raise NativeQualificationError(f"command timed out after {timeout}s: {command[0]}") from error
        stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
        process = subprocess.CompletedProcess(
            command, 124, stdout, stderr + f"\ncommand timed out after {timeout}s\n"
        )
    if check and process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise NativeQualificationError(f"command failed ({process.returncode}): {' '.join(command)}: {detail}")
    return process


def git_head() -> str:
    process = run_command(["git", "rev-parse", "HEAD"], timeout=10)
    value = process.stdout.strip()
    if not GIT_SHA.fullmatch(value):
        raise NativeQualificationError("cannot resolve the build-images source SHA")
    return value


def require_clean_source() -> None:
    process = run_command(["git", "status", "--porcelain"], timeout=10)
    if process.stdout.strip():
        raise NativeQualificationError("native qualification requires a clean exact-tag checkout")


def exact_release_tag(source_sha: str) -> str:
    process = run_command(["git", "tag", "--points-at", source_sha], timeout=10)
    tags = sorted(
        tag for tag in process.stdout.splitlines()
        if EXACT_RELEASE_TAG.fullmatch(tag.strip())
    )
    if len(tags) != 1:
        raise NativeQualificationError("native qualification requires exactly one public release tag at HEAD")
    tag = tags[0]
    resolved = run_command(["git", "rev-parse", f"refs/tags/{tag}^{{}}"], timeout=10).stdout.strip()
    if resolved != source_sha:
        raise NativeQualificationError("public release tag does not resolve to the checked-out material SHA")
    return tag


def require_tagged_runner(source_sha: str) -> None:
    tagged_blob = run_command(
        ["git", "rev-parse", f"{source_sha}:{RUNNER_REPO_PATH}"], timeout=10
    ).stdout.strip()
    worktree_blob = run_command(
        ["git", "hash-object", str(SCRIPT_PATH)], timeout=10
    ).stdout.strip()
    if (
        not GIT_SHA.fullmatch(tagged_blob)
        or not GIT_SHA.fullmatch(worktree_blob)
        or tagged_blob != worktree_blob
    ):
        raise NativeQualificationError(
            "native qualification runner bytes do not match the exact release tag"
        )


def validate_release_authority(
    passport_path: pathlib.Path,
    check_report_path: pathlib.Path,
    source_sha: str,
    release_tag: str,
) -> dict[str, Any]:
    if (
        passport_path.name != "buildchain.release.json"
        or check_report_path.name != "check-report.json"
    ):
        raise NativeQualificationError("release authority inputs must keep their public asset names")
    passport = load_json(passport_path)
    check_report = load_json(check_report_path)
    release = passport.get("release")
    transaction = passport.get("transaction")
    evidence = passport.get("evidence")
    validation = transaction.get("result", {}).get("validation", {}) if isinstance(transaction, dict) else {}
    if (
        passport.get("contract") != RELEASE_PASSPORT_CONTRACT
        or not isinstance(release, dict)
        or not isinstance(transaction, dict)
        or not isinstance(evidence, dict)
        or release.get("tag") != release_tag
        or release.get("publicTag") != release_tag
        or release.get("exactRef") != f"refs/tags/{release_tag}"
        or release.get("releaseSha") != source_sha
        or release.get("releaseMaterialSha") != source_sha
        or not GIT_SHA.fullmatch(str(release.get("sourceSha", "")))
        or transaction.get("state") != "complete"
        or transaction.get("exactTag") != release_tag
        or transaction.get("releaseSha") != source_sha
        or transaction.get("releaseMaterialSha") != source_sha
        or validation.get("valid") is not True
        or validation.get("errors") != []
        or evidence.get("checkReport") != check_report_path.name
    ):
        raise NativeQualificationError("release passport does not bind the exact reviewed material")
    if (
        check_report.get("contract") != RELEASE_CHECK_REPORT_CONTRACT
        or check_report.get("ok") is not True
        or check_report.get("trust") != "pass"
        or check_report.get("issues") != []
    ):
        raise NativeQualificationError("release check report does not grant trust")
    return {
        "tag": release_tag,
        "material_sha": source_sha,
        "source_sha": release["sourceSha"],
    }


def public_release_evidence(
    passport_path: pathlib.Path,
    check_report_path: pathlib.Path,
    source_sha: str,
    release_tag: str,
) -> dict[str, str]:
    origin = run_command(["git", "remote", "get-url", "origin"], timeout=10).stdout.strip()
    if origin not in PUBLIC_ORIGIN_URLS:
        raise NativeQualificationError("native qualification origin is not the public build-images repository")
    remote = run_command(
        [
            "git", "ls-remote", "--tags", "origin",
            f"refs/tags/{release_tag}", f"refs/tags/{release_tag}^{{}}",
        ],
        timeout=30,
    )
    refs = {}
    for line in remote.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and GIT_SHA.fullmatch(fields[0]):
            refs[fields[1]] = fields[0]
    remote_sha = refs.get(f"refs/tags/{release_tag}^{{}}", refs.get(f"refs/tags/{release_tag}"))
    if remote_sha != source_sha:
        raise NativeQualificationError("public release tag does not match the checked-out material SHA")

    api_url = f"{GITHUB_RELEASE_API}/{release_tag}"
    request = urllib.request.Request(
        api_url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "build-images-aeron-qualification"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise NativeQualificationError(f"cannot verify public GitHub release assets: {error}") from error
    if (
        not isinstance(release, dict)
        or release.get("tag_name") != release_tag
        or release.get("draft") is not False
        or not isinstance(release.get("published_at"), str)
    ):
        raise NativeQualificationError("GitHub release metadata does not match the exact release tag")
    release_assets = release.get("assets")
    if not isinstance(release_assets, list):
        raise NativeQualificationError("GitHub release metadata has no public asset set")
    assets = {
        asset.get("name"): asset.get("digest")
        for asset in release_assets
        if isinstance(asset, dict)
    }
    expected_passport = f"sha256:{sha256_file(passport_path)}"
    expected_check_report = f"sha256:{sha256_file(check_report_path)}"
    if (
        assets.get(passport_path.name) != expected_passport
        or assets.get(check_report_path.name) != expected_check_report
    ):
        raise NativeQualificationError("public GitHub release asset digest does not match the authority input")
    return {
        "origin": origin,
        "tag_ref": f"refs/tags/{release_tag}",
        "api_url": api_url,
        "passport_sha256": expected_passport.removeprefix("sha256:"),
        "check_report_sha256": expected_check_report.removeprefix("sha256:"),
    }


def require_exact_release_source(
    passport_path: pathlib.Path,
    check_report_path: pathlib.Path,
) -> tuple[str, str, dict[str, Any]]:
    require_clean_source()
    source_sha = git_head()
    release_tag = exact_release_tag(source_sha)
    require_tagged_runner(source_sha)
    authority = validate_release_authority(
        passport_path, check_report_path, source_sha, release_tag
    )
    authority.update(public_release_evidence(
        passport_path, check_report_path, source_sha, release_tag
    ))
    return source_sha, release_tag, authority


def copy_authority_inputs(
    bundle_dir: pathlib.Path,
    passport_path: pathlib.Path,
    check_report_path: pathlib.Path,
    release_authority: dict[str, Any],
) -> dict[str, dict[str, str]]:
    authority_dir = bundle_dir / "authority"
    runner_target = authority_dir / "scripts" / SCRIPT_PATH.name
    passport_target = authority_dir / "buildchain.release.json"
    check_report_target = authority_dir / "check-report.json"
    receipt_target = authority_dir / "public-release.json"
    runner_target.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT_PATH, runner_target)
    shutil.copy2(passport_path, passport_target)
    shutil.copy2(check_report_path, check_report_target)
    if (
        sha256_file(passport_target) != release_authority["passport_sha256"]
        or sha256_file(check_report_target) != release_authority["check_report_sha256"]
    ):
        raise NativeQualificationError("release authority input changed before bundle retention")
    write_json(receipt_target, {
        "contract": PUBLIC_RELEASE_RECEIPT_CONTRACT,
        "schema_version": 1,
        **release_authority,
    })
    return {
        "runner": {
            "path": runner_target.relative_to(bundle_dir).as_posix(),
            "sha256": sha256_file(runner_target),
        },
        "release_passport": {
            "path": passport_target.relative_to(bundle_dir).as_posix(),
            "sha256": sha256_file(passport_target),
        },
        "release_check_report": {
            "path": check_report_target.relative_to(bundle_dir).as_posix(),
            "sha256": sha256_file(check_report_target),
        },
        "public_release_receipt": {
            "path": receipt_target.relative_to(bundle_dir).as_posix(),
            "sha256": sha256_file(receipt_target),
        },
    }


def artifact_records(root: pathlib.Path, *, exclude: set[pathlib.Path] | None = None) -> list[dict[str, Any]]:
    excluded = {path.resolve() for path in (exclude or set())}
    records = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.resolve() in excluded:
            continue
        records.append({
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return records


def extraction_containers() -> list[str]:
    process = run_command(
        ["docker", "ps", "-aq", "--filter", f"label={EXTRACTION_LABEL}"], timeout=15
    )
    return [line for line in process.stdout.splitlines() if line]


def container_guard() -> dict[str, Any]:
    cgroup_path = pathlib.Path("/proc/self/cgroup")
    cgroup = cgroup_path.read_text(encoding="utf-8", errors="replace") if cgroup_path.is_file() else "unavailable"
    measured_in_container = any(marker in cgroup.lower() for marker in CONTAINER_MARKERS)
    extraction = extraction_containers()
    guard = {
        "measured_in_container": measured_in_container,
        "extraction_containers": extraction,
        "passed": not measured_in_container and not extraction,
    }
    if not guard["passed"]:
        raise NativeQualificationError("native measurement path is contaminated by a container")
    return guard


def prepare_kit(plan: dict[str, Any], bundle_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, dict[str, Any]]:
    preparation_dir = bundle_dir / "preparation"
    preparation_dir.mkdir(parents=True)
    image = plan["kit"]["image"]
    inspect = run_command(["docker", "image", "inspect", image], timeout=30, check=False)
    pulled = False
    if inspect.returncode != 0:
        run_command(["docker", "pull", "--platform", "linux/amd64", image], timeout=900)
        pulled = True
    inspect = run_command(["docker", "image", "inspect", image], timeout=30)
    image_records = json.loads(inspect.stdout)
    if not isinstance(image_records, list) or len(image_records) != 1:
        raise NativeQualificationError("exact kit image inspect result is invalid")
    image_record = image_records[0]
    if image not in image_record.get("RepoDigests", []):
        raise NativeQualificationError("local image identity does not contain the requested exact digest")

    if extraction_containers():
        raise NativeQualificationError("a previous Aeron extraction container still exists")
    container_name = f"kf-aeron-extract-{os.getpid()}-{int(time.time())}"
    container_id = ""
    try:
        created = run_command([
            "docker", "create", "--name", container_name,
            "--label", f"{EXTRACTION_LABEL}=true", image, "/bin/true",
        ], timeout=30)
        container_id = created.stdout.strip()
        run_command(["docker", "cp", f"{container_id}:/opt/aeron-native-kit", str(preparation_dir / "kit")], timeout=120)
        run_command(["docker", "cp", f"{container_id}:/opt/java/openjdk", str(preparation_dir / "jre")], timeout=120)
    finally:
        if container_id:
            run_command(["docker", "rm", "-f", container_id], timeout=30, check=False)
    if extraction_containers():
        raise NativeQualificationError("Aeron extraction container cleanup is incomplete")

    for relative, expected in plan["kit"]["files"].items():
        path = preparation_dir / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        if not path.is_file() or sha256_file(path) != expected:
            raise NativeQualificationError(f"extracted kit file digest mismatch: {relative}")
    for relative, expected in plan["kit"]["source_files"].items():
        path = REPO_ROOT / pathlib.Path(*pathlib.PurePosixPath(relative).parts)
        if not path.is_file() or sha256_file(path) != expected:
            raise NativeQualificationError(f"kit source file digest mismatch: {relative}")

    launcher = preparation_dir / "kit" / "bin" / "aeron-native-harness"
    java = preparation_dir / "jre" / "bin" / "java"
    if not launcher.is_file() or not java.is_file() or not os.access(launcher, os.X_OK) or not os.access(java, os.X_OK):
        raise NativeQualificationError("extracted kit launcher or JRE is not executable")
    preparation = {
        "schema": "urn:kungfu-systems:build-images:aeron-native-kit-preparation:v1",
        "image": image,
        "platform": plan["kit"]["platform"],
        "image_id": image_record.get("Id"),
        "repo_digests": image_record.get("RepoDigests", []),
        "pulled": pulled,
        "extraction_container_removed": True,
        "measurement_runtime": "host-process",
        "files": artifact_records(preparation_dir),
    }
    preparation_path = preparation_dir / "preparation.json"
    write_json(preparation_path, preparation)
    return launcher, java, preparation


def read_optional(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def host_facts(bundle_dir: pathlib.Path) -> dict[str, Any]:
    facts_dir = bundle_dir / "host"
    facts_dir.mkdir(parents=True)
    command_outputs: dict[str, dict[str, Any]] = {}
    for name, command in (
        ("lscpu", ["lscpu", "-J"]),
        ("load", ["uptime"]),
        ("memory", ["cat", "/proc/meminfo"]),
        ("mounts", ["cat", "/proc/mounts"]),
        ("affinity", ["taskset", "-pc", str(os.getpid())]),
    ):
        process = run_command(command, timeout=15, check=False)
        output_path = facts_dir / f"{name}.stdout.txt"
        error_path = facts_dir / f"{name}.stderr.txt"
        output_path.write_text(process.stdout, encoding="utf-8")
        error_path.write_text(process.stderr, encoding="utf-8")
        command_outputs[name] = {
            "argv": command,
            "exit_code": process.returncode,
            "stdout": output_path.relative_to(bundle_dir).as_posix(),
            "stderr": error_path.relative_to(bundle_dir).as_posix(),
        }
    governors = sorted({
        value for path in pathlib.Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor")
        if (value := read_optional(path))
    })
    energy_policies = sorted({
        value for path in pathlib.Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/energy_performance_preference")
        if (value := read_optional(path))
    })
    cgroup = read_optional(pathlib.Path("/proc/self/cgroup")) or "unavailable"
    if any(marker in cgroup.lower() for marker in CONTAINER_MARKERS):
        raise NativeQualificationError("native runner itself is executing inside a container")
    statvfs = os.statvfs(bundle_dir)
    facts = {
        "schema": "urn:kungfu-systems:build-images:aeron-native-host-facts:v1",
        "platform": platform.platform(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "cpu_count": os.cpu_count(),
        "governors": governors,
        "energy_policies": energy_policies,
        "perf_event_paranoid": read_optional(pathlib.Path("/proc/sys/kernel/perf_event_paranoid")),
        "cgroup": cgroup,
        "filesystem": {
            "path": str(bundle_dir),
            "block_size": statvfs.f_frsize,
            "blocks_available": statvfs.f_bavail,
        },
        "commands": command_outputs,
        "perf_counters_available": False,
        "perf_counters_required": False,
        "host_state_mutated": False,
    }
    facts_path = facts_dir / "host-facts.json"
    write_json(facts_path, facts)
    return facts


def usage_snapshot() -> dict[str, float | int]:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "user_seconds": usage.ru_utime,
        "system_seconds": usage.ru_stime,
        "max_rss_kib": usage.ru_maxrss,
        "minor_faults": usage.ru_minflt,
        "major_faults": usage.ru_majflt,
        "input_blocks": usage.ru_inblock,
        "output_blocks": usage.ru_oublock,
        "voluntary_context_switches": usage.ru_nvcsw,
        "involuntary_context_switches": usage.ru_nivcsw,
    }


def usage_delta(before: dict[str, float | int], after: dict[str, float | int]) -> dict[str, float | int]:
    return {
        key: after[key] if key == "max_rss_kib" else after[key] - before[key]
        for key in before
    }


def harness_environment(java: pathlib.Path) -> dict[str, str]:
    return {**os.environ, "AERON_NATIVE_KIT_JAVA": str(java)}


def harness_json(
    launcher: pathlib.Path,
    java: pathlib.Path,
    arguments: list[str],
    output_dir: pathlib.Path,
    name: str,
    *,
    timeout: int = 60,
) -> tuple[dict[str, Any], dict[str, float | int]]:
    guard = container_guard()
    command = [str(launcher), *arguments]
    before = usage_snapshot()
    started = time.monotonic()
    process = run_command(command, timeout=timeout, check=False, env=harness_environment(java))
    finished = time.monotonic()
    after = usage_snapshot()
    (output_dir / f"{name}.stdout.log").write_text(process.stdout, encoding="utf-8")
    (output_dir / f"{name}.stderr.log").write_text(process.stderr, encoding="utf-8")
    write_json(output_dir / f"{name}.command.json", {
        "argv": command,
        "exit_code": process.returncode,
        "timeout_seconds": timeout,
        "duration_seconds": round(finished - started, 6),
        "container_guard": guard,
    })
    if process.returncode != 0:
        raise NativeQualificationError(f"Aeron harness command failed ({process.returncode}): {arguments[0]}")
    lines = [line for line in process.stdout.splitlines() if line.strip()]
    if not lines:
        raise NativeQualificationError(f"Aeron harness emitted no JSON: {arguments[0]}")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise NativeQualificationError(f"Aeron harness emitted invalid JSON: {arguments[0]}") from error
    if not isinstance(payload, dict):
        raise NativeQualificationError("Aeron harness JSON must be an object")
    return payload, usage_delta(before, after)


def start_server(
    launcher: pathlib.Path, java: pathlib.Path, root: pathlib.Path, output_dir: pathlib.Path, suffix: str
) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    container_guard()
    root.mkdir(parents=True, exist_ok=True)
    stdout = (output_dir / f"server-{suffix}.stdout.log").open("w", encoding="utf-8")
    stderr = (output_dir / f"server-{suffix}.stderr.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            str(launcher), "server", "--root", str(root),
            "--file-sync-level", "1", "--catalog-sync-level", "1",
        ],
        stdout=stdout,
        stderr=stderr,
        text=True,
        env=harness_environment(java),
        start_new_session=True,
    )
    deadline = time.monotonic() + 30
    health: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout.close()
            stderr.close()
            raise NativeQualificationError(f"Aeron server exited before live health: {process.returncode}")
        checked = run_command(
            [str(launcher), "health", "--root", str(root)],
            timeout=5,
            check=False,
            env=harness_environment(java),
        )
        if checked.returncode == 0:
            try:
                health = json.loads(checked.stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError) as error:
                raise NativeQualificationError("Aeron health response is invalid") from error
            break
        time.sleep(0.25)
    if not isinstance(health, dict) or health.get("status") != "live":
        stop_server(process, crashed=False)
        stdout.close()
        stderr.close()
        raise NativeQualificationError("Aeron server did not establish live Driver/Archive health")
    write_json(output_dir / f"server-{suffix}.health.json", health)
    setattr(process, "_qualification_streams", (stdout, stderr))
    return process, health


def stop_server(process: subprocess.Popen[str], *, crashed: bool) -> int:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL if crashed else signal.SIGTERM)
    try:
        return_code = process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        return_code = process.wait(timeout=5)
    streams = getattr(process, "_qualification_streams", ())
    for stream in streams:
        stream.close()
    if crashed:
        if return_code not in (-signal.SIGKILL, 137):
            raise NativeQualificationError(f"crash server exit code is not SIGKILL: {return_code}")
    elif return_code not in (0, -signal.SIGTERM, 143):
        raise NativeQualificationError(f"server cleanup failed: {return_code}")
    return return_code


def result_marker(plan_sha256: str, tier: str, repetition: int, variant: str) -> str:
    return sha256_json({
        "plan_sha256": plan_sha256,
        "tier": tier,
        "repetition": repetition,
        "variant": variant,
    })


def ipc_seed(marker: str) -> int:
    require_sha256(marker, "native IPC marker")
    return int(marker[:15], 16)


def finalize_result(
    output_dir: pathlib.Path,
    tier: str,
    repetition: int,
    marker: str,
    measurement: dict[str, Any],
    metrics: dict[str, float | int],
    started_at: str,
) -> pathlib.Path:
    guard = container_guard()
    result_path = output_dir / "result.json"
    result = {
        "run_schema": RUN_SCHEMA_ID,
        "schema_version": 1,
        "status": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "native_performance_authority": False,
        "tier": tier,
        "repetition": repetition,
        "started_at": started_at,
        "finished_at": utc_now(),
        "marker": marker,
        "measurement": measurement,
        "process_metrics": metrics,
        "raw_artifacts": artifact_records(output_dir, exclude={result_path}),
        "container_guard": guard,
    }
    write_json(result_path, result)
    return result_path


def run_ipc_result(
    launcher: pathlib.Path,
    java: pathlib.Path,
    plan_sha256: str,
    tier: str,
    repetition: int,
    variant: str,
    parameters: dict[str, int],
    output_dir: pathlib.Path,
) -> pathlib.Path:
    started_at = utc_now()
    marker = result_marker(plan_sha256, tier, repetition, variant)
    seed = ipc_seed(marker)
    output_dir.mkdir(parents=True)
    histogram = output_dir / "latency.hlog"
    measurement, metrics = harness_json(
        launcher,
        java,
        [
            "ipc", "--root", str(output_dir / "ipc-root"),
            "--warmup", str(parameters["warmup"]),
            "--messages", str(parameters["messages"]),
            "--payload", str(parameters["payload"]),
            "--rate", str(parameters["rate"]),
            "--seed", str(seed),
            "--poll-batch", str(parameters["poll_batch"]),
            "--histogram", str(histogram),
        ],
        output_dir,
        "ipc",
        timeout=300,
    )
    if measurement.get("coordinated_omission") != "expected-interval-correction":
        raise NativeQualificationError("IPC result does not preserve coordinated-omission treatment")
    if measurement.get("messages") != parameters["messages"] or measurement.get("samples", 0) < parameters["messages"]:
        raise NativeQualificationError("IPC sample count is incomplete")
    if measurement.get("seed") != seed:
        raise NativeQualificationError("IPC result seed does not match the result marker")
    if measurement.get("poll_batch") != parameters["poll_batch"]:
        raise NativeQualificationError("IPC result poll batch does not match the fixed harness contract")
    if not histogram.is_file() or histogram.stat().st_size == 0:
        raise NativeQualificationError("IPC raw HdrHistogram is missing")
    measurement.update({"variant": variant, "marker_binding": marker})
    return finalize_result(output_dir, tier, repetition, marker, measurement, metrics, started_at)


def run_receipt_result(
    launcher: pathlib.Path,
    java: pathlib.Path,
    plan_sha256: str,
    repetition: int,
    mode: str,
    parameters: dict[str, Any],
    output_dir: pathlib.Path,
) -> pathlib.Path:
    started_at = utc_now()
    marker = result_marker(plan_sha256, "receipts", repetition, mode)
    output_dir.mkdir(parents=True)
    root = output_dir / "archive-root"
    server, health = start_server(launcher, java, root, output_dir, "initial")
    metrics_before = usage_snapshot()
    try:
        record, _ = harness_json(
            launcher,
            java,
            [
                "record", "--root", str(root), "--count", str(parameters["messages"]),
                "--payload", str(parameters["payload"]), "--receipt", mode,
                "--marker", marker,
            ],
            output_dir,
            "record",
            timeout=120,
        )
        replay, _ = harness_json(
            launcher,
            java,
            [
                "replay", "--root", str(root), "--count", str(parameters["messages"]),
                "--recording-id", str(record["recording_id"]),
                "--length", str(record["final_position"]), "--marker", marker,
            ],
            output_dir,
            "replay",
            timeout=120,
        )
    finally:
        stop_server(server, crashed=False)
    metrics = usage_delta(metrics_before, usage_snapshot())
    if (
        record.get("receipt") != mode
        or record.get("observed") != parameters["messages"]
        or any(record.get(field) != 0 for field in ("duplicates", "reordered", "marker_mismatches"))
        or replay.get("observed") != parameters["messages"]
        or replay.get("marker") != marker
    ):
        raise NativeQualificationError("receipt/replay oracle failed")
    measurement = {"mode": mode, "health": health, "record": record, "replay": replay}
    return finalize_result(output_dir, "receipts", repetition, marker, measurement, metrics, started_at)


def archive_inventory(root: pathlib.Path) -> list[dict[str, Any]]:
    return artifact_records(root)


def run_recovery_result(
    launcher: pathlib.Path,
    java: pathlib.Path,
    plan_sha256: str,
    repetition: int,
    mode: str,
    parameters: dict[str, Any],
    output_dir: pathlib.Path,
) -> pathlib.Path:
    started_at = utc_now()
    marker = result_marker(plan_sha256, "recovery", repetition, mode)
    output_dir.mkdir(parents=True)
    root = output_dir / "archive-root"
    metrics_before = usage_snapshot()
    server, initial_health = start_server(launcher, java, root, output_dir, "initial")
    record, _ = harness_json(
        launcher,
        java,
        [
            "record", "--root", str(root), "--count", str(parameters["messages"]),
            "--payload", str(parameters["payload"]), "--receipt", parameters["receipt"],
            "--marker", marker,
        ],
        output_dir,
        "record",
        timeout=120,
    )
    stale_health_rejected = False
    backup_sha256 = None
    if mode == "crash-replay":
        stop_server(server, crashed=True)
        stale = run_command(
            [str(launcher), "health", "--root", str(root)],
            timeout=5,
            check=False,
            env=harness_environment(java),
        )
        stale_health_rejected = stale.returncode != 0
        if not stale_health_rejected:
            raise NativeQualificationError("stale Driver/Archive files passed live health")
    elif mode == "whole-root-restore":
        stop_server(server, crashed=False)
        backup = output_dir / "whole-root-backup"
        shutil.copytree(root, backup)
        backup_records = archive_inventory(backup)
        write_json(output_dir / "whole-root-backup.inventory.json", {"files": backup_records})
        backup_sha256 = sha256_json(backup_records)
        shutil.rmtree(root)
        shutil.copytree(backup, root)
        stale_health_rejected = True
    else:
        stop_server(server, crashed=False)
        raise NativeQualificationError(f"unsupported recovery mode: {mode}")

    time.sleep(parameters["expiry_wait_seconds"])
    restarted, restart_health = start_server(launcher, java, root, output_dir, "restart")
    try:
        replay, _ = harness_json(
            launcher,
            java,
            [
                "replay", "--root", str(root), "--count", str(parameters["messages"]),
                "--recording-id", str(record["recording_id"]),
                "--length", str(record["final_position"]), "--marker", marker,
            ],
            output_dir,
            "replay",
            timeout=120,
        )
    finally:
        stop_server(restarted, crashed=False)
    metrics = usage_delta(metrics_before, usage_snapshot())
    if (
        replay.get("observed") != parameters["messages"]
        or replay.get("duplicates") != 0
        or replay.get("reordered") != 0
        or replay.get("marker_mismatches") != 0
        or replay.get("marker") != marker
    ):
        raise NativeQualificationError("recovery replay oracle failed")
    write_json(output_dir / "archive-root.inventory.json", {"files": archive_inventory(root)})
    measurement = {
        "mode": mode,
        "initial_health": initial_health,
        "restart_health": restart_health,
        "stale_health_rejected": stale_health_rejected,
        "expiry_wait_seconds": parameters["expiry_wait_seconds"],
        "backup_inventory_sha256": backup_sha256,
        "record": record,
        "replay": replay,
    }
    return finalize_result(output_dir, "recovery", repetition, marker, measurement, metrics, started_at)


def expected_result_count(plan: dict[str, Any]) -> int:
    return (
        plan["calibration"]["repetitions"]
        + plan["ipc"]["repetitions"] * len(plan["ipc"]["payloads"]) * len(plan["ipc"]["rates"])
        + plan["receipts"]["repetitions"] * len(plan["receipts"]["modes"])
        + plan["recovery"]["repetitions"] * len(plan["recovery"]["modes"])
        + plan["soak"]["repetitions"]
    )


def copy_contracts(bundle_dir: pathlib.Path) -> list[dict[str, Any]]:
    contracts_dir = bundle_dir / "contracts"
    contracts_dir.mkdir()
    records = []
    for source in (PLAN_SCHEMA_PATH, RUN_SCHEMA_PATH, BUNDLE_SCHEMA_PATH):
        target = contracts_dir / source.name
        shutil.copy2(source, target)
        records.append({"path": target.relative_to(bundle_dir).as_posix(), "sha256": sha256_file(target)})
    return records


def verify_calibration(plan: dict[str, Any], results: list[dict[str, Any]]) -> None:
    p99_values = [
        result["measurement"].get("p99_ns")
        for result in results
        if result.get("tier") == "calibration"
    ]
    if len(p99_values) != plan["calibration"]["repetitions"] or any(
        not isinstance(value, int) or value <= 0 for value in p99_values
    ):
        raise NativeQualificationError("calibration result set is incomplete")
    median = statistics.median(p99_values)
    drift = max(abs(value - median) / median for value in p99_values)
    if drift > plan["calibration"]["max_p99_drift_ratio"]:
        raise NativeQualificationError("calibration drift exceeds the frozen threshold")


def run_plan(
    plan_path: pathlib.Path,
    release_passport_path: pathlib.Path,
    release_check_report_path: pathlib.Path,
) -> pathlib.Path:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise NativeQualificationError("native qualification requires a Linux x86_64 host")
    plan = validate_plan(plan_path)
    source_sha, release_tag, release_authority = require_exact_release_source(
        release_passport_path, release_check_report_path
    )
    plan_sha = sha256_file(plan_path)
    bundle_id = f"aeron-native-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{source_sha[:12]}"
    bundle_dir = ARTIFACT_ROOT / bundle_id
    bundle_dir.mkdir(parents=True)
    copied_plan = bundle_dir / "inputs" / plan_path.name
    copied_plan.parent.mkdir()
    shutil.copy2(plan_path, copied_plan)
    contracts = copy_contracts(bundle_dir)
    authority_inputs = copy_authority_inputs(
        bundle_dir, release_passport_path, release_check_report_path, release_authority
    )
    launcher, java, preparation = prepare_kit(plan, bundle_dir)
    facts = host_facts(bundle_dir)
    results_dir = bundle_dir / "results"
    result_paths: list[pathlib.Path] = []

    calibration = plan["calibration"]
    for repetition in range(1, calibration["repetitions"] + 1):
        result_paths.append(run_ipc_result(
            launcher, java, plan_sha, "calibration", repetition, "canary",
            {
                "warmup": calibration["warmup"], "messages": calibration["messages"],
                "payload": calibration["payload"], "rate": calibration["rate"],
                "poll_batch": plan["configuration"]["ipc_poll_batch"],
            },
            results_dir / "calibration" / f"r{repetition:03d}",
        ))
    ipc = plan["ipc"]
    for payload in ipc["payloads"]:
        for rate in ipc["rates"]:
            variant = f"p{payload}-r{rate}"
            for repetition in range(1, ipc["repetitions"] + 1):
                result_paths.append(run_ipc_result(
                    launcher, java, plan_sha, "ipc", repetition, variant,
                    {
                        "warmup": ipc["warmup"], "messages": ipc["messages"],
                        "payload": payload, "rate": rate,
                        "poll_batch": plan["configuration"]["ipc_poll_batch"],
                    },
                    results_dir / "ipc" / variant / f"r{repetition:03d}",
                ))
    receipts = plan["receipts"]
    for mode in receipts["modes"]:
        for repetition in range(1, receipts["repetitions"] + 1):
            result_paths.append(run_receipt_result(
                launcher, java, plan_sha, repetition, mode, receipts,
                results_dir / "receipts" / mode / f"r{repetition:03d}",
            ))
    recovery = plan["recovery"]
    for mode in recovery["modes"]:
        for repetition in range(1, recovery["repetitions"] + 1):
            result_paths.append(run_recovery_result(
                launcher, java, plan_sha, repetition, mode, recovery,
                results_dir / "recovery" / mode / f"r{repetition:03d}",
            ))
    soak = plan["soak"]
    for repetition in range(1, soak["repetitions"] + 1):
        result_paths.append(run_ipc_result(
            launcher, java, plan_sha, "soak", repetition, "bounded-soak",
            {**soak, "poll_batch": plan["configuration"]["ipc_poll_batch"]},
            results_dir / "soak" / f"r{repetition:03d}",
        ))

    result_records = [
        {
            "path": path.relative_to(bundle_dir).as_posix(),
            "sha256": sha256_file(path),
            "tier": load_json(path)["tier"],
            "repetition": load_json(path)["repetition"],
            "status": load_json(path)["status"],
        }
        for path in result_paths
    ]
    expected = expected_result_count(plan)
    if len(result_records) != expected:
        raise NativeQualificationError("native run result count is incomplete")
    verify_calibration(plan, [load_json(path) for path in result_paths])
    bundle_path = bundle_dir / "bundle-manifest.json"
    bundle = {
        "bundle_schema": BUNDLE_SCHEMA_ID,
        "schema_version": 1,
        "status": "complete",
        "evidence_class": EVIDENCE_CLASS,
        "native_performance_authority": True,
        "containerized_user_outcome_authority": False,
        "fresh_install_cost_authority": False,
        "final_scoring_authority": False,
        "bundle_id": bundle_id,
        "generated_at": utc_now(),
        "build_images_git_sha": source_sha,
        "release_tag": release_tag,
        "release_authority": release_authority,
        **authority_inputs,
        "plan": {"path": copied_plan.relative_to(bundle_dir).as_posix(), "sha256": plan_sha},
        "preparation": {
            "path": "preparation/preparation.json",
            "sha256": sha256_file(bundle_dir / "preparation" / "preparation.json"),
            "image": preparation["image"],
        },
        "host_facts": {"path": "host/host-facts.json", "sha256": sha256_file(bundle_dir / "host" / "host-facts.json")},
        "contracts": contracts,
        "expected_results": expected,
        "completed_results": len(result_records),
        "results": result_records,
        "artifacts": artifact_records(bundle_dir, exclude={bundle_path}),
        "claim_boundary": CLAIM_BOUNDARY,
    }
    write_json(bundle_path, bundle)
    verify_bundle(bundle_dir)
    print(bundle_dir)
    return bundle_dir


def verify_artifact_inventory(bundle_dir: pathlib.Path, records: Any) -> None:
    if not isinstance(records, list) or not records:
        raise NativeQualificationError("bundle artifact inventory is empty")
    observed = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise NativeQualificationError("bundle artifact record is invalid")
        require_keys(record, {"path", "size", "sha256"}, "bundle artifact record")
        relative = safe_relative(record["path"], "bundle artifact path")
        name = relative.as_posix()
        if name in seen:
            raise NativeQualificationError(f"duplicate bundle artifact: {name}")
        seen.add(name)
        path = bundle_dir / pathlib.Path(*relative.parts)
        if not path.is_file() or path.stat().st_size != record["size"] or sha256_file(path) != record["sha256"]:
            raise NativeQualificationError(f"bundle artifact integrity mismatch: {name}")
        observed.append(name)
    actual = sorted(
        path.relative_to(bundle_dir).as_posix()
        for path in bundle_dir.rglob("*")
        if path.is_file() and path.name != "bundle-manifest.json"
    )
    if sorted(observed) != actual:
        raise NativeQualificationError("bundle artifact inventory is incomplete or contains extras")


def verify_result(plan: dict[str, Any], path: pathlib.Path, expected: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file() or sha256_file(path) != expected.get("sha256"):
        raise NativeQualificationError(f"native result digest mismatch: {path}")
    result = load_json(path)
    require_keys(
        result,
        {
            "run_schema", "schema_version", "status", "evidence_class",
            "native_performance_authority", "tier", "repetition", "started_at",
            "finished_at", "marker", "measurement", "process_metrics",
            "raw_artifacts", "container_guard",
        },
        f"native result {path}",
    )
    if (
        result["run_schema"] != RUN_SCHEMA_ID
        or result["schema_version"] != 1
        or result["status"] != "passed"
        or result["evidence_class"] != EVIDENCE_CLASS
        or result["native_performance_authority"] is not False
        or result["tier"] != expected.get("tier")
        or result["repetition"] != expected.get("repetition")
    ):
        raise NativeQualificationError(f"native result authority or identity is invalid: {path}")
    require_sha256(result["marker"], "native result marker")
    guard = result["container_guard"]
    if not isinstance(guard, dict) or guard.get("passed") is not True or guard.get("measured_in_container") is not False or guard.get("extraction_containers") != []:
        raise NativeQualificationError(f"native result container guard failed: {path}")
    metrics = result["process_metrics"]
    required_metrics = {
        "user_seconds", "system_seconds", "max_rss_kib", "minor_faults", "major_faults",
        "input_blocks", "output_blocks", "voluntary_context_switches", "involuntary_context_switches",
    }
    if not isinstance(metrics, dict) or set(metrics) != required_metrics:
        raise NativeQualificationError(f"native result process metrics are incomplete: {path}")
    measurement = result["measurement"]
    if not isinstance(measurement, dict):
        raise NativeQualificationError(f"native result measurement is invalid: {path}")
    if result["tier"] in ("calibration", "ipc", "soak"):
        for field in ("p50_ns", "p95_ns", "p99_ns", "p999_ns", "max_ns", "offered_rate", "messages", "samples"):
            if not isinstance(measurement.get(field), int) or measurement[field] < 0:
                raise NativeQualificationError(f"native IPC metric is invalid: {field}")
        if measurement["samples"] < measurement["messages"] or measurement.get("coordinated_omission") != "expected-interval-correction":
            raise NativeQualificationError("native IPC loss or coordinated-omission contract failed")
        if measurement.get("seed") != ipc_seed(result["marker"]):
            raise NativeQualificationError("native IPC seed binding failed")
        histogram = path.parent / "latency.hlog"
        if not histogram.is_file() or histogram.stat().st_size == 0:
            raise NativeQualificationError("native raw HdrHistogram is missing")
    elif result["tier"] == "receipts":
        record = measurement.get("record", {})
        replay = measurement.get("replay", {})
        if (
            record.get("receipt") != measurement.get("mode")
            or record.get("marker") != result["marker"]
            or replay.get("marker") != result["marker"]
            or any(record.get(field) != 0 for field in ("duplicates", "reordered", "marker_mismatches"))
            or any(replay.get(field) != 0 for field in ("duplicates", "reordered", "marker_mismatches"))
            or record.get("completion_duration_ns", 0) < record.get("receipt_duration_ns", 0)
        ):
            raise NativeQualificationError("native receipt mapping or replay oracle failed")
    elif result["tier"] == "recovery":
        replay = measurement.get("replay", {})
        if (
            measurement.get("stale_health_rejected") is not True
            or measurement.get("expiry_wait_seconds") != plan["recovery"]["expiry_wait_seconds"]
            or replay.get("marker") != result["marker"]
            or replay.get("observed") != replay.get("expected")
            or any(replay.get(field) != 0 for field in ("duplicates", "reordered", "marker_mismatches"))
        ):
            raise NativeQualificationError("native recovery oracle failed")
    else:
        raise NativeQualificationError(f"unsupported native result tier: {result['tier']}")
    return result


def verify_bundle(bundle_dir: pathlib.Path) -> dict[str, Any]:
    bundle_path = bundle_dir / "bundle-manifest.json"
    bundle = load_json(bundle_path)
    require_keys(
        bundle,
        {
            "bundle_schema", "schema_version", "status", "evidence_class",
            "native_performance_authority", "containerized_user_outcome_authority",
            "fresh_install_cost_authority", "final_scoring_authority", "bundle_id",
            "generated_at", "build_images_git_sha", "release_tag", "release_authority", "runner",
            "release_passport", "release_check_report", "public_release_receipt", "plan",
            "preparation", "host_facts",
            "contracts", "expected_results", "completed_results", "results", "artifacts",
            "claim_boundary",
        },
        "native bundle",
    )
    if (
        bundle["bundle_schema"] != BUNDLE_SCHEMA_ID
        or bundle["schema_version"] != 1
        or bundle["status"] != "complete"
        or bundle["evidence_class"] != EVIDENCE_CLASS
        or bundle["native_performance_authority"] is not True
        or bundle["containerized_user_outcome_authority"] is not False
        or bundle["fresh_install_cost_authority"] is not False
        or bundle["final_scoring_authority"] is not False
        or bundle["claim_boundary"] != CLAIM_BOUNDARY
        or not GIT_SHA.fullmatch(str(bundle["build_images_git_sha"]))
        or not EXACT_RELEASE_TAG.fullmatch(str(bundle["release_tag"]))
    ):
        raise NativeQualificationError("native bundle authority boundary is invalid")

    bound_paths: dict[str, pathlib.Path] = {}
    for binding_name in (
        "runner", "release_passport", "release_check_report", "public_release_receipt"
    ):
        binding = bundle[binding_name]
        if not isinstance(binding, dict):
            raise NativeQualificationError(f"native bundle {binding_name} binding is invalid")
        require_keys(binding, {"path", "sha256"}, f"native bundle {binding_name}")
        relative = safe_relative(binding["path"], f"native {binding_name} path")
        path = bundle_dir / pathlib.Path(*relative.parts)
        if path.is_symlink() or not path.is_file() or sha256_file(path) != binding["sha256"]:
            raise NativeQualificationError(f"native bundle {binding_name} digest mismatch")
        bound_paths[binding_name] = path
    if sha256_file(SCRIPT_PATH) != bundle["runner"]["sha256"]:
        raise NativeQualificationError("executing native verifier does not match the bound runner")
    passport_authority = validate_release_authority(
        bound_paths["release_passport"],
        bound_paths["release_check_report"],
        bundle["build_images_git_sha"],
        bundle["release_tag"],
    )
    release_authority = bundle["release_authority"]
    if not isinstance(release_authority, dict):
        raise NativeQualificationError("native bundle public release authority is invalid")
    require_keys(
        release_authority,
        {
            "tag", "material_sha", "source_sha", "origin", "tag_ref", "api_url",
            "passport_sha256", "check_report_sha256",
        },
        "native bundle public release authority",
    )
    receipt = load_json(bound_paths["public_release_receipt"])
    require_keys(
        receipt,
        {
            "contract", "schema_version", "tag", "material_sha", "source_sha", "origin",
            "tag_ref", "api_url", "passport_sha256", "check_report_sha256",
        },
        "native bundle public release receipt",
    )
    receipt_authority = {
        key: value for key, value in receipt.items()
        if key not in {"contract", "schema_version"}
    }
    if (
        receipt["contract"] != PUBLIC_RELEASE_RECEIPT_CONTRACT
        or receipt["schema_version"] != 1
        or receipt_authority != release_authority
        or release_authority["tag"] != passport_authority["tag"]
        or release_authority["material_sha"] != passport_authority["material_sha"]
        or release_authority["source_sha"] != passport_authority["source_sha"]
        or release_authority["origin"] not in PUBLIC_ORIGIN_URLS
        or release_authority["tag_ref"] != f"refs/tags/{bundle['release_tag']}"
        or release_authority["api_url"] != f"{GITHUB_RELEASE_API}/{bundle['release_tag']}"
        or release_authority["passport_sha256"] != bundle["release_passport"]["sha256"]
        or release_authority["check_report_sha256"] != bundle["release_check_report"]["sha256"]
    ):
        raise NativeQualificationError("native bundle public release authority is inconsistent")

    plan_record = bundle["plan"]
    if not isinstance(plan_record, dict):
        raise NativeQualificationError("native bundle plan record is invalid")
    require_keys(plan_record, {"path", "sha256"}, "native bundle plan")
    plan_relative = safe_relative(plan_record["path"], "native plan path")
    plan_path = bundle_dir / pathlib.Path(*plan_relative.parts)
    if not plan_path.is_file() or sha256_file(plan_path) != plan_record["sha256"]:
        raise NativeQualificationError("native bundle plan digest mismatch")
    plan = load_json(plan_path)
    validate_plan_document(plan)

    contracts = bundle["contracts"]
    expected_contracts = {PLAN_SCHEMA_ID, RUN_SCHEMA_ID, BUNDLE_SCHEMA_ID}
    observed_contracts = set()
    if not isinstance(contracts, list) or len(contracts) != 3:
        raise NativeQualificationError("native bundle contract set is incomplete")
    for contract in contracts:
        if not isinstance(contract, dict):
            raise NativeQualificationError("native bundle contract record is invalid")
        require_keys(contract, {"path", "sha256"}, "native bundle contract")
        relative = safe_relative(contract["path"], "native contract path")
        path = bundle_dir / pathlib.Path(*relative.parts)
        if not path.is_file() or sha256_file(path) != contract["sha256"]:
            raise NativeQualificationError("native bundle contract digest mismatch")
        schema = load_json(path)
        if schema.get("additionalProperties") is not False:
            raise NativeQualificationError("native bundle contract is not fail-closed")
        observed_contracts.add(schema.get("$id"))
    if observed_contracts != expected_contracts:
        raise NativeQualificationError("native bundle contract identities are incomplete")

    for binding_name in ("preparation", "host_facts"):
        binding = bundle[binding_name]
        if not isinstance(binding, dict) or "path" not in binding or "sha256" not in binding:
            raise NativeQualificationError(f"native bundle {binding_name} binding is invalid")
        relative = safe_relative(binding["path"], f"native {binding_name} path")
        path = bundle_dir / pathlib.Path(*relative.parts)
        if not path.is_file() or sha256_file(path) != binding["sha256"]:
            raise NativeQualificationError(f"native bundle {binding_name} digest mismatch")
    preparation = load_json(bundle_dir / bundle["preparation"]["path"])
    if (
        preparation.get("image") != plan["kit"]["image"]
        or preparation.get("extraction_container_removed") is not True
        or preparation.get("measurement_runtime") != "host-process"
    ):
        raise NativeQualificationError("native kit preparation does not close the container boundary")
    facts = load_json(bundle_dir / bundle["host_facts"]["path"])
    if facts.get("host_state_mutated") is not False or facts.get("perf_counters_required") is not False:
        raise NativeQualificationError("native host facts violate the no-mutation/perf-optional policy")

    expected = expected_result_count(plan)
    results = bundle["results"]
    if (
        bundle["expected_results"] != expected
        or bundle["completed_results"] != expected
        or not isinstance(results, list)
        or len(results) != expected
    ):
        raise NativeQualificationError("native bundle repetitions are missing or inconsistent")
    verified = []
    for record in results:
        if not isinstance(record, dict):
            raise NativeQualificationError("native result index record is invalid")
        require_keys(record, {"path", "sha256", "tier", "repetition", "status"}, "native result index")
        if record["status"] != "passed":
            raise NativeQualificationError("native result index contains a failed run")
        relative = safe_relative(record["path"], "native result path")
        verified.append(verify_result(plan, bundle_dir / pathlib.Path(*relative.parts), record))
    verify_calibration(plan, verified)
    verify_artifact_inventory(bundle_dir, bundle["artifacts"])
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-plan")
    validate.add_argument("--plan", required=True, type=pathlib.Path)
    run = subparsers.add_parser("run")
    run.add_argument("--plan", required=True, type=pathlib.Path)
    run.add_argument("--release-passport", required=True, type=pathlib.Path)
    run.add_argument("--release-check-report", required=True, type=pathlib.Path)
    run.add_argument("--execute", action="store_true")
    verify = subparsers.add_parser("verify-bundle")
    verify.add_argument("--bundle", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        if args.command == "validate-plan":
            plan = validate_plan(args.plan)
            print(json.dumps(plan, indent=2, sort_keys=True))
        elif args.command == "run":
            if not args.execute:
                raise NativeQualificationError("run requires --execute")
            run_plan(
                args.plan.resolve(),
                args.release_passport.resolve(),
                args.release_check_report.resolve(),
            )
        else:
            bundle = verify_bundle(args.bundle.resolve())
            print(json.dumps({
                "status": bundle["status"],
                "native_performance_authority": bundle["native_performance_authority"],
                "completed_results": bundle["completed_results"],
            }, sort_keys=True))
    except (NativeQualificationError, OSError, subprocess.SubprocessError) as error:
        print(f"Aeron native qualification error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
