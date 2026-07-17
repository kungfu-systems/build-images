#!/usr/bin/env python3
"""Materialize one exact package-bound Kungfu Phase B qualification plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import tarfile
import tempfile
from typing import Any
from urllib.parse import urlsplit

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
PILOT_DIR = SCRIPT_DIR.parent
TEMPLATE_PATH = PILOT_DIR / "plans" / "kungfu-phase-b-v1.template.json"
LOCK_PATH = PILOT_DIR / "environment.lock.json"
COMPOSE_PATH = PILOT_DIR / "compose.yaml"
REGISTRY_PATH = PILOT_DIR / "workload-adapters" / "registry.json"
PACKAGE_PATH = PILOT_DIR / "images" / "kungfu" / "kungfu-episodes-cli-linux-x64.tar.gz"
ADAPTER_ID = "kungfu-phase-b-v1"
PACKAGE_NAME = "kungfu-episodes-cli-linux-x64.tar.gz"
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TEMPLATE_SCHEMA = "urn:kungfu-systems:build-images:kungfu-phase-b-plan-template:v1"
PLAN_SCHEMA = "urn:kungfu-systems:build-images:comparator-qualification-plan:v1"
EVIDENCE_CLASS = "containerized-user-outcome-qualification"
JOBS = (
    "J1-multi-session-progress-triage",
    "J2-cross-repo-delivery-trust",
    "J3-interrupted-go-recovery-handoff",
)
TIERS = (
    "normal",
    "concurrent",
    "crash-recovery",
    "whole-root-restore",
    "historical-query",
    "schema-evolution",
    "new-agent-takeover",
)


class MaterializationError(ValueError):
    """A package or plan input violates the frozen Phase B contract."""


def load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MaterializationError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise MaterializationError(f"JSON root must be an object: {path}")
    return value


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_https(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise MaterializationError("package evidence URL must be an unauthenticated HTTPS coordinate")
    return value


def _archive_json(
    archive: tarfile.TarFile,
    suffix: str,
) -> tuple[str, dict[str, Any]]:
    members = [member for member in archive.getmembers() if member.isfile() and member.name.endswith(suffix)]
    if len(members) != 1:
        raise MaterializationError(f"package must contain exactly one {suffix}")
    member = members[0]
    if member.size > 4 * 1024 * 1024:
        raise MaterializationError(f"package metadata is unexpectedly large: {member.name}")
    stream = archive.extractfile(member)
    if stream is None:
        raise MaterializationError(f"package metadata cannot be read: {member.name}")
    try:
        value = json.loads(stream.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MaterializationError(f"package metadata is invalid JSON: {member.name}: {error}") from error
    if not isinstance(value, dict):
        raise MaterializationError(f"package metadata root must be an object: {member.name}")
    return member.name, value


def verify_package(
    package: pathlib.Path,
    expected_sha256: str,
    version: str,
    source_sha: str,
) -> dict[str, Any]:
    if package.name != PACKAGE_NAME or not package.is_file():
        raise MaterializationError(f"package must be one existing {PACKAGE_NAME}")
    if not SHA256.fullmatch(expected_sha256):
        raise MaterializationError("package SHA-256 must be lowercase hexadecimal")
    if not GIT_SHA.fullmatch(source_sha):
        raise MaterializationError("source SHA must be an exact 40-character commit")
    if not version or version.startswith("INPUT_"):
        raise MaterializationError("package version must be materialized")
    actual_sha256 = sha256_file(package)
    if actual_sha256 != expected_sha256:
        raise MaterializationError("package SHA-256 does not match the materialization input")
    try:
        with tarfile.open(package, "r:gz") as archive:
            for member in archive.getmembers():
                path = pathlib.PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise MaterializationError(f"package contains an unsafe path: {member.name}")
            product_name, product = _archive_json(archive, "/product.json")
            compatibility_name, compatibility = _archive_json(
                archive, "/runtime/product-compatibility.json"
            )
            upgrade_name, upgrade = _archive_json(
                archive, "/upgrade/kungfu-release-manifest.json"
            )
    except (OSError, tarfile.TarError) as error:
        raise MaterializationError(f"cannot inspect Kungfu package: {error}") from error
    prefix = product_name[: -len("product.json")]
    if compatibility_name != f"{prefix}runtime/product-compatibility.json":
        raise MaterializationError("package compatibility metadata is outside the product root")
    if upgrade_name != f"{prefix}upgrade/kungfu-release-manifest.json":
        raise MaterializationError("package upgrade metadata is outside the product root")
    entries = product.get("entries")
    if (
        product.get("schema") != "kungfu.product.cli/v1"
        or product.get("platform") != "linux-x64"
        or product.get("archive") != PACKAGE_NAME
        or not isinstance(entries, dict)
        or entries.get("compatibility") != "runtime/product-compatibility.json"
        or entries.get("upgradeManifest") != "upgrade/kungfu-release-manifest.json"
    ):
        raise MaterializationError("package product.json does not match the fixed Linux CLI contract")
    versions = compatibility.get("versions")
    if (
        compatibility.get("schema") != "kungfu.product.compatibility/v1"
        or compatibility.get("source_commit") != source_sha
        or not isinstance(versions, dict)
        or versions.get("product") != version
    ):
        raise MaterializationError("package compatibility metadata does not match source/version inputs")
    if (
        upgrade.get("schema") != "kungfu.product-upgrade.manifest/v1"
        or upgrade.get("productVersion") != version
        or upgrade.get("sourceCommit") != source_sha
        or upgrade.get("platform") != "linux"
        or upgrade.get("architecture") != "x64"
    ):
        raise MaterializationError("package upgrade manifest does not match source/version/platform inputs")
    return {
        "artifact_name": PACKAGE_NAME,
        "package_sha256": actual_sha256,
        "package_size_bytes": package.stat().st_size,
        "version": version,
        "source_sha": source_sha,
        "product_schema": product["schema"],
        "compatibility_schema": compatibility["schema"],
        "upgrade_schema": upgrade["schema"],
    }


def validate_template(template: dict[str, Any]) -> None:
    if template.get("template_schema") != TEMPLATE_SCHEMA:
        raise MaterializationError("Kungfu Phase B template schema is unsupported")
    if tuple(template.get("jobs", [])) != JOBS:
        raise MaterializationError("Kungfu Phase B template job set/order is not frozen")
    if tuple(template.get("tiers", [])) != TIERS:
        raise MaterializationError("Kungfu Phase B template tier set/order is not frozen")
    if template.get("repetitions") != 3:
        raise MaterializationError("Kungfu Phase B requires exactly three scored repetitions")
    if template.get("configuration_slot") != "realistic-default":
        raise MaterializationError("Kungfu Phase B template must use realistic-default")


def build_plan(
    template: dict[str, Any],
    package_identity: dict[str, Any],
    evidence_url: str,
) -> dict[str, Any]:
    validate_template(template)
    lock = load_json(LOCK_PATH)
    registry = load_json(REGISTRY_PATH)
    adapter = registry.get("adapters", {}).get(ADAPTER_ID)
    if not isinstance(adapter, dict):
        raise MaterializationError(f"workload adapter is not registered: {ADAPTER_ID}")
    scenarios = []
    for job_index, job_id in enumerate(JOBS, start=1):
        for tier in TIERS:
            scenarios.append(
                {
                    "id": f"j{job_index}-{tier}",
                    "job_id": job_id,
                    "tier": tier,
                    "steps": [
                        {
                            "id": template["step"]["id"],
                            "action": "workload-adapter",
                            "adapter_id": ADAPTER_ID,
                            "timeout_seconds": template["step"]["timeout_seconds"],
                            "oracles": [
                                {"type": "exit-code", "expected": 0},
                                {
                                    "type": "artifact-exists",
                                    "path": "adapter-receipt.json",
                                    "expected": True,
                                },
                                {
                                    "type": "artifact-text-contains",
                                    "path": "adapter-receipt.json",
                                    "expected": f'"job_id": "{job_id}"',
                                },
                                {
                                    "type": "artifact-text-contains",
                                    "path": "tier-evidence.json",
                                    "expected": f'"tier": "{tier}"',
                                },
                                {"type": "semantic-verdict", "expected": True},
                            ],
                        }
                    ],
                }
            )
    runner = lock.get("runner", {}).get("image")
    if not isinstance(runner, str) or "@sha256:" not in runner:
        raise MaterializationError("environment lock has no exact runner image")
    subject = {
        "artifact_name": package_identity["artifact_name"],
        "version": package_identity["version"],
        "package_sha256": package_identity["package_sha256"],
        "package_size_bytes": package_identity["package_size_bytes"],
        "source_sha": package_identity["source_sha"],
        "evidence_url": require_https(evidence_url),
    }
    return {
        "plan_schema": PLAN_SCHEMA,
        "schema_version": 1,
        "test_only": False,
        "evidence_class": EVIDENCE_CLASS,
        "charter": template["charter"],
        "fixture_set": template["fixture_set"],
        "profile": "kungfu",
        "configuration_slot": template["configuration_slot"],
        "environment": {
            "compose_path": "compose.yaml",
            "compose_sha256": sha256_file(COMPOSE_PATH),
            "environment_lock_path": "environment.lock.json",
            "environment_lock_sha256": sha256_file(LOCK_PATH),
            "runner_image": runner,
        },
        "adapter_registry": {
            "path": "workload-adapters/registry.json",
            "sha256": sha256_file(REGISTRY_PATH),
        },
        "workload_adapter": {"id": ADAPTER_ID, **adapter},
        "subject": subject,
        "repetitions": template["repetitions"],
        "scenarios": scenarios,
        "artifact_retention": template["artifact_retention"],
        "claim_boundary": template["claim_boundary"],
    }


def write_json_atomic(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
    try:
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=pathlib.Path, default=PACKAGE_PATH)
    parser.add_argument("--package-sha256", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--evidence-url", required=True)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        package = args.package.resolve()
        if package != PACKAGE_PATH.resolve():
            raise MaterializationError(f"package must be staged at {PACKAGE_PATH}")
        output = args.output.resolve()
        if PILOT_DIR.resolve() not in output.parents:
            raise MaterializationError("materialized plan output must stay inside pilots/comparator")
        identity = verify_package(package, args.package_sha256, args.version, args.source_sha)
        plan = build_plan(load_json(TEMPLATE_PATH), identity, args.evidence_url)
        if args.execute:
            write_json_atomic(output, plan)
        print(
            json.dumps(
                {
                    "status": "materialized" if args.execute else "planned",
                    "output": str(output),
                    "package": identity,
                    "plan_sha256": hashlib.sha256(
                        (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode("utf-8")
                    ).hexdigest(),
                    "scenario_count": len(plan["scenarios"]),
                    "required_step_count": len(plan["scenarios"]) * plan["repetitions"],
                },
                indent=2,
                sort_keys=True,
            )
        )
    except (MaterializationError, OSError) as error:
        print(f"Kungfu Phase B materialization error: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
