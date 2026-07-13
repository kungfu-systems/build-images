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
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DIGEST_REF = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
READY_PROFILES = ("aeron", "clickhouse", "postgres")


def load_lock() -> dict:
    with LOCK_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def validate() -> dict:
    lock = load_lock()
    errors: list[str] = []
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
    if kungfu.get("status") == "ready":
        for field in ("version", "artifact_url", "artifact_sha256", "release_evidence_url"):
            if not kungfu.get(field):
                errors.append(f"ready kungfu profile requires {field}")
        if not SHA256.fullmatch(kungfu.get("artifact_sha256", "")):
            errors.append("ready kungfu profile requires an exact SHA-256")
    elif kungfu.get("status") == "blocked-unpublished":
        for field in ("version", "artifact_url", "artifact_sha256", "release_evidence_url"):
            if kungfu.get(field) is not None:
                errors.append(f"blocked kungfu profile must leave {field} null")
    else:
        errors.append("kungfu status must be ready or blocked-unpublished")

    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    for forbidden in ("privileged:", "network_mode: host", "/var/run/docker.sock"):
        if forbidden in compose:
            errors.append(f"compose contains forbidden setting: {forbidden}")
    for required in (runner, profiles["clickhouse"]["image"], profiles["postgres"]["image"]):
        if required not in compose:
            errors.append(f"compose is not aligned with lock: {required}")

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
