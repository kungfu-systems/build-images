#!/usr/bin/env python3
"""Validate locks and emit an explicitly unscored comparator run manifest."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "environment.lock.json"
COMPOSE_PATH = ROOT / "compose.yaml"
MANIFEST_SCHEMA_PATH = ROOT / "environment-manifest.schema.json"
MANIFEST_SCHEMA_ID = "urn:kungfu-systems:build-images:comparator-environment-manifest:v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST_REF = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
READY_PROFILES = ("aeron", "clickhouse", "postgres", "kungfu")


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
    required_kungfu_fields = (
        "version",
        "distribution_mode",
        "source_repository",
        "source_ref",
        "source_sha",
        "source_evidence_url",
        "builder_image",
        "builder_image_release",
        "runtime_image",
        "rustup_version",
        "rustup_init_url",
        "rustup_init_sha256",
        "rust_toolchain",
        "build_entrypoint",
        "claim_boundary",
    )
    for field in required_kungfu_fields:
        if not kungfu.get(field):
            errors.append(f"ready kungfu profile requires {field}")
    if kungfu.get("distribution_mode") != "pinned-source-build":
        errors.append("kungfu distribution_mode must be pinned-source-build")
    if not GIT_SHA.fullmatch(kungfu.get("source_sha", "")):
        errors.append("kungfu source must use an exact 40-character Git SHA")
    if not SHA256.fullmatch(kungfu.get("rustup_init_sha256", "")):
        errors.append("kungfu rustup installer must use an exact SHA-256")
    for field in ("builder_image", "runtime_image"):
        if not DIGEST_REF.fullmatch(kungfu.get(field, "")):
            errors.append(f"kungfu {field} must use an immutable sha256 digest")
    if kungfu.get("runtime_image") != runner:
        errors.append("kungfu runtime image must match the locked runner image")
    if kungfu.get("source_sha", "") not in kungfu.get("source_evidence_url", ""):
        errors.append("kungfu source evidence URL must identify the locked source SHA")

    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    for forbidden in ("privileged:", "network_mode: host", "/var/run/docker.sock"):
        if forbidden in compose:
            errors.append(f"compose contains forbidden setting: {forbidden}")
    for required in (
        runner,
        profiles["clickhouse"]["image"],
        profiles["postgres"]["image"],
        kungfu.get("builder_image", ""),
        kungfu.get("source_repository", ""),
        kungfu.get("source_sha", ""),
        kungfu.get("rustup_init_sha256", ""),
        kungfu.get("rust_toolchain", ""),
    ):
        if required not in compose:
            errors.append(f"compose is not aligned with lock: {required}")
    if ":latest" in compose:
        errors.append("compose must not use latest tags")

    if errors:
        raise ValueError("\n".join(errors))
    return lock


def emit_manifest(profile: str, project: str, output: pathlib.Path) -> None:
    lock = validate()
    selected = lock["profiles"].get(profile)
    if selected is None:
        raise ValueError(f"unknown profile: {profile}")
    if selected.get("status") != "ready":
        raise ValueError(f"profile {profile} is not runnable: {selected.get('blocked_reason', selected.get('status'))}")
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
