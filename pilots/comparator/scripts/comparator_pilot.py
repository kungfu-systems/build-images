#!/usr/bin/env python3
"""Validate locks and emit an explicitly unscored comparator run manifest."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "environment.lock.json"
COMPOSE_PATH = ROOT / "compose.yaml"
KUNGFU_DOCKERFILE_PATH = ROOT / "images" / "kungfu" / "Dockerfile"
KUNGFU_PACKAGE_PATH = ROOT / "images" / "kungfu" / "kungfu-episodes-cli-linux-x64.tar.gz"
MANIFEST_SCHEMA_PATH = ROOT / "environment-manifest.schema.json"
MANIFEST_SCHEMA_ID = "urn:kungfu-systems:build-images:comparator-environment-manifest:v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST_REF = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
READY_PROFILES = ("aeron", "clickhouse", "postgres")


def load_lock() -> dict:
    with LOCK_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def validate() -> dict:
    lock = load_lock()
    errors: list[str] = []
    with MANIFEST_SCHEMA_PATH.open(encoding="utf-8") as handle:
        manifest_schema = json.load(handle)
    if manifest_schema.get("$id") != MANIFEST_SCHEMA_ID:
        errors.append("environment manifest schema has an unexpected $id")
    if manifest_schema.get("properties", {}).get("pilot_unscored", {}).get("const") is not True:
        errors.append("environment manifest schema must require pilot_unscored=true")
    if manifest_schema.get("properties", {}).get("performance_authority", {}).get("const") is not False:
        errors.append("environment manifest schema must require performance_authority=false")
    pilot = lock.get("pilot", {})
    if pilot.get("status") != "unscored" or pilot.get("performance_authority") is not False:
        errors.append("pilot must remain unscored and non-authoritative")

    runner = lock.get("runner", {}).get("image", "")
    if not DIGEST_REF.fullmatch(runner):
        errors.append("runner image must use an immutable sha256 digest")

    profiles = lock.get("profiles", {})
    for name in READY_PROFILES:
        profile = profiles.get(name, {})
        if profile.get("status") != "ready":
            errors.append(f"{name} must be ready")
    for name in ("clickhouse", "postgres"):
        if not DIGEST_REF.fullmatch(profiles.get(name, {}).get("image", "")):
            errors.append(f"{name} image must use an immutable sha256 digest")
    aeron = profiles.get("aeron", {})
    if not DIGEST_REF.fullmatch(aeron.get("base_image", "")):
        errors.append("aeron base image must use an immutable sha256 digest")
    if not SHA256.fullmatch(aeron.get("artifact_sha256", "")):
        errors.append("aeron artifact must use an exact SHA-256")

    kungfu = profiles.get("kungfu", {})
    if kungfu.get("status") != "package-input-required":
        errors.append("kungfu must require a prebuilt package input")
    if kungfu.get("distribution_mode") != "prebuilt-cli-package":
        errors.append("kungfu distribution_mode must be prebuilt-cli-package")
    if kungfu.get("package_filename") != KUNGFU_PACKAGE_PATH.name:
        errors.append("kungfu package filename is not aligned with the Docker input")
    if kungfu.get("package_schema") != "kungfu.product.cli/v1":
        errors.append("kungfu package must require the CLI product schema")
    for field in ("version", "package_sha256", "source_sha", "evidence_url"):
        if kungfu.get(field) is not None:
            errors.append(f"kungfu runtime input field must remain unset in the static lock: {field}")
    if not DIGEST_REF.fullmatch(kungfu.get("runtime_image", "")):
        errors.append("kungfu runtime image must use an immutable sha256 digest")
    if kungfu.get("runtime_image") != runner:
        errors.append("kungfu runtime image must match the locked runner image")
    required_inputs = {
        "KUNGFU_CLI_ARTIFACT_NAME",
        "KUNGFU_CLI_PACKAGE_SHA256",
        "KUNGFU_CLI_VERSION",
        "KUNGFU_CLI_SOURCE_SHA",
        "KUNGFU_CLI_EVIDENCE_URL",
    }
    if set(kungfu.get("required_runtime_inputs", [])) != required_inputs:
        errors.append("kungfu required runtime inputs are incomplete")

    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    kungfu_dockerfile = KUNGFU_DOCKERFILE_PATH.read_text(encoding="utf-8")
    for forbidden in ("privileged:", "network_mode: host", "/var/run/docker.sock"):
        if forbidden in compose:
            errors.append(f"compose contains forbidden setting: {forbidden}")
    for required in (
        runner,
        profiles["clickhouse"]["image"],
        profiles["postgres"]["image"],
        "KUNGFU_CLI_PACKAGE_SHA256",
        "KUNGFU_CLI_VERSION",
        "KUNGFU_CLI_SOURCE_SHA",
    ):
        if required not in compose:
            errors.append(f"compose is not aligned with lock: {required}")
    if ":latest" in compose:
        errors.append("compose must not use latest tags")
    for required in (
        kungfu.get("runtime_image", ""),
        kungfu.get("package_filename", ""),
        kungfu.get("package_schema", ""),
        "KUNGFU_CLI_PACKAGE_SHA256",
        "KUNGFU_CLI_VERSION",
        "KUNGFU_CLI_SOURCE_SHA",
    ):
        if required not in kungfu_dockerfile:
            errors.append(f"kungfu Dockerfile is not aligned with lock: {required}")
    for forbidden in ("git fetch", "git clone", "shifu build", "cargo build", "conan install"):
        if forbidden in kungfu_dockerfile:
            errors.append(f"kungfu package consumer must not build source: {forbidden}")

    if errors:
        raise ValueError("\n".join(errors))
    return lock


def resolve_subject(lock: dict, profile: str) -> dict:
    selected = lock["profiles"].get(profile)
    if selected is None:
        raise ValueError(f"unknown profile: {profile}")
    if profile != "kungfu":
        if selected.get("status") != "ready":
            raise ValueError(f"profile {profile} is not runnable: {selected.get('status')}")
        return selected

    supplied = {
        "artifact_name": os.environ.get("KUNGFU_CLI_ARTIFACT_NAME", ""),
        "version": os.environ.get("KUNGFU_CLI_VERSION", ""),
        "package_sha256": os.environ.get("KUNGFU_CLI_PACKAGE_SHA256", ""),
        "source_sha": os.environ.get("KUNGFU_CLI_SOURCE_SHA", ""),
        "evidence_url": os.environ.get("KUNGFU_CLI_EVIDENCE_URL", ""),
    }
    for field, value in supplied.items():
        if not value:
            raise ValueError(f"kungfu package run requires {field}")
    if not SHA256.fullmatch(supplied["package_sha256"]):
        raise ValueError("kungfu package run requires an exact SHA-256")
    if not GIT_SHA.fullmatch(supplied["source_sha"]):
        raise ValueError("kungfu package run requires an exact source Git SHA")
    if not KUNGFU_PACKAGE_PATH.is_file():
        raise ValueError(f"kungfu package is missing: {KUNGFU_PACKAGE_PATH}")
    actual_sha256 = hashlib.sha256(KUNGFU_PACKAGE_PATH.read_bytes()).hexdigest()
    if actual_sha256 != supplied["package_sha256"]:
        raise ValueError("kungfu package SHA-256 does not match the supplied lock")

    resolved = dict(selected)
    resolved.update(supplied)
    resolved["status"] = "ready"
    return resolved


def emit_manifest(profile: str, project: str, output: pathlib.Path) -> None:
    lock = validate()
    selected = resolve_subject(lock, profile)
    manifest = {
        "manifest_schema": MANIFEST_SCHEMA_ID,
        "schema_version": 1,
        "pilot_unscored": True,
        "performance_authority": False,
        "profile": profile,
        "project": project,
        "configuration_slot": "realistic-default",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "lock_sha256": hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest(),
        "subject": selected,
        "runner": lock["runner"],
        "claim_boundary": "Disposable Docker functional/recovery evidence; not a native performance result",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    emit = subparsers.add_parser("emit-manifest")
    emit.add_argument("--profile", required=True)
    emit.add_argument("--project", required=True)
    emit.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            validate()
            print("comparator pilot locks: ok")
        else:
            emit_manifest(args.profile, args.project, args.output)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"comparator pilot error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
