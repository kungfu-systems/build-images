#!/usr/bin/env python3
import json
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def load_manifests() -> dict[str, dict]:
    manifests: dict[str, dict] = {}
    for manifest_path in sorted((ROOT / "images").glob("*/image.toml")):
        with manifest_path.open("rb") as handle:
            data = tomllib.load(handle)
        manifests[data["name"]] = data
    return manifests


def main() -> int:
    lock_path = ROOT / "images.lock.json"
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    manifests = load_manifests()
    errors: list[str] = []

    if data.get("schema") != 1:
        errors.append("images.lock.json: schema must be 1")
    if not isinstance(data.get("tag"), str) or not data["tag"].startswith("v"):
        errors.append("images.lock.json: tag must be a v-prefixed release tag")
    if not isinstance(data.get("source"), str) or len(data["source"]) != 40:
        errors.append("images.lock.json: source must be a full commit SHA")

    seen: set[str] = set()
    entries_by_name = {
        entry.get("name"): entry
        for entry in data.get("images", [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    provenance_mode = any(
        any(key in entry for key in ("platform", "contract_major", "content", "release"))
        for entry in entries_by_name.values()
    )
    for entry in data.get("images", []):
        name = entry.get("name")
        if name not in manifests:
            errors.append(f"images.lock.json: unknown image {name!r}")
            continue
        if name in seen:
            errors.append(f"images.lock.json: duplicate image {name}")
        seen.add(name)
        expected_image = f"ghcr.io/kungfu-systems/build-images/{name}"
        if entry.get("image") != expected_image:
            errors.append(f"images.lock.json: {name} image must be {expected_image}")
        if not isinstance(entry.get("digest"), str) or not DIGEST_RE.match(entry["digest"]):
            errors.append(f"images.lock.json: {name} digest must be sha256:<64 hex>")
        manifest_commands = manifests[name].get("build", {}).get("test_commands", [])
        if entry.get("test_commands") != manifest_commands:
            errors.append(f"images.lock.json: {name} test_commands must match image.toml")
        if provenance_mode:
            platform_map = {"linux-x64": "linux/amd64", "linux-arm64": "linux/arm64"}
            expected_platform = platform_map.get(manifests[name].get("platform"))
            if entry.get("platform") != expected_platform:
                errors.append(f"images.lock.json: {name} platform must be {expected_platform}")
            if entry.get("contract_major") != manifests[name].get("contract_major"):
                errors.append(f"images.lock.json: {name} contract_major must match image.toml")
            parent = manifests[name].get("base", {}).get("image")
            expected_parent = entries_by_name.get(parent, {}).get("digest") if parent else None
            if entry.get("parent_digest") != expected_parent:
                errors.append(f"images.lock.json: {name} parent_digest must match its locked parent")
            content = entry.get("content")
            release = entry.get("release")
            if not isinstance(content, dict):
                errors.append(f"images.lock.json: {name} content provenance is required")
            else:
                if content.get("ref") != f"v{content.get('version', '')}":
                    errors.append(f"images.lock.json: {name} content version/ref mismatch")
                for key in ("source_sha", "material_sha"):
                    if not isinstance(content.get(key), str) or not SHA_RE.fullmatch(content[key]):
                        errors.append(f"images.lock.json: {name} content {key} must be a full SHA")
            if not isinstance(release, dict):
                errors.append(f"images.lock.json: {name} release provenance is required")
            else:
                expected_release = {
                    "version": data.get("tag", "").removeprefix("v"),
                    "ref": data.get("tag"),
                    "source_sha": data.get("source"),
                }
                for key, expected in expected_release.items():
                    if release.get(key) != expected:
                        errors.append(f"images.lock.json: {name} release {key} mismatch")
                if not isinstance(release.get("material_sha"), str) or not SHA_RE.fullmatch(release["material_sha"]):
                    errors.append(f"images.lock.json: {name} release material_sha must be a full SHA")
                if not isinstance(release.get("target_ref"), str) or not release["target_ref"]:
                    errors.append(f"images.lock.json: {name} release target_ref is required")

    missing = sorted(set(manifests) - seen)
    pending_first_publish = [
        name
        for name in missing
        if manifests[name].get("lock", {}).get("status") == "pending-first-publish"
    ]
    hard_missing = [name for name in missing if name not in pending_first_publish]
    if hard_missing:
        errors.append("images.lock.json: missing images: " + ", ".join(hard_missing))

    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    output = dict(data)
    if pending_first_publish:
        output["pending_first_publish"] = pending_first_publish
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
