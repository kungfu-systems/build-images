#!/usr/bin/env python3
import json
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


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

    missing = sorted(set(manifests) - seen)
    if missing:
        errors.append("images.lock.json: missing images: " + ", ".join(missing))

    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

